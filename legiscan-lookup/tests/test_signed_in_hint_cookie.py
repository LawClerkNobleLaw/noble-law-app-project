"""
Tests for SIGNED_IN_HINT_COOKIE (app.py) — a deliberately non-HttpOnly
cookie set/cleared alongside the real session cookie on signup/login/
logout, whose only job is letting THEME_INIT_SCRIPT's synchronous,
pre-paint inline <script> tell "there's probably a session" from
plain document.cookie, since the real session cookie is HttpOnly and
unreadable from JS. See app.py's own comments on THEME_INIT_SCRIPT and
Handler._signed_in_hint_cookie_header() for the full reasoning
(default-dark-when-signed-out without a flash of the wrong theme).

Same live_server/real-HTTP-handler approach as
test_landing_redirect.py, for the same reason: this is response-header
behavior (Set-Cookie), not something db.py's functions expose.
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


def _post_json(server, path, payload, cookie=None):
    import json

    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if cookie:
            headers["Cookie"] = cookie
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        # http.client folds repeated response headers with the same name
        # into one comma-joined string via getheader(), which would
        # mangle two separate Set-Cookie values (cookie attributes can
        # themselves contain commas in principle, and this app's two
        # cookies are simplest read apart) — get_all keeps them as a
        # list of the two distinct header lines.
        return resp.status, resp.getheaders()
    finally:
        conn.close()


def _cookie_values(headers, name):
    """All Set-Cookie header lines whose cookie name matches, in order."""
    return [v for (k, v) in headers if k == "Set-Cookie" and v.startswith(f"{name}=")]


def test_signup_sets_both_the_session_cookie_and_the_signed_in_hint(live_server):
    status, headers = _post_json(
        live_server, "/api/signup", {"email": "hint-signup@example.com", "password": "TestPassword123!"}
    )

    assert status == 200
    session_cookies = _cookie_values(headers, accounts.SESSION_COOKIE)
    hint_cookies = _cookie_values(headers, app.SIGNED_IN_HINT_COOKIE)
    assert len(session_cookies) == 1
    assert len(hint_cookies) == 1
    assert "HttpOnly" in session_cookies[0]
    # The hint cookie is the whole point: it must NOT be HttpOnly, or
    # THEME_INIT_SCRIPT's document.cookie read can't see it at all.
    assert "HttpOnly" not in hint_cookies[0]
    assert hint_cookies[0].startswith(f"{app.SIGNED_IN_HINT_COOKIE}=1")


def test_login_sets_both_the_session_cookie_and_the_signed_in_hint(live_server):
    conn = db.get_connection()
    try:
        accounts.create_user(conn, "hint-login@example.com", "TestPassword123!")
    finally:
        conn.close()

    status, headers = _post_json(
        live_server, "/api/login", {"email": "hint-login@example.com", "password": "TestPassword123!"}
    )

    assert status == 200
    assert len(_cookie_values(headers, accounts.SESSION_COOKIE)) == 1
    hint_cookies = _cookie_values(headers, app.SIGNED_IN_HINT_COOKIE)
    assert len(hint_cookies) == 1
    assert hint_cookies[0].startswith(f"{app.SIGNED_IN_HINT_COOKIE}=1")


def test_logout_clears_both_cookies(live_server):
    conn = db.get_connection()
    try:
        user_id = accounts.create_user(conn, "hint-logout@example.com", "TestPassword123!")
        token = accounts.create_session(conn, user_id)
    finally:
        conn.close()

    status, headers = _post_json(
        live_server, "/api/logout", {}, cookie=f"{accounts.SESSION_COOKIE}={token}"
    )

    assert status == 200
    session_cookies = _cookie_values(headers, accounts.SESSION_COOKIE)
    hint_cookies = _cookie_values(headers, app.SIGNED_IN_HINT_COOKIE)
    assert len(session_cookies) == 1
    assert len(hint_cookies) == 1
    assert "Max-Age=0" in session_cookies[0]
    assert "Max-Age=0" in hint_cookies[0]


def test_theme_init_script_defaults_dark_only_when_hint_cookie_is_absent():
    """Not a browser/JS test (this suite has no JS engine) — just
    confirms the inline script app.py ships actually contains the
    cookie-gated fallback this whole feature depends on, so a future
    edit to THEME_INIT_SCRIPT can't silently drop the "signed-out
    defaults to dark" behavior without a test noticing."""
    script = app.THEME_INIT_SCRIPT
    assert f"'{app.SIGNED_IN_HINT_COOKIE}=1'" in script
    assert "setAttribute('data-theme', 'dark')" in script
    # The explicit localStorage choice (light OR dark) must still be
    # checked, and win, before the cookie fallback is ever consulted.
    assert "localStorage.getItem('theme')" in script
