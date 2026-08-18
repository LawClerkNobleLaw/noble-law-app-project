"""
accounts.py — individual accounts, layered inside the site's existing
shared LOOKUP_USER/PASSWORD login.

That outer Basic Auth login (see app.py) still gates the whole site —
this is a second, personal layer inside it: real per-person accounts,
so an account can hold its own lobbyist_profiles row (the Form
601-style firm info collected at sign-up) rather than everything being
anonymous behind one shared password.

Passwords are hashed with PBKDF2-HMAC-SHA256 (Python's own hashlib,
no new dependency — matches this whole project's "boring, well-
supported technology" preference) with a random per-user salt and
600,000 iterations (current OWASP guidance for PBKDF2-SHA256 as of this
writing). The stored value is "iterations$salt_hex$hash_hex" so the
iteration count can be raised later without breaking existing accounts.

Sessions are a random 256-bit token stored server-side in `sessions`
and set as an HttpOnly cookie — the cookie itself carries no meaning
other than "look this up," so nothing about a session can be forged by
tampering with the cookie value.
"""

import hashlib
import hmac
import re
import secrets
import time

PBKDF2_ITERATIONS = 600_000
SESSION_COOKIE = "session"


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def _verify_password(password, stored):
    try:
        iterations, salt, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(email):
    return bool(email) and bool(EMAIL_RE.match(email))


def create_user(conn, email, password):
    """Raises ValueError with a message safe to show the user (bad
    input, or email already registered) rather than a raw DB error."""
    email = (email or "").strip().lower()
    if not valid_email(email):
        raise ValueError("Enter a valid email address.")
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    existing = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        raise ValueError("An account with that email already exists.")
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, datetime('now'))",
        (email, _hash_password(password)),
    )
    conn.commit()
    return cur.lastrowid


def verify_login(conn, email, password):
    """Returns the user id on success, None on failure — deliberately
    the same return shape either way so callers can't accidentally leak
    via a different code path whether it was the email or the password
    that was wrong."""
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", ((email or "").strip().lower(),)
    ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return None
    return row["id"]


def create_session(conn, user_id):
    token = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, datetime('now'))",
        (token, user_id),
    )
    conn.commit()
    return token


def user_id_for_session(conn, token):
    if not token:
        return None
    row = conn.execute("SELECT user_id FROM sessions WHERE token = ?", (token,)).fetchone()
    return row["user_id"] if row else None


def destroy_session(conn, token):
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def save_profile(conn, user_id, fields):
    """fields: legal_name, registrant_type ('individual'|'firm'),
    bus_addr1/city/st/zip4, mail_same_as_bus (bool), mail_addr1/city/st/zip4
    (ignored when mail_same_as_bus is true), bus_phone, existing_filer_id.
    Upserts — a user can come back and correct their profile."""
    mail_same = bool(fields.get("mail_same_as_bus"))
    conn.execute(
        """INSERT INTO lobbyist_profiles
             (user_id, legal_name, registrant_type, bus_addr1, bus_city, bus_st, bus_zip4,
              mail_same_as_bus, mail_addr1, mail_city, mail_st, mail_zip4,
              bus_phone, existing_filer_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
             legal_name=excluded.legal_name, registrant_type=excluded.registrant_type,
             bus_addr1=excluded.bus_addr1, bus_city=excluded.bus_city,
             bus_st=excluded.bus_st, bus_zip4=excluded.bus_zip4,
             mail_same_as_bus=excluded.mail_same_as_bus, mail_addr1=excluded.mail_addr1,
             mail_city=excluded.mail_city, mail_st=excluded.mail_st, mail_zip4=excluded.mail_zip4,
             bus_phone=excluded.bus_phone, existing_filer_id=excluded.existing_filer_id""",
        (
            user_id, fields.get("legal_name"), fields.get("registrant_type"),
            fields.get("bus_addr1"), fields.get("bus_city"), fields.get("bus_st"), fields.get("bus_zip4"),
            1 if mail_same else 0,
            None if mail_same else fields.get("mail_addr1"),
            None if mail_same else fields.get("mail_city"),
            None if mail_same else fields.get("mail_st"),
            None if mail_same else fields.get("mail_zip4"),
            fields.get("bus_phone"), fields.get("existing_filer_id") or None,
        ),
    )
    conn.commit()


def get_profile(conn, user_id):
    row = conn.execute("SELECT * FROM lobbyist_profiles WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None
