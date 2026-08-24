"""
Tests for accounts.py — password hashing/verification, and (grouped
here by concern, even though the actual code lives in app.py's module-
level login-lockout guard, not accounts.py) the brute-force lockout
that sits in front of accounts.verify_login().
"""

import datetime

import pytest

import accounts
import app


# ── Password hashing/verification ──────────────────────────────────

def test_hash_password_round_trips():
    stored = accounts._hash_password("correct-horse-battery-staple")
    assert accounts._verify_password("correct-horse-battery-staple", stored)


def test_hash_password_rejects_wrong_password():
    stored = accounts._hash_password("correct-horse-battery-staple")
    assert not accounts._verify_password("wrong-password", stored)


def test_hash_password_uses_a_random_salt_by_default():
    # Same password, hashed twice with no explicit salt, must not
    # produce the same stored value — otherwise two users with the same
    # password would have identical rows, and a leaked hash table would
    # trivially reveal who shares a password.
    a = accounts._hash_password("same-password")
    b = accounts._hash_password("same-password")
    assert a != b
    assert accounts._verify_password("same-password", a)
    assert accounts._verify_password("same-password", b)


def test_hash_password_stores_iterations_and_salt_in_plain_text():
    stored = accounts._hash_password("x", salt="ab" * 16)
    iterations, salt, hash_hex = stored.split("$")
    assert int(iterations) == accounts.PBKDF2_ITERATIONS
    assert salt == "ab" * 16


def test_verify_password_returns_false_on_malformed_stored_value():
    # _verify_password is also asked to check _DUMMY_HASH-shaped values
    # and (in principle) whatever garbage might already be in a very
    # old row — it should fail closed, not raise.
    assert not accounts._verify_password("anything", "not-a-real-stored-hash")
    assert not accounts._verify_password("anything", "")
    assert not accounts._verify_password("anything", None)


# ── Email validation ────────────────────────────────────────────────

def test_valid_email_accepts_ordinary_addresses():
    assert accounts.valid_email("lawclerk@noblelawpc.com")


def test_valid_email_rejects_missing_at_or_domain_dot():
    assert not accounts.valid_email("not-an-email")
    assert not accounts.valid_email("missing-domain@")
    assert not accounts.valid_email("@missing-local.com")
    assert not accounts.valid_email("")
    assert not accounts.valid_email(None)


# ── create_user / verify_login ─────────────────────────────────────

def test_create_user_then_verify_login_succeeds(conn):
    user_id = accounts.create_user(conn, "New.User@Example.com", "a-real-password")
    assert accounts.verify_login(conn, "new.user@example.com", "a-real-password") == user_id


def test_create_user_lowercases_and_trims_email(conn):
    accounts.create_user(conn, "  Person@Example.com  ", "a-real-password")
    row = conn.execute("SELECT email FROM users").fetchone()
    assert row["email"] == "person@example.com"


def test_create_user_rejects_invalid_email(conn):
    with pytest.raises(ValueError):
        accounts.create_user(conn, "not-an-email", "a-real-password")


def test_create_user_rejects_short_password(conn):
    with pytest.raises(ValueError):
        accounts.create_user(conn, "person@example.com", "short")


def test_create_user_rejects_duplicate_email(conn):
    accounts.create_user(conn, "person@example.com", "a-real-password")
    with pytest.raises(ValueError):
        accounts.create_user(conn, "person@example.com", "a-different-password")


def test_verify_login_fails_with_wrong_password(conn):
    accounts.create_user(conn, "person@example.com", "a-real-password")
    assert accounts.verify_login(conn, "person@example.com", "wrong-password") is None


def test_verify_login_fails_for_nonexistent_email_without_raising(conn):
    # No user was ever created — this must return None, not raise, and
    # (per accounts.verify_login's own docstring) still runs a full
    # PBKDF2 verification against _DUMMY_HASH rather than short-
    # circuiting, so a timing attack can't distinguish "no such email"
    # from "wrong password" for a real one. Not asserting on timing
    # here (too flaky for a unit test) — just that the dummy-hash path
    # is real and doesn't blow up.
    assert accounts.verify_login(conn, "nobody@example.com", "whatever") is None
    assert accounts._verify_password("whatever", accounts._DUMMY_HASH) is False


# ── Sessions ────────────────────────────────────────────────────────

def test_create_session_then_look_it_up(conn):
    user_id = accounts.create_user(conn, "person@example.com", "a-real-password")
    token = accounts.create_session(conn, user_id)
    assert accounts.user_id_for_session(conn, token) == user_id


def test_user_id_for_session_rejects_unknown_token(conn):
    assert accounts.user_id_for_session(conn, "not-a-real-token") is None


def test_user_id_for_session_rejects_empty_or_none_token(conn):
    assert accounts.user_id_for_session(conn, "") is None
    assert accounts.user_id_for_session(conn, None) is None


def test_user_id_for_session_rejects_expired_session(conn):
    user_id = accounts.create_user(conn, "person@example.com", "a-real-password")
    token = accounts.create_session(conn, user_id)
    # Back-date the row past SESSION_TTL_DAYS instead of waiting real
    # days or mocking datetime.now() everywhere — created_at is a
    # plain stored TEXT timestamp, so this is a direct, honest way to
    # simulate "this session is old" against the real query.
    conn.execute(
        "UPDATE sessions SET created_at = datetime('now', ?) WHERE token = ?",
        (f"-{accounts.SESSION_TTL_DAYS + 1} days", token),
    )
    conn.commit()
    assert accounts.user_id_for_session(conn, token) is None


def test_destroy_session_invalidates_the_token(conn):
    user_id = accounts.create_user(conn, "person@example.com", "a-real-password")
    token = accounts.create_session(conn, user_id)
    accounts.destroy_session(conn, token)
    assert accounts.user_id_for_session(conn, token) is None


# ── Login lockout (app.py's module-level guard in front of
#    accounts.verify_login — see MAX_LOGIN_ATTEMPTS/LOGIN_LOCKOUT_WINDOW) ──

@pytest.fixture(autouse=True)
def _clear_login_failure_state():
    """The lockout state (app._login_failures) is a plain module-level
    dict, not stored per-test anywhere — clear it before AND after each
    test in this section so one test's lockout can't leak into the
    next one that happens to use the same email."""
    app._login_failures.clear()
    yield
    app._login_failures.clear()


def test_not_locked_out_before_any_failures():
    assert app._login_locked_out("person@example.com") is False


def test_locked_out_after_max_attempts():
    for _ in range(app.MAX_LOGIN_ATTEMPTS):
        app._record_login_failure("person@example.com")
    assert app._login_locked_out("person@example.com") is True


def test_not_locked_out_below_max_attempts():
    for _ in range(app.MAX_LOGIN_ATTEMPTS - 1):
        app._record_login_failure("person@example.com")
    assert app._login_locked_out("person@example.com") is False


def test_lockout_is_scoped_per_email():
    for _ in range(app.MAX_LOGIN_ATTEMPTS):
        app._record_login_failure("person-a@example.com")
    assert app._login_locked_out("person-a@example.com") is True
    assert app._login_locked_out("person-b@example.com") is False


def test_clear_login_failures_lifts_the_lockout():
    for _ in range(app.MAX_LOGIN_ATTEMPTS):
        app._record_login_failure("person@example.com")
    assert app._login_locked_out("person@example.com") is True
    app._clear_login_failures("person@example.com")
    assert app._login_locked_out("person@example.com") is False


def test_lockout_expires_after_the_window_passes():
    # Directly seed an old first-failure timestamp rather than waiting
    # real minutes or mocking datetime.now() — _login_failures' values
    # are a plain (count, first_failure_datetime) tuple this test can
    # construct honestly.
    old_enough = datetime.datetime.now() - app.LOGIN_LOCKOUT_WINDOW - datetime.timedelta(seconds=1)
    app._login_failures["person@example.com"] = (app.MAX_LOGIN_ATTEMPTS, old_enough)
    assert app._login_locked_out("person@example.com") is False
    # _login_locked_out() also deletes the stale entry once it notices
    # the window passed — a fresh failure right after should start a
    # brand-new count, not resume the old (already-expired) one.
    assert "person@example.com" not in app._login_failures


def test_record_login_failure_resets_count_after_window_expires():
    old_enough = datetime.datetime.now() - app.LOGIN_LOCKOUT_WINDOW - datetime.timedelta(seconds=1)
    app._login_failures["person@example.com"] = (app.MAX_LOGIN_ATTEMPTS, old_enough)
    app._record_login_failure("person@example.com")
    count, _ = app._login_failures["person@example.com"]
    assert count == 1
