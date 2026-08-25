"""
Tests for the "/" route's session check (see app.py's _do_GET) —
unlike every other page route in this app (see _require_user_for_page()
and the ~30 route handlers built on _current_user_id()), "/" used to
serve LANDING_PAGE unconditionally with no login check at all. A
signed-in visitor landing on "/" (e.g. clicking the logo — TOP_BRAND
and TOP_NAV_ACCOUNT_LINKS both keep pointing at "/" on purpose) would
see the marketing page again instead of their own app.

These are the first tests in this suite that exercise the real HTTP
handler (app.Handler) end to end rather than calling db.py functions
directly — the thing under test here IS the routing/cookie-handling
behavior that only exists at that layer, so a real ThreadingHTTPServer
is spun up on an ephemeral port for it. It's backed by a temp on-disk
sqlite database, not the usual :memory: `conn` fixture the rest of
this suite uses — Handler opens its own fresh connection per request
via db.get_connection(), so an in-memory database wouldn't be visible
across those separate connections the way one shared connection is
everywhere else in this suite.
"""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

import accounts
import app
import db


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()

    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


def _get(server, path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location"), resp.read()
    finally:
        conn.close()


def test_signed_out_visit_to_root_still_shows_landing_page(live_server):
    status, location, body = _get(live_server, "/")

    assert status == 200
    assert location is None
    assert b"The system of record for every bill your clients care about." in body


def test_signed_in_visit_to_root_redirects_to_flagged_without_rendering_landing_page(live_server):
    conn = db.get_connection()
    try:
        user_id = accounts.create_user(conn, "roottest@example.com", "TestPassword123!")
        token = accounts.create_session(conn, user_id)
    finally:
        conn.close()

    status, location, body = _get(live_server, "/", cookie=f"{accounts.SESSION_COOKIE}={token}")

    assert status == 302
    assert location == "/flagged"
    # No landing-page HTML was ever sent — not even accidentally
    # alongside the redirect.
    assert body == b""


def test_visit_to_root_with_a_bogus_session_cookie_still_shows_landing_page(live_server):
    # An expired/garbage/forged token must behave exactly like no
    # cookie at all, not raise or otherwise treat the visitor as
    # logged in.
    status, location, body = _get(live_server, "/", cookie=f"{accounts.SESSION_COOKIE}=not-a-real-token")

    assert status == 200
    assert location is None
    assert b"The system of record for every bill your clients care about." in body
