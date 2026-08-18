"""
db.py — SQLite connection and the watch-list read/write logic.

The database file (db/billwatch.db) is created on first run from
db/schema.sql and is not committed to git — it's local, per-machine data,
the same way the LegiScan API key itself is per-machine. Both app.py and
refresh_watchlist.py import this file instead of each having their own copy
of "how to open the database" or "how to save a bill."

Locally, the database file lives right next to this code (db/billwatch.db).
When hosted (Render), the code directory is rebuilt on every deploy but a
persistent disk is mounted elsewhere — so if BILLWATCH_DATA_DIR is set,
the database file goes there instead, surviving redeploys. schema.sql
itself always comes from the repo checkout either way — it's source, not
data, and isn't on the persistent disk.
"""

import os
import sqlite3

_REPO_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
DB_DIR = os.environ.get("BILLWATCH_DATA_DIR", _REPO_DB_DIR)
DB_PATH = os.path.join(DB_DIR, "billwatch.db")
SCHEMA_PATH = os.path.join(_REPO_DB_DIR, "schema.sql")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create the database file and apply schema.sql if needed. Safe to
    call every time the app or the daily job starts — every CREATE TABLE
    in schema.sql uses IF NOT EXISTS, so re-applying it is a no-op once
    the tables already exist."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = get_connection()
    try:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Hand-rolled migrations for columns added after a table already
    existed on someone's machine — CREATE TABLE IF NOT EXISTS in
    schema.sql only helps brand-new databases; it can't add a column to
    a bill_client_links table that was created before `position` existed.
    Each check is a no-op once the column's there, so this is safe to
    run on every startup, same as the schema.sql apply above."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(bill_client_links)")}
    if "position" not in cols:
        conn.execute("ALTER TABLE bill_client_links ADD COLUMN position TEXT NOT NULL DEFAULT 'watch'")


def upsert_bill(conn, bill):
    """Store (or replace) a bill's current snapshot, sponsors, and full
    history. LegiScan always returns the complete current sponsor list and
    complete history on every call — not just what's new — so this
    replaces rather than tries to diff, which is simpler and can't drift
    out of sync with what LegiScan actually reports."""
    conn.execute(
        """INSERT INTO bills (id, state, bill_number, session_label, title,
                               description, status_code, status_label,
                               status_date, url, change_hash, last_synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(id) DO UPDATE SET
             state=excluded.state, bill_number=excluded.bill_number,
             session_label=excluded.session_label, title=excluded.title,
             description=excluded.description, status_code=excluded.status_code,
             status_label=excluded.status_label, status_date=excluded.status_date,
             url=excluded.url, change_hash=excluded.change_hash,
             last_synced_at=excluded.last_synced_at""",
        (
            bill["id"], bill["state"], bill["bill_number"], bill.get("session_label"),
            bill.get("title"), bill.get("description"), bill.get("status_code"),
            bill.get("status_label"), bill.get("status_date"), bill.get("url"),
            bill.get("change_hash"),
        ),
    )
    conn.execute("DELETE FROM bill_sponsors WHERE bill_id = ?", (bill["id"],))
    conn.executemany(
        "INSERT INTO bill_sponsors (bill_id, name, party, role) VALUES (?,?,?,?)",
        [(bill["id"], s.get("name"), s.get("party"), s.get("role")) for s in bill.get("sponsors", [])],
    )
    conn.execute("DELETE FROM bill_status_history WHERE bill_id = ?", (bill["id"],))
    conn.executemany(
        "INSERT INTO bill_status_history (bill_id, date, chamber, action) VALUES (?,?,?,?)",
        [(bill["id"], h.get("date"), h.get("chamber"), h.get("action")) for h in bill.get("history", [])],
    )
    conn.execute("DELETE FROM bill_amendments WHERE bill_id = ?", (bill["id"],))
    conn.executemany(
        """INSERT INTO bill_amendments
             (bill_id, amendment_id, chamber, date, title, description, adopted, url)
           VALUES (?,?,?,?,?,?,?,?)""",
        [
            (
                bill["id"], a.get("amendment_id"), a.get("chamber"), a.get("date"),
                a.get("title"), a.get("description"), int(bool(a.get("adopted"))), a.get("url"),
            )
            for a in bill.get("amendments", [])
        ],
    )
    conn.execute("DELETE FROM bill_hearings WHERE bill_id = ?", (bill["id"],))
    conn.executemany(
        "INSERT INTO bill_hearings (bill_id, event_type, date, time, location, description) VALUES (?,?,?,?,?,?)",
        [
            (bill["id"], h.get("event_type"), h.get("date"), h.get("time"), h.get("location"), h.get("description"))
            for h in bill.get("hearings", [])
        ],
    )


def add_to_watchlist(conn, bill_id):
    conn.execute(
        """INSERT INTO watchlist (bill_id, added_at, last_checked_at)
           VALUES (?, datetime('now'), datetime('now'))
           ON CONFLICT(bill_id) DO NOTHING""",
        (bill_id,),
    )


def remove_from_watchlist(conn, bill_id):
    conn.execute("DELETE FROM watchlist WHERE bill_id = ?", (bill_id,))


def touch_watchlist(conn, bill_id):
    """Mark a watched bill as checked just now, regardless of whether
    anything on it actually changed."""
    conn.execute("UPDATE watchlist SET last_checked_at = datetime('now') WHERE bill_id = ?", (bill_id,))


def list_watchlist_bill_ids(conn):
    return [row["bill_id"] for row in conn.execute("SELECT bill_id FROM watchlist")]


# ── Flagged bills — a personal, per-user list, unlike the shared
# watchlist above. Reuses it underneath: flagging still upserts into
# `bills` and `watchlist` (via add_to_watchlist) so the daily refresh
# job keeps a flagged bill fresh — flagged_bills only adds "which user
# cares about this one," a many-to-many relation the single shared
# watchlist has no room for. ──

def flag_bill(conn, user_id, bill_id):
    add_to_watchlist(conn, bill_id)  # ensures the daily job keeps refreshing it
    conn.execute(
        """INSERT INTO flagged_bills (user_id, bill_id, flagged_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(user_id, bill_id) DO NOTHING""",
        (user_id, bill_id),
    )


def unflag_bill(conn, user_id, bill_id):
    conn.execute("DELETE FROM flagged_bills WHERE user_id = ? AND bill_id = ?", (user_id, bill_id))
    # Any client assignments for this (user, bill) go with it — an
    # unflagged bill shouldn't leave a dangling "assigned to client X"
    # relationship the UI no longer has anywhere to show.
    conn.execute("DELETE FROM bill_client_links WHERE user_id = ? AND bill_id = ?", (user_id, bill_id))
    still_flagged_by_someone = conn.execute(
        "SELECT 1 FROM flagged_bills WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    if not still_flagged_by_someone:
        # Nobody has this one flagged anymore — stop spending daily
        # LegiScan quota refreshing a bill nobody's tracking. Doesn't
        # touch `bills` itself; just the "worth refreshing daily" list.
        remove_from_watchlist(conn, bill_id)


def list_flagged_bills(conn, user_id):
    rows = conn.execute(
        """SELECT f.bill_id, f.flagged_at, w.last_checked_at,
                  b.state, b.bill_number, b.title, b.status_label, b.status_date, b.url
           FROM flagged_bills f
           JOIN bills b ON b.id = f.bill_id
           LEFT JOIN watchlist w ON w.bill_id = f.bill_id
           WHERE f.user_id = ?
           ORDER BY b.bill_number""",
        (user_id,),
    ).fetchall()
    result = [dict(r) for r in rows]
    clients_by_bill = clients_for_bills(conn, user_id, [r["bill_id"] for r in result])
    for r in result:
        r["assigned_clients"] = clients_by_bill.get(r["bill_id"], [])
    return result


# ── Clients — one-to-many with a user, unlike flagged_bills (many-to-
# many) or lobbyist_profiles (one-to-one). No cross-checking against
# lobbying_entities yet — existing_filer_id is stored for that future
# use, not acted on here. ──

def create_client(conn, user_id, fields):
    cur = conn.execute(
        """INSERT INTO clients
             (user_id, name, bus_addr1, bus_city, bus_st, bus_zip4,
              interests, existing_filer_id, created_at)
           VALUES (?,?,?,?,?,?,?,?, datetime('now'))""",
        (
            user_id, fields.get("name"),
            fields.get("bus_addr1"), fields.get("bus_city"),
            fields.get("bus_st"), fields.get("bus_zip4"),
            fields.get("interests"), fields.get("existing_filer_id") or None,
        ),
    )
    return cur.lastrowid


def list_clients(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM clients WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete_client(conn, user_id, client_id):
    """Scoped to user_id so one account can't delete another's client
    just by guessing/incrementing an id. Real bug found by testing this
    against actual assigned data rather than just the happy path: without
    clearing bill_client_links first, this raised an unhandled
    sqlite3.IntegrityError (FOREIGN KEY constraint failed) and crashed
    the request outright the moment a client was actually assigned to a
    bill — deleting a client should just take its assignments with it,
    not block on them."""
    conn.execute("DELETE FROM bill_client_links WHERE client_id = ? AND user_id = ?", (client_id, user_id))
    conn.execute("DELETE FROM clients WHERE id = ? AND user_id = ?", (client_id, user_id))


# ── Bill-to-client links — many-to-many, since a bill can matter to
# more than one client. ──

VALID_POSITIONS = ("support", "oppose", "watch")


def link_bill_to_client(conn, user_id, bill_id, client_id, position="watch"):
    """Raises ValueError (safe to show the user) if the client isn't
    actually theirs, the bill isn't actually one they've flagged, or
    position isn't one of the three allowed values — all checked
    explicitly rather than trusted from the request, since a bare
    foreign key can't express "belongs to the same user".

    Doubles as the "change position later" path: called again for a
    link that already exists, it updates position on the existing row
    instead of leaving it untouched — same endpoint handles both
    assigning a client to a bill and changing its stance afterward."""
    if position not in VALID_POSITIONS:
        raise ValueError("Position must be support, oppose, or watch.")
    owns_client = conn.execute(
        "SELECT 1 FROM clients WHERE id = ? AND user_id = ?", (client_id, user_id)
    ).fetchone()
    if not owns_client:
        raise ValueError("That client doesn't belong to your account.")
    has_flagged = conn.execute(
        "SELECT 1 FROM flagged_bills WHERE user_id = ? AND bill_id = ?", (user_id, bill_id)
    ).fetchone()
    if not has_flagged:
        raise ValueError("Flag this bill before assigning it to a client.")
    conn.execute(
        """INSERT INTO bill_client_links (user_id, bill_id, client_id, position, linked_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(user_id, bill_id, client_id) DO UPDATE SET position=excluded.position""",
        (user_id, bill_id, client_id, position),
    )


def unlink_bill_from_client(conn, user_id, bill_id, client_id):
    conn.execute(
        "DELETE FROM bill_client_links WHERE user_id = ? AND bill_id = ? AND client_id = ?",
        (user_id, bill_id, client_id),
    )


def clients_for_bills(conn, user_id, bill_ids):
    """bill_id -> [{id, name}, ...] for every bill in bill_ids, scoped
    to this user's own links and clients."""
    if not bill_ids:
        return {}
    placeholders = ",".join("?" * len(bill_ids))
    rows = conn.execute(
        f"""SELECT l.bill_id, c.id AS client_id, c.name, l.position
            FROM bill_client_links l JOIN clients c ON c.id = l.client_id
            WHERE l.user_id = ? AND l.bill_id IN ({placeholders})""",
        (user_id, *bill_ids),
    ).fetchall()
    by_bill = {}
    for r in rows:
        by_bill.setdefault(r["bill_id"], []).append(
            {"id": r["client_id"], "name": r["name"], "position": r["position"]}
        )
    return by_bill


# ── Action report — everything about one bill in a single call: its
# current status, full status history, amendment history, upcoming
# hearings, and (scoped to this user) which of their own clients it's
# assigned to and each one's current position. ──

def get_bill_report(conn, user_id, bill_id):
    bill = conn.execute(
        """SELECT id AS bill_id, state, bill_number, session_label, title,
                  description, status_label, status_date, url
           FROM bills WHERE id = ?""",
        (bill_id,),
    ).fetchone()
    if not bill:
        return None
    result = dict(bill)
    result["history"] = [
        dict(r) for r in conn.execute(
            "SELECT date, chamber, action FROM bill_status_history WHERE bill_id = ? ORDER BY date",
            (bill_id,),
        ).fetchall()
    ]
    result["amendments"] = [
        dict(r) for r in conn.execute(
            """SELECT date, chamber, title, description, adopted, url
               FROM bill_amendments WHERE bill_id = ? ORDER BY date""",
            (bill_id,),
        ).fetchall()
    ]
    # "Upcoming" is applied here, not stored that way — bill_hearings
    # keeps past events too, this just filters what the report shows.
    result["upcoming_hearings"] = [
        dict(r) for r in conn.execute(
            """SELECT event_type, date, time, location, description
               FROM bill_hearings WHERE bill_id = ? AND date >= date('now')
               ORDER BY date, time""",
            (bill_id,),
        ).fetchall()
    ]
    result["assigned_clients"] = clients_for_bills(conn, user_id, [bill_id]).get(bill_id, [])
    return result
