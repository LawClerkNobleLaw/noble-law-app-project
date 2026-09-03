"""
accounts.py — individual, per-person accounts.

The site itself (live lookup, lobbying search, signup/login) is open to
anyone with the URL — there's no outer shared login gating it. This
module is the actual access boundary for the personal features (flagged
bills, clients, action reports, profile): real per-person accounts, so
an account can hold its own lobbyist_profiles row (the Form 601-style
firm info collected at sign-up) rather than everything being anonymous
behind one shared password. (An earlier shared LOOKUP_USER/PASSWORD
Basic Auth gate existed before individual accounts matured into this
real per-user boundary; see app.py's module docstring — it was removed
once it became redundant.)

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

# How long a session stays valid with no activity check beyond its own
# existence — after this, the token is treated as if it were never
# created. Without this, a session token was valid forever once issued,
# so a cookie leaked via a shared machine or browser-history sync could
# never be forced to expire short of deleting its row by hand.
SESSION_TTL_DAYS = 30


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
    user_id = cur.lastrowid
    # Every account belongs to a firm from the moment it exists — a solo
    # lobbyist is a firm of one, not a special case with no organization
    # (see the organizations table in schema.sql). Named from the email
    # for now; sign-up step 2 collects the real legal name a moment later
    # and save_profile below renames it. Written here rather than through
    # db.py for the same reason every other statement in this module is:
    # accounts.py owns its own tables' SQL.
    org = conn.execute(
        "INSERT INTO organizations (name, created_at) VALUES (?, datetime('now'))",
        (email,),
    )
    conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (org.lastrowid, user_id))
    conn.commit()
    return user_id


_DUMMY_HASH = _hash_password("no-such-user-timing-decoy")


def verify_login(conn, email, password):
    """Returns the user id on success, None on failure — deliberately
    the same return shape either way so callers can't accidentally leak
    via a different code path whether it was the email or the password
    that was wrong.

    Always runs a full PBKDF2 verification, even when the email doesn't
    exist — hashing against _DUMMY_HASH instead of short-circuiting on
    `not row`. Skipping the hash for a nonexistent email would make
    those responses measurably faster than a real user's wrong-password
    attempt, letting an attacker enumerate registered emails just by
    timing /api/login."""
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", ((email or "").strip().lower(),)
    ).fetchone()
    stored_hash = row["password_hash"] if row else _DUMMY_HASH
    password_ok = _verify_password(password, stored_hash)
    if not row or not password_ok:
        return None
    return row["id"]


def create_session(conn, user_id):
    token = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, datetime('now'))",
        (token, user_id),
    )
    # Opportunistic cleanup, piggybacked on the one write that already
    # touches this table — not a background job, just means the sessions
    # table doesn't grow forever with rows nothing will ever accept again.
    conn.execute(
        "DELETE FROM sessions WHERE created_at <= datetime('now', ?)",
        (f"-{SESSION_TTL_DAYS} days",),
    )
    conn.commit()
    return token


def user_id_for_session(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT user_id FROM sessions WHERE token = ? AND created_at > datetime('now', ?)",
        (token, f"-{SESSION_TTL_DAYS} days"),
    ).fetchone()
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
    # The registrant's legal name is the firm's name as they gave it to
    # the state, so it's also the organization's — otherwise the firm
    # would go on being called by whoever's email address happened to
    # create the account (see create_user).
    legal_name = (fields.get("legal_name") or "").strip()
    if legal_name:
        conn.execute(
            """UPDATE organizations SET name = ?
               WHERE id = (SELECT org_id FROM users WHERE id = ?)""",
            (legal_name, user_id),
        )
    conn.commit()


def get_profile(conn, user_id):
    row = conn.execute("SELECT * FROM lobbyist_profiles WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None
