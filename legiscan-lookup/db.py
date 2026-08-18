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
        conn.commit()
    finally:
        conn.close()


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


def list_watchlist(conn):
    """Everything the /watchlist page needs to display, one row per
    watched bill, joined against its stored bill data."""
    rows = conn.execute(
        """SELECT w.bill_id, w.added_at, w.last_checked_at,
                  b.state, b.bill_number, b.title, b.status_label, b.status_date, b.url
           FROM watchlist w JOIN bills b ON b.id = w.bill_id
           ORDER BY b.bill_number"""
    ).fetchall()
    return [dict(r) for r in rows]
