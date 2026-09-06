"""
The account widget's state arrives with the page (I5).

The sidebar footer used to be empty on every page load until /api/me
answered, then filled in: a hole in the chrome, a layout shift when it
closed, and "am I signed in?" unanswerable in between — on a product
whose own notes record three-to-five-second cold starts.

End-to-end against the real handler for the same reason
test_landing_redirect.py is: the thing under test is the substitution
_send_html does per request, which only exists at that layer.
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
        return resp.status, resp.read().decode("utf-8")
    finally:
        conn.close()


def _signed_in(email="widget@example.com"):
    conn = db.get_connection()
    try:
        user_id = accounts.create_user(conn, email, "TestPassword123!")
        return accounts.create_session(conn, user_id)
    finally:
        conn.close()


def test_a_signed_in_page_arrives_knowing_who_it_is(live_server):
    token = _signed_in()

    status, body = _get(live_server, "/dashboard", cookie=f"{accounts.SESSION_COOKIE}={token}")

    assert status == 200
    assert 'data-account="user"' in body
    assert "widget@example.com" in body
    assert ">WI<" in body          # the avatar's initials, also server-side
    assert app.ACCOUNT_STATE_SLOT not in body


def test_a_signed_out_page_arrives_knowing_that_too(live_server):
    # /lookup renders the shell without requiring a session — one of the
    # two pages the old client-side fetch existed for.
    status, body = _get(live_server, "/lookup")

    assert status == 200
    assert 'data-account="guest"' in body
    assert app.ACCOUNT_STATE_SLOT not in body


def test_a_bogus_session_reads_as_signed_out(live_server):
    status, body = _get(live_server, "/lookup", cookie=f"{accounts.SESSION_COOKIE}=not-a-real-token")

    assert status == 200
    assert 'data-account="guest"' in body


def test_no_page_leaks_an_unfilled_slot(live_server):
    # The substitution is in _send_html, so this holds for every page
    # route without each one having to remember. The landing splash has
    # no widget at all and is checked for the same reason.
    token = _signed_in("leak@example.com")
    for path in ("/", "/lookup", "/lobbying", "/login", "/signup", "/flagged", "/clients", "/profile"):
        for cookie in (None, f"{accounts.SESSION_COOKIE}={token}"):
            status, body = _get(live_server, path, cookie=cookie)
            assert app.ACCOUNT_STATE_SLOT not in body, (path, cookie, status)
            assert app.ACCOUNT_EMAIL_SLOT not in body, (path, cookie, status)


def test_the_widget_no_longer_asks_the_network_who_it_is(live_server):
    # The point of the change: not a faster fetch, no fetch.
    assert "fetch('/api/me')" not in app.account_widget()
    # /api/me itself stays — other pages use it to notice a 401.
    status, body = _get(live_server, "/api/me")
    assert status == 200
