"""
The onboarding wizard route (/onboarding) and its client-free
flag-by-number endpoint (POST /api/flag-by-number).

Like test_landing_redirect, these exercise the real HTTP handler end to
end (routing + session/cookie handling live only at that layer), on a
ThreadingHTTPServer backed by a temp on-disk sqlite DB — Handler opens
its own connection per request, so the shared :memory: `conn` fixture
wouldn't be visible across them.
"""

import http.client
import json
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


def _signed_in_cookie():
    conn = db.get_connection()
    try:
        user_id = accounts.create_user(conn, "onboard@example.com", "TestPassword123!")
        token = accounts.create_session(conn, user_id)
    finally:
        conn.close()
    return f"{accounts.SESSION_COOKIE}={token}"


def _get(server, path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location"), resp.read()
    finally:
        conn.close()


def _post(server, path, payload, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        conn.request("POST", path, body=json.dumps(payload), headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_onboarding_redirects_to_signup_when_logged_out(live_server):
    status, location, _ = _get(live_server, "/onboarding")
    assert status == 302
    assert location == "/signup"


def test_onboarding_renders_the_wizard_when_signed_in(live_server):
    status, _, body = _get(live_server, "/onboarding", cookie=_signed_in_cookie())
    assert status == 200
    assert b"Set up Rotunda" in body
    assert b"How are you registered" in body


def test_flag_by_number_rejects_a_blank_number(live_server):
    status, body = _post(live_server, "/api/flag-by-number", {"bill_number": "  "},
                         cookie=_signed_in_cookie())
    assert status == 400
    assert b"Enter a bill number" in body


def test_flag_by_number_requires_sign_in(live_server):
    status, _ = _post(live_server, "/api/flag-by-number", {"bill_number": "SB122"})
    assert status == 401
