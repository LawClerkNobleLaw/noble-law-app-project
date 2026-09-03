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

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config

_REPO_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
# config.BILLWATCH_DATA_DIR is just the raw env var (or None) — the
# repo-local default below is this module's own concern, not
# config.py's, since it depends on where db.py itself lives on disk.
DB_DIR = config.BILLWATCH_DATA_DIR or _REPO_DB_DIR
DB_PATH = os.path.join(DB_DIR, "billwatch.db")
SCHEMA_PATH = os.path.join(_REPO_DB_DIR, "schema.sql")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # app.py runs as a ThreadingHTTPServer — more than one request's
    # connection can be open at once. SQLite's default rollback-journal
    # mode takes an exclusive lock for the whole duration of any write,
    # so a second thread's write (or even a read, depending on timing)
    # can hit "database is locked" outright once there's real
    # concurrent traffic — today's low request volume just hides it.
    # WAL lets readers keep going while one writer commits; busy_timeout
    # makes SQLite retry for up to 5s against whatever brief lock
    # contention WAL doesn't eliminate, instead of failing immediately.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn=None):
    """Create the database file and apply schema.sql if needed. Safe to
    call every time the app or the daily job starts — every CREATE TABLE
    in schema.sql uses IF NOT EXISTS, so re-applying it is a no-op once
    the tables already exist.

    conn is normally left as None — the real app applies this to its
    own on-disk file (DB_PATH), opening and closing its own connection.
    Tests pass an already-open in-memory connection instead, so the
    exact schema/migration path every real boot runs is also what every
    test's fixture runs — not a hand-maintained duplicate of it that
    could quietly drift out of sync with schema.sql."""
    owns_conn = conn is None
    if owns_conn:
        os.makedirs(DB_DIR, exist_ok=True)
        conn = get_connection()
    try:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        _migrate(conn)
        conn.commit()
    finally:
        if owns_conn:
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

    client_cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    for col in ("effective_date", "contract_period", "agencies_lobbied", "bus_phone"):
        if col not in client_cols:
            conn.execute(f"ALTER TABLE clients ADD COLUMN {col} TEXT")

    bill_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bills)")}
    if "amend_by_date" not in bill_cols:
        conn.execute("ALTER TABLE bills ADD COLUMN amend_by_date TEXT")

    filing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(prepared_filings)")}
    for col in ("pdf_field_data_hash", "client_row_ids", "trigger_date", "due_date"):
        if col not in filing_cols:
            conn.execute(f"ALTER TABLE prepared_filings ADD COLUMN {col} TEXT")


# ── Change detection for the daily digest email — see digest.py. Both
# functions here exist because upsert_bill() below is a full
# replace-not-diff (same reasoning as the sponsors/history/amendments/
# hearings/votes tables it rewrites): by the time anything could compare
# "old vs new," the old rows would already be gone. So the caller
# (refresh_watchlist.py) must call snapshot_bill_state() BEFORE
# upsert_bill(), then diff_bill_state() with that snapshot and the fresh
# bill dict AFTER — this module just supplies both halves. ──

def snapshot_bill_state(conn, bill_id):
    """Capture just enough of a bill's current state to diff against
    after the next upsert_bill() overwrites it. Returns None if this
    bill has never been synced before — nothing to compare against, and
    a first sighting isn't a "change"."""
    bill_row = conn.execute(
        "SELECT status_code, status_label, status_date FROM bills WHERE id = ?", (bill_id,)
    ).fetchone()
    if not bill_row:
        return None
    return {
        "status_code": bill_row["status_code"],
        "status_label": bill_row["status_label"],
        "status_date": bill_row["status_date"],
        "amendment_ids": {
            r["amendment_id"] for r in conn.execute(
                "SELECT amendment_id FROM bill_amendments WHERE bill_id = ?", (bill_id,)
            ).fetchall()
            if r["amendment_id"] is not None
        },
        "hearing_keys": {
            (r["date"], r["time"], r["event_type"]) for r in conn.execute(
                "SELECT date, time, event_type FROM bill_hearings WHERE bill_id = ?", (bill_id,)
            ).fetchall()
        },
        "vote_ids": {
            r["id"] for r in conn.execute("SELECT id FROM votes WHERE bill_id = ?", (bill_id,)).fetchall()
        },
    }


def diff_bill_state(before, bill):
    """Compare a snapshot from snapshot_bill_state() against a freshly
    fetched (not-yet-stored) bill dict from legiscan_client.get_bill_detail,
    and return one dict per change — empty if `before` is None or nothing
    actually changed. Four change types, in the order the digest email
    lists them: status, amendments, hearings, votes.

    Each dict carries:
      change_type  one of status/amendment/hearing/vote
      summary      short chip label for the flagged list ("Enrolled")
      description  the full sentence the digest email sends
      event_date   the date the change itself carries, or None — not the
                   same as when it was detected, since LegiScan often
                   reports an action days after it happened

    Structured rather than the bare sentences this used to return because
    the same diff now feeds two places with different needs: the digest
    email wants prose, and bill_change_events wants something the flagged
    list can render as a dated chip without re-parsing English."""
    if before is None:
        return []
    changes = []

    if bill.get("status_code") != before["status_code"]:
        new_label = bill.get("status_label") or "Unknown"
        changes.append({
            "change_type": "status",
            "summary": new_label,
            "description": (
                f"Status changed from {before['status_label'] or 'Unknown'} to "
                f"{new_label} (as of {bill.get('status_date') or 'an unknown date'})."
            ),
            "event_date": bill.get("status_date"),
        })

    for a in bill.get("amendments", []):
        if a.get("amendment_id") is not None and a["amendment_id"] not in before["amendment_ids"]:
            adopted = " — adopted" if a.get("adopted") else ""
            changes.append({
                "change_type": "amendment",
                "summary": "Amended" if not a.get("adopted") else "Amendment adopted",
                "description": (
                    f"New amendment in the {a.get('chamber') or 'legislature'} "
                    f"on {a.get('date') or 'an unspecified date'}{adopted}."
                ),
                "event_date": a.get("date"),
            })

    for h in bill.get("hearings", []):
        key = (h.get("date"), h.get("time"), h.get("event_type"))
        if key not in before["hearing_keys"]:
            when = h.get("date") or "an unspecified date"
            if h.get("time"):
                when += f" at {h['time']}"
            what = f" — {h['description']}" if h.get("description") else ""
            changes.append({
                "change_type": "hearing",
                "summary": "Hearing set",
                "description": f"Hearing scheduled for {when}{what}.",
                "event_date": h.get("date"),
            })

    for v in bill.get("votes", []):
        if v.get("roll_call_id") is not None and v["roll_call_id"] not in before["vote_ids"]:
            outcome = "passed" if v.get("passed") else "failed"
            changes.append({
                "change_type": "vote",
                "summary": f"Vote {outcome}",
                "description": (
                    f"Vote recorded in the {v.get('chamber') or 'legislature'}: "
                    f"{outcome} {v.get('yea') or 0}-{v.get('nay') or 0}."
                ),
                "event_date": v.get("date"),
            })

    return changes


def record_bill_changes(conn, bill_id, changes, detected_at=None):
    """Append what diff_bill_state() just found to bill_change_events.

    Append-only on purpose: this is the app's own record of what it
    observed, and rewriting it would reintroduce exactly the problem it
    exists to solve. Caller commits — refresh_one() batches this into the
    same transaction as the upsert it belongs to.

    Safe to call with an empty list, which is the common case: most bills
    on most days haven't moved."""
    if not changes:
        return 0
    detected_at = detected_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.executemany(
        """INSERT INTO bill_change_events
             (bill_id, detected_at, change_type, summary, description, event_date)
           VALUES (?,?,?,?,?,?)""",
        [
            (
                bill_id, detected_at, c["change_type"], c["summary"],
                c["description"], c.get("event_date"),
            )
            for c in changes
        ],
    )
    return len(changes)


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
    conn.execute("DELETE FROM votes WHERE bill_id = ?", (bill["id"],))
    conn.executemany(
        """INSERT INTO votes (id, bill_id, date, chamber, description, yea, nay, nv, absent, total, passed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                v["roll_call_id"], bill["id"], v.get("date"), v.get("chamber"), v.get("description"),
                v.get("yea"), v.get("nay"), v.get("nv"), v.get("absent"), v.get("total"),
                int(bool(v.get("passed"))),
            )
            for v in bill.get("votes", [])
            if v.get("roll_call_id") is not None  # id is the primary key — can't insert without one
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


def _days_between(start, end):
    """Whole days from one ISO 'YYYY-MM-DD' string to another, or None if
    either won't parse. Counted in dates, never in elapsed hours, so a
    hearing tomorrow morning is 1 rather than 0."""
    try:
        first = datetime.strptime(start, "%Y-%m-%d").date()
        second = datetime.strptime(end, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (second - first).days


def _next_hearings_for_bills(conn, bill_ids, today):
    """The soonest still-to-come hearing for each of these bills, keyed by
    bill_id, with days_until precomputed — what the flagged list's Next
    action column and its urgency sort are built from.

    days_until is worked out here rather than in the browser so the
    countdown is measured from the same California clock the "still to
    come" cut was made with (see today_in_california), instead of from
    whatever timezone the user's laptop happens to be set to.

    Undated hearings are skipped, unlike on the calendar: this column
    answers "when do I have to act," and a hearing LegiScan hasn't put a
    date on can't answer that. The calendar shows them because there the
    question is "what exists," not "what's next.\""""
    if not bill_ids:
        return {}
    placeholders = ",".join("?" for _ in bill_ids)
    rows = conn.execute(
        f"""SELECT bill_id, date, time, event_type, location, description
            FROM bill_hearings
            WHERE bill_id IN ({placeholders})
              AND date IS NOT NULL AND date != '' AND date >= ?
            ORDER BY date, time""",
        (*bill_ids, today),
    ).fetchall()

    # Rows arrive soonest-first, so the first one seen for a bill is its next.
    next_by_bill = {}
    for row in rows:
        if row["bill_id"] in next_by_bill:
            continue
        hearing = dict(row)
        hearing["days_until"] = _days_between(today, hearing["date"])
        next_by_bill[row["bill_id"]] = hearing
    return next_by_bill


def _latest_changes_for_bills(conn, bill_ids):
    """The most recently detected change for each of these bills, keyed by
    bill_id — what the flagged list's Last change column shows.

    Returns nothing for a bill the refresh job hasn't seen move yet, which
    on a database that predates bill_change_events means every bill. The
    column falls back to latest_activity_date in that case rather than
    sitting empty; see the table's comment in schema.sql.

    One refresh often finds several changes on the same bill at the same
    instant, so "the latest" can't be settled by time alone. They're
    ranked by what a lobbyist reads first: the bill moving stage, then a
    recorded vote, then an amendment, then a hearing being scheduled —
    that last one ranks lowest here only because the Next action column
    already shows it in full. The rest of the run is reported as a count
    (`also_count`) rather than dropped silently."""
    if not bill_ids:
        return {}
    placeholders = ",".join("?" for _ in bill_ids)
    rows = conn.execute(
        f"""SELECT bill_id, detected_at, change_type, summary, description, event_date,
                   CASE change_type
                     WHEN 'status' THEN 1 WHEN 'vote' THEN 2
                     WHEN 'amendment' THEN 3 ELSE 4
                   END AS rank
            FROM bill_change_events
            WHERE bill_id IN ({placeholders})
            ORDER BY detected_at DESC, rank ASC, id DESC""",
        tuple(bill_ids),
    ).fetchall()

    latest = {}
    for row in rows:
        bill_id = row["bill_id"]
        if bill_id not in latest:
            change = dict(row)
            change.pop("rank", None)
            change["also_count"] = 0
            latest[bill_id] = change
        elif row["detected_at"] == latest[bill_id]["detected_at"]:
            # Same refresh run as the headline change — worth saying there
            # was more, even though only one line fits in the column.
            latest[bill_id]["also_count"] += 1
    return latest


def list_flagged_bills(conn, user_id, today=None):
    rows = conn.execute(
        """SELECT f.bill_id, f.flagged_at, w.last_checked_at,
                  b.state, b.bill_number, b.title, b.status_label, b.status_date, b.url,
                  (SELECT MAX(h.date) FROM bill_status_history h
                   WHERE h.bill_id = f.bill_id) AS latest_activity_date
           FROM flagged_bills f
           JOIN bills b ON b.id = f.bill_id
           LEFT JOIN watchlist w ON w.bill_id = f.bill_id
           WHERE f.user_id = ?
           ORDER BY b.bill_number""",
        (user_id,),
    ).fetchall()
    result = [dict(r) for r in rows]
    bill_ids = [r["bill_id"] for r in result]
    clients_by_bill = clients_for_bills(conn, user_id, bill_ids)
    next_hearings = _next_hearings_for_bills(conn, bill_ids, today or today_in_california())
    latest_changes = _latest_changes_for_bills(conn, bill_ids)
    for r in result:
        r["assigned_clients"] = clients_by_bill.get(r["bill_id"], [])
        # None for a bill with nothing scheduled — the column says so in
        # words rather than leaving the cell blank.
        r["next_hearing"] = next_hearings.get(r["bill_id"])
        # None until the refresh job has seen this bill move at least once.
        r["last_change"] = latest_changes.get(r["bill_id"])
    return result


CAPITOL_TZ = "America/Los_Angeles"


def today_in_california():
    """Today's date in Sacramento, as an ISO 'YYYY-MM-DD' string.

    Every deadline this app deals with — hearing dates, FPPC filing
    dates — is a California date, so "is this still upcoming?" has to be
    asked in Pacific rather than in the server's clock. Hosted on Render
    the process runs UTC, which rolls over to tomorrow at 4pm or 5pm
    local; asking there would drop a hearing off the calendar during the
    working afternoon of the very day it happens.

    A string rather than a date object because every date column in this
    schema is TEXT in that same ISO format (LegiScan hands them over that
    way), so comparisons stay plain lexicographic string comparisons on
    both sides — no parsing, no date() conversion."""
    try:
        tz = ZoneInfo(CAPITOL_TZ)
    except Exception:
        # No system tz database. Rare, and not worth taking the calendar
        # down over — UTC runs ahead of Pacific, so the fallback's worst
        # case is the early-rollover described above rather than a
        # hearing that never appears at all.
        tz = timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d")


def list_hearings_for_flagged_bills(conn, user_id, today=None):
    """Every hearing across every bill this user has flagged, split into
    upcoming and past — for the /flagged/calendar view. Pure aggregation
    of what the daily refresh job (refresh_watchlist.py) already stores
    in bill_hearings for each bill it watches; no new LegiScan call
    happens here, just a join across tables that already exist.

    Split, and sorted in opposite directions, because the two halves
    answer different questions: upcoming runs soonest-first (what do I
    have to prepare for), past runs newest-first (what just happened).
    That ordering is settled here rather than in the template because the
    calendar groups *consecutive* same-date rows into a day — it depends
    on rows arriving already in the order they'll be displayed, so a
    re-sort in JS would silently shatter the day groups.

    A hearing with no date at all counts as upcoming: LegiScan has told
    us a hearing exists without saying when, and that's something the
    user still needs to see rather than something to file under history.

    Returns the flagged-bill count alongside, so an empty calendar can
    say which bills it actually checked, and `today` so the view marks
    the current day from the same clock the split was made with instead
    of re-deriving it from whatever timezone the browser is in."""
    rows = conn.execute(
        """SELECT h.id, h.bill_id, h.event_type, h.date, h.time, h.location, h.description,
                  b.state, b.bill_number, b.title
           FROM bill_hearings h
           JOIN bills b ON b.id = h.bill_id
           JOIN flagged_bills f ON f.bill_id = h.bill_id AND f.user_id = ?""",
        (user_id,),
    ).fetchall()

    today = today or today_in_california()
    upcoming, past = [], []
    for row in rows:
        hearing = dict(row)
        if not hearing["date"] or hearing["date"] >= today:
            upcoming.append(hearing)
        else:
            past.append(hearing)

    # Undated hearings sort on '' and so land at the top of upcoming,
    # which is where an unscheduled-but-real hearing belongs.
    upcoming.sort(key=lambda h: (h["date"] or "", h["time"] or ""))
    past.sort(key=lambda h: (h["date"] or "", h["time"] or ""), reverse=True)

    flagged_count = conn.execute(
        "SELECT COUNT(*) FROM flagged_bills WHERE user_id = ?", (user_id,)
    ).fetchone()[0]

    return {
        "today": today,
        "flagged_count": flagged_count,
        "upcoming": upcoming,
        "past": past,
    }


def list_sponsor_vote_rollup(conn, user_id):
    """For every sponsor across this user's flagged bills: which of
    those specific bills they sponsored, and how each one's own votes
    turned out. Pure aggregation of bill_sponsors + votes, both already
    stored by the daily refresh job — no new LegiScan call.

    Grouped by sponsor NAME as stored in bill_sponsors (which doesn't
    carry LegiScan's people_id — shape_bill() never captured it, see
    legiscan_client.py), so two different sponsors who happen to share
    an identical name string would be merged here. Accepted as an
    unlikely edge case for one user's own handful of flagged bills
    rather than a reason to add a people_id column and re-backfill.

    Important: this is NOT each legislator's personal ballot. LegiScan's
    votes table (and the Votes panel on /lookup and /report) is a
    chamber-level roll-call tally — yea/nay/nv/absent counts — not a
    record of which way any individual voted. That level of detail
    exists on LegiScan's side (getRollCall, confirmed live to return
    each vote keyed by people_id) but this app has never called it;
    doing so would be a new integration, not reuse of what's already
    stored, so it's deliberately out of scope here."""
    sponsor_rows = conn.execute(
        """SELECT s.name, s.party, s.role, s.bill_id, b.state, b.bill_number, b.title
           FROM bill_sponsors s
           JOIN bills b ON b.id = s.bill_id
           JOIN flagged_bills f ON f.bill_id = s.bill_id AND f.user_id = ?
           ORDER BY s.name""",
        (user_id,),
    ).fetchall()

    votes_by_bill = {}
    for r in conn.execute(
        """SELECT v.bill_id, v.date, v.chamber, v.description, v.yea, v.nay, v.nv, v.absent, v.total, v.passed
           FROM votes v
           JOIN flagged_bills f ON f.bill_id = v.bill_id AND f.user_id = ?
           ORDER BY v.date""",
        (user_id,),
    ).fetchall():
        votes_by_bill.setdefault(r["bill_id"], []).append(dict(r))

    by_sponsor = {}
    for r in sponsor_rows:
        name = r["name"] or "Unknown"
        entry = by_sponsor.setdefault(name, {"name": name, "party": r["party"], "bills": []})
        entry["bills"].append({
            "bill_id": r["bill_id"], "state": r["state"], "bill_number": r["bill_number"],
            "title": r["title"], "role": r["role"], "votes": votes_by_bill.get(r["bill_id"], []),
        })
    return sorted(by_sponsor.values(), key=lambda s: s["name"])


# ── Support for the daily digest email — see digest.py. ──

def list_users_with_flagged_bills(conn):
    """[(user_id, email), ...] for every user who currently has at least
    one flagged bill — the candidate list the daily digest job starts
    from, before narrowing down to only those whose bills actually
    changed today."""
    rows = conn.execute(
        """SELECT DISTINCT u.id AS user_id, u.email
           FROM users u JOIN flagged_bills f ON f.user_id = u.id"""
    ).fetchall()
    return [(r["user_id"], r["email"]) for r in rows]


def list_flagged_bill_ids_for_user(conn, user_id):
    return {r["bill_id"] for r in conn.execute(
        "SELECT bill_id FROM flagged_bills WHERE user_id = ?", (user_id,)
    ).fetchall()}


def get_bill_basic(conn, bill_id):
    """Just enough about a bill to reference it in a digest email —
    not the full db.get_bill_report() payload, which pulls history/
    amendments/hearings the digest doesn't need."""
    row = conn.execute(
        "SELECT id AS bill_id, state, bill_number, title FROM bills WHERE id = ?", (bill_id,)
    ).fetchone()
    return dict(row) if row else None


# ── Clients — one-to-many with a user, unlike flagged_bills (many-to-
# many) or lobbyist_profiles (one-to-one). No cross-checking against
# lobbying_entities yet — existing_filer_id is stored for that future
# use, not acted on here. ──

def create_client(conn, user_id, fields):
    cur = conn.execute(
        """INSERT INTO clients
             (user_id, name, bus_addr1, bus_city, bus_st, bus_zip4, bus_phone,
              interests, existing_filer_id, effective_date, contract_period,
              agencies_lobbied, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
        (
            user_id, fields.get("name"),
            fields.get("bus_addr1"), fields.get("bus_city"),
            fields.get("bus_st"), fields.get("bus_zip4"), fields.get("bus_phone"),
            fields.get("interests"), fields.get("existing_filer_id") or None,
            fields.get("effective_date") or None, fields.get("contract_period") or None,
            fields.get("agencies_lobbied") or None,
        ),
    )
    return cur.lastrowid


def list_clients(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM clients WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_client(conn, user_id, client_id):
    """Scoped to user_id, same reasoning as delete_client — one account
    can't view another's client just by guessing/incrementing an id."""
    row = conn.execute(
        "SELECT * FROM clients WHERE id = ? AND user_id = ?", (client_id, user_id)
    ).fetchone()
    return dict(row) if row else None


def get_client_bills(conn, user_id, client_id):
    """Every bill this client is currently linked to, with its own
    position — the reverse direction of clients_for_bills() above (that
    one goes bill -> clients; this one goes client -> bills), for the
    client detail page."""
    rows = conn.execute(
        """SELECT b.id AS bill_id, b.state, b.bill_number, b.title,
                  b.status_label, b.status_date, b.url, l.position
           FROM bill_client_links l
           JOIN bills b ON b.id = l.bill_id
           WHERE l.user_id = ? AND l.client_id = ?
           ORDER BY b.bill_number""",
        (user_id, client_id),
    ).fetchall()
    return [dict(r) for r in rows]


def update_client(conn, user_id, client_id, fields):
    """Scoped to user_id, same reasoning as delete_client. Added so a
    client created before effective_date/contract_period/
    agencies_lobbied existed (or before a user had that information
    handy) can still have them filled in later — without this, those
    three fields could only ever be set at creation time, which would
    leave every already-existing client permanently gapped."""
    conn.execute(
        """UPDATE clients
           SET name = ?, bus_addr1 = ?, bus_city = ?, bus_st = ?, bus_zip4 = ?, bus_phone = ?,
               interests = ?, existing_filer_id = ?, effective_date = ?,
               contract_period = ?, agencies_lobbied = ?
           WHERE id = ? AND user_id = ?""",
        (
            fields.get("name"), fields.get("bus_addr1"), fields.get("bus_city"),
            fields.get("bus_st"), fields.get("bus_zip4"), fields.get("bus_phone"),
            fields.get("interests"),
            fields.get("existing_filer_id") or None, fields.get("effective_date") or None,
            fields.get("contract_period") or None, fields.get("agencies_lobbied") or None,
            client_id, user_id,
        ),
    )


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
# hearings, roll-call votes, and (scoped to this user) which of their
# own clients it's assigned to and each one's current position. ──

def get_bill_report(conn, user_id, bill_id):
    bill = conn.execute(
        """SELECT id AS bill_id, state, bill_number, session_label, title,
                  description, status_label, status_date, url, amend_by_date
           FROM bills WHERE id = ?""",
        (bill_id,),
    ).fetchone()
    if not bill:
        return None
    result = dict(bill)
    result["history"] = [
        dict(r) for r in conn.execute(
            "SELECT date, chamber, action FROM bill_status_history WHERE bill_id = ? ORDER BY date DESC",
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
    result["votes"] = [
        dict(r) for r in conn.execute(
            """SELECT date, chamber, description, yea, nay, nv, absent, total, passed
               FROM votes WHERE bill_id = ? ORDER BY date""",
            (bill_id,),
        ).fetchall()
    ]
    result["assigned_clients"] = clients_for_bills(conn, user_id, [bill_id]).get(bill_id, [])
    # /api/report will upsert a bill straight from LegiScan on first
    # view now (see that route) — a bill can exist here without this
    # user having flagged it at all, so the report page needs an
    # explicit way to tell "not flagged yet" from "flagged, no client
    # assigned" instead of inferring it from assigned_clients being
    # empty either way.
    result["flagged"] = bool(conn.execute(
        "SELECT 1 FROM flagged_bills WHERE user_id = ? AND bill_id = ?", (user_id, bill_id)
    ).fetchone())
    return result


def set_bill_amend_by_date(conn, bill_id, amend_by_date):
    """Manually-entered "amend by" deadline on a bill — see the column
    comment on bills.amend_by_date in schema.sql for why this isn't
    synced from LegiScan. amend_by_date=None (or "") clears it, same as
    every other optional-field setter in this file. Not user-scoped —
    the deadline belongs to the bill itself, same as status_label, not
    to any one user's view of it, so every user tracking this bill sees
    the same date."""
    conn.execute(
        "UPDATE bills SET amend_by_date = ? WHERE id = ?",
        (amend_by_date or None, bill_id),
    )


# ── "Prepare my disclosure form" — see pdf_forms.py for how field_data
# actually turns into a filled PDF. Everything here just stores/reads
# that JSON snapshot and the sign-off state around it. ──

def _hash_field_data(field_data):
    """A fingerprint of exactly what values a filing's PDF would be
    built from right now. Used to prove (or disprove) that a
    previously-generated PDF still matches the current field_data — see
    "Staleness guard" in docs/disclosure-html-editor-plan.md.
    sort_keys=True so the same field_data always hashes the same way
    regardless of dict insertion order."""
    return hashlib.sha256(json.dumps(field_data, sort_keys=True).encode()).hexdigest()


def _row_to_prepared_filing(row):
    d = dict(row)
    d["field_data"] = json.loads(d["field_data"])
    d["confirmed_accurate"] = bool(d["confirmed_accurate"])
    d["client_row_ids"] = json.loads(d["client_row_ids"]) if d.get("client_row_ids") else []
    # True only when a PDF has actually been generated (mark_prepared_
    # filing_pdf_generated) since the last edit to field_data — the one
    # thing sign-off is allowed to trust.
    d["pdf_current"] = bool(d["pdf_field_data_hash"]) and d["pdf_field_data_hash"] == _hash_field_data(d["field_data"])
    # Counted from California's date, like every other deadline in this
    # app — the browser's clock isn't what a filing deadline runs on.
    # None when no due date has been set, which is every filing until the
    # lobbyist supplies the trigger; negative once it's overdue.
    d["days_until_due"] = _days_between(today_in_california(), d["due_date"]) if d.get("due_date") else None
    return d


def create_prepared_filing(conn, user_id, form_type, period_label, field_data, client_row_ids=None):
    """client_row_ids records which clients (in order) values_for_form_601
    already placed into the client rows baked into `field_data` — without
    this, a freshly-created draft's rows would look "empty" to the
    editor (see disclosure_fields.py) even though they're already
    pre-filled, until the lobbyist explicitly touches the row picker."""
    cur = conn.execute(
        """INSERT INTO prepared_filings (user_id, form_type, period_label, field_data, client_row_ids, created_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (user_id, form_type, period_label, json.dumps(field_data), json.dumps(client_row_ids) if client_row_ids else None),
    )
    return cur.lastrowid


def get_prepared_filing(conn, user_id, filing_id):
    """Scoped to user_id — same reasoning as delete_client/unflag_bill:
    never trust a client-supplied ID alone for a per-user record."""
    row = conn.execute(
        "SELECT * FROM prepared_filings WHERE id = ? AND user_id = ?", (filing_id, user_id)
    ).fetchone()
    return _row_to_prepared_filing(row) if row else None


def list_prepared_filings(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM prepared_filings WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    return [_row_to_prepared_filing(r) for r in rows]


def delete_prepared_filing(conn, user_id, filing_id):
    """Scoped to user_id, same reasoning as delete_client/unflag_bill —
    never trust a client-supplied ID alone for a per-user record. No
    cascade needed (unlike delete_client): nothing else references a
    prepared_filings row."""
    conn.execute("DELETE FROM prepared_filings WHERE id = ? AND user_id = ?", (filing_id, user_id))


def _edit_prepared_filing_field_data(conn, user_id, filing_id, new_field_data, client_row_ids=None):
    """Shared write path for every kind of in-place edit (a single
    field, or a whole client-row selection) — see update_prepared_
    filing_field / set_prepared_filing_client_rows. Every edit does two
    things atomically, in the same UPDATE:

    1. Clears pdf_field_data_hash — whatever PDF was last generated no
       longer provably matches this filing's data, so sign-off must be
       blocked until it's regenerated (see _hash_field_data / the
       "Staleness guard" doc).
    2. Reopens a signed-off filing — if this filing was 'ready_to_file',
       editing it now reverts status to 'draft' and clears the old
       sign-off (signed_name/confirmed_accurate/signed_at). This is
       intentionally unconditional rather than "only if it was signed":
       resetting fields that are already blank/draft is a harmless
       no-op, and it means there's exactly one code path to reason
       about instead of two. No history of the prior sign-off is kept
       (deliberately deferred — see the plan doc).

    client_row_ids is only passed by set_prepared_filing_client_rows;
    left as None (unchanged) for a plain single-field edit."""
    filing = get_prepared_filing(conn, user_id, filing_id)
    if not filing:
        raise ValueError("No prepared filing found.")
    if client_row_ids is None:
        conn.execute(
            """UPDATE prepared_filings
               SET field_data = ?, pdf_field_data_hash = NULL,
                   status = 'draft', signed_name = NULL, confirmed_accurate = 0, signed_at = NULL
               WHERE id = ? AND user_id = ?""",
            (json.dumps(new_field_data), filing_id, user_id),
        )
    else:
        conn.execute(
            """UPDATE prepared_filings
               SET field_data = ?, client_row_ids = ?, pdf_field_data_hash = NULL,
                   status = 'draft', signed_name = NULL, confirmed_accurate = 0, signed_at = NULL
               WHERE id = ? AND user_id = ?""",
            (json.dumps(new_field_data), json.dumps(client_row_ids), filing_id, user_id),
        )
    return get_prepared_filing(conn, user_id, filing_id)


def update_prepared_filing_field(conn, user_id, filing_id, field_key, value):
    """Autosaves one edited field (see app.py's /api/prepared-filings/
    field — that route is responsible for checking field_key is a real,
    editable field before calling this; this function just writes
    whatever key it's given)."""
    filing = get_prepared_filing(conn, user_id, filing_id)
    if not filing:
        raise ValueError("No prepared filing found.")
    field_data = filing["field_data"]
    field_data[field_key] = value
    return _edit_prepared_filing_field_data(conn, user_id, filing_id, field_data)


def set_prepared_filing_deadline(conn, user_id, filing_id, trigger_date, due_date):
    """Store a filing's deadline and the event it's counted from. Both may
    be None — a filing with no trigger entered yet simply has no due date,
    which the list says plainly rather than guessing at.

    Stores only; the derivation lives in disclosure_fields.due_date_for
    and is applied by the route. Keeping the statutory rule out of here is
    deliberate — this module's job is SQLite, and db.py sits below the
    form-domain modules rather than importing them. It also means an
    overridden due_date is stored exactly as given: the lobbyist's reading
    of their own deadline wins over the app's arithmetic."""
    cur = conn.execute(
        "UPDATE prepared_filings SET trigger_date = ?, due_date = ? WHERE id = ? AND user_id = ?",
        (trigger_date or None, due_date or None, filing_id, user_id),
    )
    if cur.rowcount == 0:
        raise ValueError("No prepared filing found.")
    return get_prepared_filing(conn, user_id, filing_id)


def set_prepared_filing_client_rows(conn, user_id, filing_id, client_ids, row_field_data):
    """Applies a new client-row selection/order. `row_field_data` is the
    already-built {field_name: value} dict for all 9 row slots (see
    pdf_forms.client_row_values) — this function just merges it in and
    remembers `client_ids` so the editor can show which client is in
    which row next time it loads."""
    filing = get_prepared_filing(conn, user_id, filing_id)
    if not filing:
        raise ValueError("No prepared filing found.")
    field_data = filing["field_data"]
    field_data.update(row_field_data)
    return _edit_prepared_filing_field_data(conn, user_id, filing_id, field_data, client_row_ids=client_ids)


def mark_prepared_filing_pdf_generated(conn, user_id, filing_id):
    """Stamps the filing with proof a PDF matching its exact current
    field_data now exists — call this right after actually generating
    that PDF. Any edit after this point (update_prepared_filing_field /
    set_prepared_filing_client_rows) clears the stamp again."""
    filing = get_prepared_filing(conn, user_id, filing_id)
    if not filing:
        raise ValueError("No prepared filing found.")
    conn.execute(
        "UPDATE prepared_filings SET pdf_field_data_hash = ? WHERE id = ? AND user_id = ?",
        (_hash_field_data(filing["field_data"]), filing_id, user_id),
    )
    return get_prepared_filing(conn, user_id, filing_id)


def sign_off_prepared_filing(conn, user_id, filing_id, signed_name, confirmed_accurate):
    """The only path that can move a filing from 'draft' to
    'ready_to_file' — a non-empty typed name AND the checkbox both have
    to be true, AND the filing's current field_data has to match a PDF
    that was actually generated (pdf_current — see _hash_field_data);
    any one of the three missing leaves it a draft. Raises ValueError
    (safe to show the user) for any of these, including a filing that
    doesn't exist or isn't theirs."""
    filing = get_prepared_filing(conn, user_id, filing_id)
    if not filing:
        raise ValueError("No prepared filing found.")
    signed_name = (signed_name or "").strip()
    if not signed_name or not confirmed_accurate:
        raise ValueError("A typed legal name and the confirmation checkbox are both required.")
    if not filing["pdf_current"]:
        raise ValueError("This filing has changed since the PDF was generated — regenerate it before signing off.")
    conn.execute(
        """UPDATE prepared_filings
           SET status = 'ready_to_file', signed_name = ?, confirmed_accurate = 1,
               signed_at = datetime('now')
           WHERE id = ? AND user_id = ?""",
        (signed_name, filing_id, user_id),
    )
    return get_prepared_filing(conn, user_id, filing_id)
