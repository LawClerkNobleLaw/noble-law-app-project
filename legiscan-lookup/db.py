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
import re
import sqlite3
from datetime import datetime, timedelta, timezone
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


# ── Whose data is it ────────────────────────────────────────────────────
#
# The firm's, not the individual's. Clients, flagged bills, positions,
# prepared filings, letters and saved searches are all the work product
# of an organization; the person who typed a given row in is recorded on
# it, and is not who it belongs to. See the organizations table in
# schema.sql for why this went in while every org still has one seat.
#
# Rather than adding an org_id to nine tables and backfilling it, the
# ownership predicate resolves through the user who created the row:
# "rows created by anyone in my organization". Same answer, one migration
# instead of nine, and every row keeps saying who acted — which is what
# the position history and the filing sign-off need anyway.
#
# The COALESCE(org_id, -id) pair is what makes a user with no
# organization (a database mid-migration, or a test that inserts a user
# directly) match only their own rows instead of matching every other
# org-less user's: a NULL org falls back to a value unique to that user.
# Negative because ids are positive, so the two spaces can't collide.
#
# One `?` in, in the same position the old `user_id = ?` had, so every
# call site's parameter tuple is unchanged.

def _org_scope(column="user_id"):
    return (f"{column} IN (SELECT id FROM users "
            "WHERE COALESCE(org_id, -id) = "
            "(SELECT COALESCE(org_id, -id) FROM users WHERE id = ?))")


ORG_SCOPE = _org_scope()


def org_id_for_user(conn, user_id):
    """This user's organization, or None on a database that predates
    them. Only needed by callers that write org-owned rows directly
    (the lobbyist roster); everything else scopes through ORG_SCOPE."""
    row = conn.execute("SELECT org_id FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["org_id"] if row else None


def create_organization(conn, name):
    cur = conn.execute(
        "INSERT INTO organizations (name, created_at) VALUES (?, datetime('now'))",
        (name or "My firm",),
    )
    return cur.lastrowid


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
    if "effective_date" not in cols:
        conn.execute("ALTER TABLE bill_client_links ADD COLUMN effective_date TEXT")

    client_cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    for col in ("effective_date", "contract_period", "agencies_lobbied", "bus_phone",
                "compensation_amount", "compensation_period", "notes"):
        if col not in client_cols:
            conn.execute(f"ALTER TABLE clients ADD COLUMN {col} TEXT")

    bill_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bills)")}
    if "amend_by_date" not in bill_cols:
        conn.execute("ALTER TABLE bills ADD COLUMN amend_by_date TEXT")

    flagged_cols = {row["name"] for row in conn.execute("PRAGMA table_info(flagged_bills)")}
    if "notes" not in flagged_cols:
        conn.execute("ALTER TABLE flagged_bills ADD COLUMN notes TEXT")
    if "last_viewed_at" not in flagged_cols:
        conn.execute("ALTER TABLE flagged_bills ADD COLUMN last_viewed_at TEXT")
    if "archived_at" not in flagged_cols:
        conn.execute("ALTER TABLE flagged_bills ADD COLUMN archived_at TEXT")

    filing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(prepared_filings)")}
    for col in ("pdf_field_data_hash", "client_row_ids", "trigger_date", "due_date"):
        if col not in filing_cols:
            conn.execute(f"ALTER TABLE prepared_filings ADD COLUMN {col} TEXT")
    if "signed_by" not in filing_cols:
        conn.execute("ALTER TABLE prepared_filings ADD COLUMN signed_by INTEGER REFERENCES users(id)")

    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "org_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN org_id INTEGER REFERENCES organizations(id)")
    _backfill_organizations(conn)
    _migrate_bill_views(conn)
    _migrate_calaccess_dates(conn)

    corpus_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bill_texts)")}
    if "sections_parsed_at" not in corpus_cols:
        conn.execute("ALTER TABLE bill_texts ADD COLUMN sections_parsed_at TEXT")


def _migrate_calaccess_dates(conn):
    """Rewrite CAL-ACCESS's "M/D/YYYY h:mm:ss AM" dates to ISO in place.

    The pipeline now normalizes on the way in (see
    calaccess-pipeline/calaccess_db.normalize_filing_date), but the rows
    already on disk were written before it did — 667k of them — and
    every one of them sorts lexically: "9/5/2007" above "10/31/2024",
    so the lobbying detail page's "most recent filings" led with 2007
    and the search page's MAX(filed_date) returned whichever string
    happened to start with the largest digit. Both queries are correct
    the moment the column is; neither needed changing.

    Done in SQL rather than by importing the pipeline's own normalizer,
    for two reasons: db.py has no business reaching into the sibling
    project (app.py does the sys.path insert for that, tests don't), and
    a bulk in-place rewrite of 667k rows belongs in the database rather
    than round-tripping every row through Python. CAST() on a string
    takes its leading integer, which is what reads the month and day
    without a nested substr for each.

    The guard is one row rather than a scan for any remaining slash: the
    UPDATE below is a single statement in the caller's transaction, so
    the column is either wholly converted or wholly not, and probing all
    667k rows on every boot to learn that costs about a second.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lobbying_disclosures'"
    ).fetchone():
        return
    probe = conn.execute(
        "SELECT filed_date FROM lobbying_disclosures WHERE filed_date IS NOT NULL LIMIT 1"
    ).fetchone()
    if not probe or "/" not in (probe["filed_date"] or ""):
        return
    for col in ("filed_date", "period_start", "period_end"):
        conn.execute(f"""
            UPDATE lobbying_disclosures
               SET {col} =
                   substr(substr({col}, instr({col}, '/') + 1),
                          instr(substr({col}, instr({col}, '/') + 1), '/') + 1, 4)
                   || '-' || printf('%02d', CAST({col} AS INTEGER))
                   || '-' || printf('%02d', CAST(substr({col}, instr({col}, '/') + 1) AS INTEGER))
             WHERE {col} LIKE '_/%/%' OR {col} LIKE '__/%/%'
        """)


def _backfill_organizations(conn):
    """Give every account without one an organization of its own.

    One org per existing user, not one org for everybody: these accounts
    predate the concept, and there is nothing in the data that says two
    of them are the same firm. Merging on a guess (a shared email domain,
    a matching legal_name) would put one lobbyist's clients in front of
    another's, which is the one mistake this whole layer exists to make
    impossible.

    Named from the registrant's own legal_name where a profile exists,
    since that is the firm's name as they gave it to the state, and from
    the email otherwise."""
    rows = conn.execute(
        """SELECT u.id, u.email, p.legal_name
           FROM users u LEFT JOIN lobbyist_profiles p ON p.user_id = u.id
           WHERE u.org_id IS NULL"""
    ).fetchall()
    for row in rows:
        name = (row["legal_name"] or "").strip() or (row["email"] or "").strip() or "My firm"
        org_id = create_organization(conn, name)
        conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (org_id, row["id"]))


def _migrate_bill_views(conn):
    """Move "I have read this bill" out of flagged_bills and into
    bill_views.

    The flag became the firm's; being up to date stayed the person's. On
    one row per user this is a straight copy — the column is left in
    place rather than dropped, since SQLite's DROP COLUMN is recent
    enough to be worth not depending on and a stale column costs nothing
    but the note in schema.sql saying it's dead."""
    flagged_cols = {row["name"] for row in conn.execute("PRAGMA table_info(flagged_bills)")}
    if "last_viewed_at" not in flagged_cols:
        return
    conn.execute(
        """INSERT OR IGNORE INTO bill_views (user_id, bill_id, last_viewed_at)
           SELECT user_id, bill_id, last_viewed_at FROM flagged_bills
           WHERE last_viewed_at IS NOT NULL"""
    )
    conn.execute("UPDATE flagged_bills SET last_viewed_at = NULL WHERE last_viewed_at IS NOT NULL")


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


# ── The searchable bill corpus — see bill_text.py and the schema.sql
# note. Distinct from everything above: `bills` and `watchlist` hold
# what this firm is tracking, `bill_texts` holds the whole session so
# that a search can find a bill nobody here has ever opened. ──

# FTS5's MATCH argument is a query language, not a string, so raw user
# input in it is at best a syntax error ("cannabis (licensing") and at
# worst a query that silently means something else (a bare "OR", a
# leading "-", a column filter like "title:x"). Every token is
# therefore requoted as a literal phrase, which is FTS5's own escape
# hatch: inside double quotes, its operators are just words.
#
# Quoted runs in the user's own input survive as phrases, so
#   cannabis "local control"
# is two terms, the second matched adjacently — the one bit of query
# syntax worth exposing, because a lobbyist searching a term of art is
# searching for the phrase and not for its words scattered apart.
_FTS_PHRASE = re.compile(r'"([^"]*)"')
_FTS_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def fts_query(text):
    """User input -> an FTS5 MATCH expression, or None if there is
    nothing left to search for once the punctuation is dropped."""
    text = (text or "").strip()
    if not text:
        return None
    terms = []
    for phrase in _FTS_PHRASE.findall(text):
        words = _FTS_WORD.findall(phrase)
        if words:
            terms.append(" ".join(words))
    remainder = _FTS_PHRASE.sub(" ", text)
    terms.extend(_FTS_WORD.findall(remainder))
    if not terms:
        return None
    # Every term required, rather than FTS5's default of any: a
    # two-word search that returns everything matching either word is a
    # search that got broader when the user tried to narrow it.
    return " AND ".join(f'"{term}"' for term in terms)


def upsert_bill_text(conn, row):
    """One bill's current version into the corpus. Full replace, like
    upsert_bill — a new version supersedes the old one outright, and
    there is nothing in the old row worth merging forward."""
    conn.execute(
        """INSERT INTO bill_texts
             (bill_id, bill_number, title, description, url, last_action,
              last_action_date, doc_id, version_date, version_type, body,
              byte_size, change_hash, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(bill_id) DO UPDATE SET
             bill_number=excluded.bill_number, title=excluded.title,
             description=excluded.description, url=excluded.url,
             last_action=excluded.last_action,
             last_action_date=excluded.last_action_date,
             doc_id=excluded.doc_id, version_date=excluded.version_date,
             version_type=excluded.version_type, body=excluded.body,
             byte_size=excluded.byte_size, change_hash=excluded.change_hash,
             fetched_at=datetime('now')""",
        (
            row["bill_id"], row.get("bill_number"), row.get("title"),
            row.get("description"), row.get("url"), row.get("last_action"),
            row.get("last_action_date"), row.get("doc_id"),
            row.get("version_date"), row.get("version_type"), row.get("body"),
            row.get("byte_size"), row.get("change_hash"),
        ),
    )


def indexed_change_hashes(conn):
    """bill_id -> the change_hash the corpus was built from, for deciding
    what needs re-fetching. One read for the whole corpus rather than a
    query per bill: the caller is comparing against a 5,060-row master
    list, and 5,060 point lookups to avoid one scan is the wrong trade.
    """
    return {
        row["bill_id"]: row["change_hash"]
        for row in conn.execute("SELECT bill_id, change_hash FROM bill_texts")
    }


def search_bill_text(conn, query, limit=200):
    """Full-text search across the corpus, best match first.

    Returns rows shaped like legiscan_client's search rows — same keys,
    so /lookup renders them with the code it already has — plus the
    `snippet` that is the whole point of searching text rather than
    titles: the user needs to see WHY a bill matched, because a hit
    somewhere in 40KB of statute is otherwise indistinguishable from a
    false positive.

    bm25() is negated because FTS5 returns it as "lower is better" and
    every other relevance number in this app sorts descending.

    The match markers are control characters, not "<mark>", and that is
    deliberate: snippet() wraps them around a span of the BILL'S OWN
    TEXT, which is fetched HTML that this app stripped tags out of but
    does not otherwise trust. Returning "<mark>" would mean the page
    could not escape the snippet without also escaping the markup it
    needs, i.e. the one field on the page that has to be inserted as
    HTML would be the one field built from remote input. Sentinels the
    page swaps for tags AFTER escaping keeps it inert; U+0002/U+0003
    are the choice because bill text does not contain control
    characters and JSON carries them fine.
    """
    match = fts_query(query)
    if not match:
        return []
    rows = conn.execute(
        """SELECT t.bill_id, t.bill_number, t.title, t.description, t.url,
                  t.last_action, t.last_action_date, t.version_type,
                  t.version_date,
                  snippet(bill_text_fts, 3, char(2), char(3), '…', 24) AS snippet,
                  -bm25(bill_text_fts, 4.0, 8.0, 4.0, 1.0) AS relevance
             FROM bill_text_fts
             JOIN bill_texts t ON t.bill_id = bill_text_fts.rowid
            WHERE bill_text_fts MATCH ?
            ORDER BY relevance DESC
            LIMIT ?""",
        (match, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def replace_bill_code_sections(conn, bill_id, sections):
    """The citations one bill makes, replacing whatever was there.

    Full replace rather than merge, same as upsert_bill_text above it:
    a re-parse means the sections are being re-derived from scratch,
    and a section dropped from an amended bill's title has to actually
    disappear or the search keeps answering for an edit that is no
    longer in the bill.

    Stamping sections_parsed_at in the same statement run is what makes
    "text fetched" and "citations derived" one fact rather than two that
    can disagree.
    """
    conn.execute("DELETE FROM bill_code_sections WHERE bill_id = ?", (bill_id,))
    conn.executemany(
        """INSERT OR IGNORE INTO bill_code_sections
             (bill_id, code, section, action, citation, is_range)
           VALUES (?,?,?,?,?,?)""",
        [
            (bill_id, s["code"], s["section"], s["action"],
             s.get("citation"), 1 if s.get("is_range") else 0)
            for s in sections
        ],
    )
    conn.execute(
        "UPDATE bill_texts SET sections_parsed_at = datetime('now') WHERE bill_id = ?",
        (bill_id,),
    )


def bills_needing_section_parse(conn):
    """Corpus rows whose text is here but whose citations aren't derived
    yet. Costs no API calls to work through — see the schema note on
    bill_texts.sections_parsed_at."""
    return [
        (row["bill_id"], row["body"])
        for row in conn.execute(
            "SELECT bill_id, body FROM bill_texts WHERE sections_parsed_at IS NULL AND body IS NOT NULL"
        )
    ]


def search_code_sections(conn, code=None, section=None, limit=200):
    """Bills touching a code section, newest action first.

    Either half of the citation may be absent: a section with no code
    ("17053.5") searches every code, and a code with no section ("Health
    and Safety Code") is "everything moving against this code" — which
    is a real question a lobbyist asks at the start of a session.

    Rows come back shaped like the other two search modes so /lookup
    renders them unchanged, plus `sections`: the citations that matched,
    which is this mode's answer to "why is this bill here" the way
    `snippet` is full-text search's.

    Ordered by last action rather than relevance — every hit is an exact
    citation match, so there is no relevance to rank by, and what
    distinguishes them is which one moved most recently.
    """
    if not code and not section:
        return []
    where, params = [], []
    if code:
        where.append("s.code = ?")
        params.append(code)
    if section:
        where.append("s.section = ?")
        params.append(section)
    params.append(limit)
    rows = conn.execute(
        f"""SELECT t.bill_id, t.bill_number, t.title, t.description, t.url,
                   t.last_action, t.last_action_date, t.version_type, t.version_date
              FROM bill_texts t
              JOIN bill_code_sections s ON s.bill_id = t.bill_id
             WHERE {' AND '.join(where)}
             GROUP BY t.bill_id
             ORDER BY t.last_action_date DESC, t.bill_id DESC
             LIMIT ?""",
        params,
    ).fetchall()
    results = [dict(row) for row in rows]
    matched = sections_for_bills(conn, [r["bill_id"] for r in results], code=code, section=section)
    for row in results:
        row["sections"] = matched.get(row["bill_id"], [])
    return results


def sections_for_bills(conn, bill_ids, code=None, section=None):
    """bill_id -> its citations, optionally narrowed to the ones that
    matched a search. One query for the whole page rather than one per
    row."""
    if not bill_ids:
        return {}
    placeholders = ",".join("?" for _ in bill_ids)
    where, params = [f"bill_id IN ({placeholders})"], list(bill_ids)
    if code:
        where.append("code = ?")
        params.append(code)
    if section:
        where.append("section = ?")
        params.append(section)
    out = {}
    for row in conn.execute(
        f"""SELECT bill_id, code, section, action, citation, is_range
              FROM bill_code_sections
             WHERE {' AND '.join(where)}
             ORDER BY code, section""",
        params,
    ):
        out.setdefault(row["bill_id"], []).append({
            "code": row["code"], "section": row["section"], "action": row["action"],
            "citation": row["citation"], "is_range": bool(row["is_range"]),
        })
    return out


def code_section_stats(conn):
    """How much of the corpus has been parsed, so an empty result can
    say which kind of empty it is."""
    row = conn.execute(
        """SELECT COUNT(*) AS parsed,
                  (SELECT COUNT(*) FROM bill_code_sections) AS citations
             FROM bill_texts WHERE sections_parsed_at IS NOT NULL"""
    ).fetchone()
    return {"parsed": row["parsed"], "citations": row["citations"]}


def corpus_stats(conn):
    """What the corpus holds, for telling the user whether a text search
    just searched the session or searched four bills."""
    row = conn.execute(
        """SELECT COUNT(*) AS bills, MAX(fetched_at) AS last_fetched,
                  COALESCE(SUM(byte_size), 0) AS bytes
             FROM bill_texts"""
    ).fetchone()
    return {
        "bills": row["bills"],
        "last_fetched": row["last_fetched"],
        "bytes": row["bytes"],
    }


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
           ON CONFLICT(user_id, bill_id) DO UPDATE SET archived_at = NULL""",
        (user_id, bill_id),
    )
    # The DO UPDATE is what makes "restore" the same action as flagging —
    # see archive_flagged_bill. A bill flagged, archived, then flagged
    # again comes back with its client assignments and notes intact,
    # because those were never touched.


def archive_flagged_bill(conn, user_id, bill_id):
    """Archive (P1-16), not delete. The old unflag_bill DELETEd this row
    and every bill_client_links row hanging off it — the client this bill
    was assigned to, and the position taken on it, gone with no way back.
    Archiving stops the daily refresh and the digest from caring about
    this bill without destroying either: bill_client_links and notes are
    left exactly as they were, and flag_bill (re-flagging) is the only
    "restore" this needs, since it clears archived_at back to NULL."""
    conn.execute(
        f"""UPDATE flagged_bills SET archived_at = datetime('now')
           WHERE {ORG_SCOPE} AND bill_id = ? AND archived_at IS NULL""",
        (user_id, bill_id),
    )
    still_active_for_someone = conn.execute(
        "SELECT 1 FROM flagged_bills WHERE bill_id = ? AND archived_at IS NULL", (bill_id,)
    ).fetchone()
    if not still_active_for_someone:
        # Nobody has this one actively flagged anymore — stop spending
        # daily LegiScan quota refreshing a bill nobody's tracking.
        # Doesn't touch `bills` itself, or the archived row; just the
        # "worth refreshing daily" list.
        remove_from_watchlist(conn, bill_id)


def list_archived_bills(conn, user_id):
    """The firm's archived flags, most recently archived first — the
    other half of P1-16's "prefer archive over delete": somewhere to see
    what was archived and restore it. Client assignments and notes ride
    along, same as they did before archiving, so a restored bill comes
    back exactly as it left."""
    rows = conn.execute(
        f"""SELECT f.bill_id, f.archived_at, f.flagged_at,
                  b.state, b.bill_number, b.title, b.status_label, b.url
           FROM flagged_bills f
           JOIN bills b ON b.id = f.bill_id
           WHERE {_org_scope("f.user_id")} AND f.archived_at IS NOT NULL
           ORDER BY f.archived_at DESC""",
        (user_id,),
    ).fetchall()
    result = [dict(r) for r in rows]
    clients_by_bill = clients_for_bills(conn, user_id, [r["bill_id"] for r in result])
    for r in result:
        r["assigned_clients"] = clients_by_bill.get(r["bill_id"], [])
    return result


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


def _unread_counts_for_bills(conn, user_id, bill_ids):
    """How many recorded changes on each of these bills this user hasn't
    seen yet, keyed by bill_id — the flagged list's unread dot.

    "Seen" means this person opened the bill's report since the change was
    detected (bill_views.last_viewed_at, written by mark_bill_viewed). A
    bill they have never opened counts every change on it, rather than
    starting at zero: the point of the dot is to say "you have not looked
    at this", and a never-opened bill is the strongest case of that.

    The flag is the firm's and the dot is the reader's, so this joins
    bill_views on the individual while the flagged list around it is
    scoped to the organization. Two lobbyists at the same firm see the
    same bills and their own dots.

    Reading the digest email doesn't clear anything. It can't — the mark
    happens on the report page, and the email links to it, so following
    the link is what clears the bill. That's the intended path.

    Counting rows rather than storing a flag means this stays correct
    with no writes on the refresh side: the daily job appends to
    bill_change_events exactly as before and knows nothing about who has
    read what."""
    if not bill_ids:
        return {}
    placeholders = ",".join("?" for _ in bill_ids)
    rows = conn.execute(
        f"""SELECT f.bill_id, COUNT(e.id) AS unread
            FROM flagged_bills f
            LEFT JOIN bill_views v ON v.bill_id = f.bill_id AND v.user_id = ?
            JOIN bill_change_events e
              ON e.bill_id = f.bill_id
             AND (v.last_viewed_at IS NULL OR e.detected_at > v.last_viewed_at)
            WHERE {_org_scope("f.user_id")} AND f.bill_id IN ({placeholders})
            GROUP BY f.bill_id""",
        (user_id, user_id, *bill_ids),
    ).fetchall()
    return {row["bill_id"]: row["unread"] for row in rows}


def tracking_for_bills(conn, user_id, bill_ids):
    """bill_id -> {"flagged": True, "clients": [...]} for whichever of
    these bills this user already tracks — what search results are
    annotated with so a results page knows what the user has already
    settled.

    Takes LegiScan bill_ids (the same ids `bills.id` is keyed on) and
    silently returns nothing for the ones this app has never seen, which
    on a broad search is most of them. Cheap on purpose: two indexed
    reads over the user's own rows, no LegiScan call, no per-row work —
    a hundred-result page has to be able to afford this."""
    if not bill_ids:
        return {}
    placeholders = ",".join("?" for _ in bill_ids)
    flagged = {
        row["bill_id"] for row in conn.execute(
            f"""SELECT bill_id FROM flagged_bills
                WHERE {ORG_SCOPE} AND archived_at IS NULL AND bill_id IN ({placeholders})""",
            (user_id, *bill_ids),
        )
    }
    if not flagged:
        return {}
    clients_by_bill = clients_for_bills(conn, user_id, sorted(flagged))
    return {
        bill_id: {"flagged": True, "clients": clients_by_bill.get(bill_id, [])}
        for bill_id in flagged
    }

def list_flagged_bills(conn, user_id, today=None):
    rows = conn.execute(
        f"""SELECT f.bill_id, f.flagged_at, v.last_viewed_at, w.last_checked_at,
                  b.state, b.bill_number, b.title, b.status_label, b.status_date, b.url,
                  b.amend_by_date,
                  (SELECT MAX(h.date) FROM bill_status_history h
                   WHERE h.bill_id = f.bill_id) AS latest_activity_date
           FROM flagged_bills f
           JOIN bills b ON b.id = f.bill_id
           LEFT JOIN watchlist w ON w.bill_id = f.bill_id
           LEFT JOIN bill_views v ON v.bill_id = f.bill_id AND v.user_id = ?
           WHERE {_org_scope("f.user_id")} AND f.archived_at IS NULL
           ORDER BY b.bill_number""",
        # Twice: once for "has this reader seen it", once for "does this
        # reader's firm track it". Same person, two different questions.
        (user_id, user_id),
    ).fetchall()
    result = [dict(r) for r in rows]
    bill_ids = [r["bill_id"] for r in result]
    clients_by_bill = clients_for_bills(conn, user_id, bill_ids)
    today = today or today_in_california()
    next_hearings = _next_hearings_for_bills(conn, bill_ids, today)
    latest_changes = _latest_changes_for_bills(conn, bill_ids)
    unread = _unread_counts_for_bills(conn, user_id, bill_ids)
    for r in result:
        r["assigned_clients"] = clients_by_bill.get(r["bill_id"], [])
        # None for a bill with nothing scheduled — the column says so in
        # words rather than leaving the cell blank.
        r["next_hearing"] = next_hearings.get(r["bill_id"])
        # None until the refresh job has seen this bill move at least once.
        r["last_change"] = latest_changes.get(r["bill_id"])
        # How many recorded changes this user hasn't looked at yet. 0 is
        # the resting state, and the only state that renders no dot.
        r["unread_count"] = unread.get(r["bill_id"], 0)
        # bills.amend_by_date is the user's own hand-entered amendment
        # deadline (nothing to do with LegiScan). Counted here off the
        # same California `today` the hearing countdown uses, rather than
        # in the browser, so a reminder and a hearing on the same day
        # can't disagree about how far away that day is. None when the
        # field is empty or already past — the column only carries
        # deadlines still ahead of the user.
        r["amend_by_days_until"] = None
        if r.get("amend_by_date"):
            days = _days_between(today, r["amend_by_date"])
            r["amend_by_days_until"] = days if days is not None and days >= 0 else None
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
        f"""SELECT h.id, h.bill_id, h.event_type, h.date, h.time, h.location, h.description,
                  b.state, b.bill_number, b.title
           FROM bill_hearings h
           JOIN bills b ON b.id = h.bill_id
           JOIN flagged_bills f ON f.bill_id = h.bill_id
              AND {_org_scope("f.user_id")} AND f.archived_at IS NULL""",
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
        f"SELECT COUNT(*) FROM flagged_bills WHERE {ORG_SCOPE} AND archived_at IS NULL", (user_id,)
    ).fetchone()[0]

    return {
        "today": today,
        "flagged_count": flagged_count,
        "upcoming": upcoming,
        "past": past,
    }


def _bill_position_verdict(status_label, position):
    """How a bill's outcome lines up with one of the firm's own positions
    on it — 'with_us', 'against_us', 'pending' (still moving), or None
    for 'watch' (not a stance that can win or lose). P2-25's fix for real:
    the audit asked for "their vote against your client's position" —
    but LegiScan's votes table is a chamber-level tally, never a
    per-legislator ballot (see this function's caller), so there is no
    honest "their vote" to compare. This compares the BILL's own outcome
    instead, which the app already has.

    LegiScan's status is a small closed vocabulary (see STATUS_LABELS in
    legiscan_client.py) — passed/failed/vetoed are the only terminal
    ones (same three TERMINAL_STATUSES keys flagged_body.html's own
    next-action column uses), so this is a plain lookup, not a guess."""
    if position == "watch":
        return None
    status = (status_label or "").lower()
    if status not in ("passed", "failed", "vetoed"):
        return "pending"
    became_law = status == "passed"
    if position == "support":
        return "with_us" if became_law else "against_us"
    if position == "oppose":
        return "against_us" if became_law else "with_us"
    return None


def list_sponsor_vote_rollup(conn, user_id):
    """For every sponsor across this user's flagged bills: which of
    those specific bills they sponsored, how each one's own votes turned
    out, and — per assigned client — whether the bill's outcome landed
    with or against the position taken on it. Pure aggregation of
    bill_sponsors + votes + bill_client_links, all already stored by the
    daily refresh job or entered by the firm — no new LegiScan call.

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
    stored, so it's deliberately out of scope here. See
    _bill_position_verdict for what this builds instead."""
    sponsor_rows = conn.execute(
        f"""SELECT s.name, s.party, s.role, s.bill_id, b.state, b.bill_number, b.title, b.status_label
           FROM bill_sponsors s
           JOIN bills b ON b.id = s.bill_id
           JOIN flagged_bills f ON f.bill_id = s.bill_id
              AND {_org_scope("f.user_id")} AND f.archived_at IS NULL
           ORDER BY s.name""",
        (user_id,),
    ).fetchall()

    votes_by_bill = {}
    for r in conn.execute(
        f"""SELECT v.bill_id, v.date, v.chamber, v.description, v.yea, v.nay, v.nv, v.absent, v.total, v.passed
           FROM votes v
           JOIN flagged_bills f ON f.bill_id = v.bill_id
              AND {_org_scope("f.user_id")} AND f.archived_at IS NULL
           ORDER BY v.date""",
        (user_id,),
    ).fetchall():
        vote = dict(r)
        # A roll call where nobody voted no and nobody sat out carries no
        # information about anyone's disposition — the sponsor page
        # collapses these behind "show all roll calls" rather than
        # burying the contested ones under a wall of them.
        vote["unanimous"] = vote["nay"] == 0 and vote["nv"] == 0
        votes_by_bill.setdefault(r["bill_id"], []).append(vote)

    bill_ids = sorted({r["bill_id"] for r in sponsor_rows})
    clients_by_bill = clients_for_bills(conn, user_id, bill_ids)

    by_sponsor = {}
    for r in sponsor_rows:
        name = r["name"] or "Unknown"
        entry = by_sponsor.setdefault(name, {"name": name, "party": r["party"], "bills": []})
        positions = [
            {**c, "verdict": _bill_position_verdict(r["status_label"], c["position"])}
            for c in clients_by_bill.get(r["bill_id"], [])
        ]
        entry["bills"].append({
            "bill_id": r["bill_id"], "state": r["state"], "bill_number": r["bill_number"],
            "title": r["title"], "role": r["role"], "status_label": r["status_label"],
            "positions": positions, "votes": votes_by_bill.get(r["bill_id"], []),
        })
    result = sorted(by_sponsor.values(), key=lambda s: s["name"])
    for s in result:
        s["bill_count"] = len(s["bills"])
    return result


# ── Support for the daily digest email — see digest.py. ──

def list_users_with_flagged_bills(conn):
    """[(user_id, email), ...] for every user who currently has at least
    one flagged bill — the candidate list the daily digest job starts
    from, before narrowing down to only those whose bills actually
    changed today."""
    rows = conn.execute(
        """SELECT DISTINCT u.id AS user_id, u.email
           FROM users u JOIN flagged_bills f ON f.user_id = u.id
           WHERE f.archived_at IS NULL"""
    ).fetchall()
    return [(r["user_id"], r["email"]) for r in rows]


def list_recipients(conn):
    """[(user_id, email), ...] for every user the daily digest might have
    something to say to — anyone with a flagged bill or a saved search.

    Superset of list_users_with_flagged_bills, which it replaces as the
    digest's starting point: a user with nothing flagged but a saved
    search is exactly the account saved searches exist for, and the old
    list skipped them entirely. The narrowing to "actually has news
    today" still happens in build_user_digest.

    Matched through the organization, not the individual, for the same
    reason the flagged list is: a colleague who has never personally
    clicked a flag still works at a firm whose bills moved this morning,
    and the digest is what tells them."""
    rows = conn.execute(
        """SELECT DISTINCT u.id AS user_id, u.email FROM users u
           WHERE EXISTS (
                   SELECT 1 FROM flagged_bills f JOIN users o ON o.id = f.user_id
                    WHERE f.archived_at IS NULL
                      AND COALESCE(o.org_id, -o.id) = COALESCE(u.org_id, -u.id))
              OR EXISTS (
                   SELECT 1 FROM saved_searches s JOIN users o ON o.id = s.user_id
                    WHERE COALESCE(o.org_id, -o.id) = COALESCE(u.org_id, -u.id))"""
    ).fetchall()
    return [(r["user_id"], r["email"]) for r in rows]


def list_flagged_bill_ids_for_user(conn, user_id):
    return {r["bill_id"] for r in conn.execute(
        f"SELECT bill_id FROM flagged_bills WHERE {ORG_SCOPE} AND archived_at IS NULL", (user_id,)
    ).fetchall()}


def get_bill_basic(conn, bill_id):
    """Just enough about a bill to reference it in a digest email —
    not the full db.get_bill_report() payload, which pulls history/
    amendments/hearings the digest doesn't need."""
    row = conn.execute(
        "SELECT id AS bill_id, state, bill_number, title FROM bills WHERE id = ?", (bill_id,)
    ).fetchone()
    return dict(row) if row else None


# ── Digest settings — the user's side of the daily email ────────────────
#
# Everything here is per PERSON. See notification_prefs in schema.sql for
# why that differs from almost every other table in this app.
#
# A user with no row gets DEFAULT_NOTIFICATION_PREFS, which is exactly
# the behaviour the digest had before it was configurable: every change
# type, daily, to the account address, saved-search matches included. So
# "has never opened Profile" and "has opened Profile and changed
# nothing" are the same state, and no backfill was needed.

CHANGE_TYPES = ("status", "amendment", "hearing", "vote")
DIGEST_FREQUENCIES = ("daily", "weekdays", "weekly", "off")

DEFAULT_NOTIFICATION_PREFS = {
    "frequency": "daily",
    "event_types": list(CHANGE_TYPES),
    "include_matches": True,
    "extra_recipients": [],
}

# Deliberately permissive: the point is to catch a typo like "sam@" or a
# stray comma, not to adjudicate what the RFC allows. A real address that
# this rejects would be a bug; a bad address that this accepts just
# bounces, which the sending domain reports anyway.
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]{2,}$")

# One email, cc'd. Five is enough for an assistant, an associate and a
# client contact or two, and low enough that this can never quietly
# become a mailing list the firm forgot it was running.
MAX_EXTRA_RECIPIENTS = 5


def _split_recipients(raw):
    """Accepts what a person actually types into a single text box —
    commas, semicolons, newlines, or just spaces between addresses."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = re.split(r"[,;\s]+", str(raw))
    seen, out = set(), []
    for part in parts:
        addr = part.strip().strip("<>")
        key = addr.lower()
        if addr and key not in seen:
            seen.add(key)
            out.append(addr)
    return out


def validate_extra_recipients(raw):
    """Returns (addresses, error). The caller decides whether an error is
    a 400 or a message in the page; this only says which address is the
    problem, since "one of these five is wrong" is not an actionable
    thing to tell someone."""
    addrs = _split_recipients(raw)
    if len(addrs) > MAX_EXTRA_RECIPIENTS:
        return None, f"At most {MAX_EXTRA_RECIPIENTS} extra recipients."
    for addr in addrs:
        if not _EMAIL_RE.match(addr):
            return None, f"{addr} doesn't look like an email address."
    return addrs, None


def get_notification_prefs(conn, user_id):
    """This user's digest settings, with every default filled in. Never
    returns None — the absence of a row is a valid, meaningful state."""
    prefs = dict(DEFAULT_NOTIFICATION_PREFS)
    prefs["event_types"] = list(DEFAULT_NOTIFICATION_PREFS["event_types"])
    row = conn.execute(
        "SELECT * FROM notification_prefs WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        return prefs
    if row["frequency"] in DIGEST_FREQUENCIES:
        prefs["frequency"] = row["frequency"]
    # Intersected with CHANGE_TYPES rather than trusted, so a change to
    # the vocabulary can't be poisoned by a stale stored value.
    stored = {t.strip() for t in (row["event_types"] or "").split(",")}
    prefs["event_types"] = [t for t in CHANGE_TYPES if t in stored]
    prefs["include_matches"] = bool(row["include_matches"])
    prefs["extra_recipients"] = _split_recipients(row["extra_recipients"])
    return prefs


def save_notification_prefs(conn, user_id, fields):
    """Writes the whole row — this is a settings form, not a patch, and a
    field the caller left out means "unchecked", not "unchanged".

    Raises ValueError on a bad frequency or recipient list so the route
    can turn it into a 400 rather than storing something the digest job
    would have to defend itself against later."""
    frequency = (fields.get("frequency") or "daily").strip()
    if frequency not in DIGEST_FREQUENCIES:
        raise ValueError("Choose how often the digest should go out.")

    requested = fields.get("event_types")
    if requested is None:
        requested = []
    elif isinstance(requested, str):
        requested = [t.strip() for t in requested.split(",")]
    event_types = [t for t in CHANGE_TYPES if t in set(requested)]

    recipients, error = validate_extra_recipients(fields.get("extra_recipients"))
    if error:
        raise ValueError(error)

    conn.execute(
        """INSERT INTO notification_prefs
               (user_id, frequency, event_types, include_matches,
                extra_recipients, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
               frequency = excluded.frequency,
               event_types = excluded.event_types,
               include_matches = excluded.include_matches,
               extra_recipients = excluded.extra_recipients,
               updated_at = excluded.updated_at""",
        (user_id, frequency, ",".join(event_types),
         1 if fields.get("include_matches", True) else 0,
         ",".join(recipients) or None),
    )
    conn.commit()
    return get_notification_prefs(conn, user_id)


def list_digest_muted_bill_ids(conn, user_id):
    return {r["bill_id"] for r in conn.execute(
        "SELECT bill_id FROM digest_mutes WHERE user_id = ?", (user_id,)
    ).fetchall()}


def list_digest_mutes(conn, user_id):
    """The muted bills with enough about each to name it on the settings
    panel — a mute nobody can find again is a bug report waiting to
    happen ("it just stopped emailing me about SB1159")."""
    rows = conn.execute(
        """SELECT m.bill_id, b.state, b.bill_number, b.title, m.created_at
             FROM digest_mutes m JOIN bills b ON b.id = m.bill_id
            WHERE m.user_id = ?
            ORDER BY b.state, b.bill_number""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_digest_muted(conn, user_id, bill_id, muted):
    """Mute or unmute one bill for one person. Does not touch the flag —
    see digest_mutes in schema.sql."""
    if muted:
        conn.execute(
            """INSERT OR IGNORE INTO digest_mutes (user_id, bill_id, created_at)
               VALUES (?, ?, datetime('now'))""",
            (user_id, bill_id),
        )
    else:
        conn.execute(
            "DELETE FROM digest_mutes WHERE user_id = ? AND bill_id = ?",
            (user_id, bill_id),
        )
    conn.commit()
    return bool(muted)


def changes_by_bill_since(conn, since_date):
    """{bill_id: [change dict, ...]} for everything detected on or after
    since_date, shaped exactly like diff_bill_state()'s return so the
    digest builder can't tell the difference.

    This is what makes a weekly digest possible. The refresh job hands
    the builder only what changed in the last few minutes, which is the
    right answer for a daily send and useless for a Monday roll-up: the
    job already ran on Tuesday through Sunday and said nothing to a
    weekly recipient. bill_change_events is the app's own append-only
    record of those days, so the roll-up reads back from it rather than
    asking LegiScan to re-diff a week."""
    rows = conn.execute(
        """SELECT bill_id, change_type, summary, description, event_date
             FROM bill_change_events
            WHERE date(detected_at) >= date(?)
            ORDER BY bill_id, detected_at""",
        (since_date,),
    ).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["bill_id"], []).append({
            "change_type": row["change_type"],
            "summary": row["summary"],
            "description": row["description"],
            "event_date": row["event_date"],
        })
    return out


# ── The Capitol directory — legislators, their staff, and who owns
# which portfolio. Org-owned like clients (see the schema.sql note on
# why that scoping is a boundary here and not just a convenience). ──

def save_directory_import(conn, user_id, source_name, as_of, legislators):
    """Write one parsed import — see directory.build_records for the
    shape it takes.

    Replaces the firm's directory rather than merging into it. A sheet
    is a snapshot of who works where on a date, and merging two
    snapshots produces a roster that never existed: the staffer who left
    in March survives forever because the June sheet simply doesn't
    mention them. Replacing means the directory always says exactly what
    one real sheet said, and the import row records which.

    The one thing carried across is the stale flags — those are a
    person's own reports about contacts they found wrong, not the
    sheet's content, and re-importing shouldn't quietly discard them.
    """
    stale = {
        (row["legislator"] or "", row["full_name"] or "")
        for row in conn.execute(
            f"""SELECT s.full_name, l.full_name AS legislator
                  FROM capitol_staff s
                  LEFT JOIN legislators l ON l.id = s.legislator_id
                 WHERE s.is_stale = 1 AND {_org_scope("s.user_id")}""",
            (user_id,),
        )
    }

    cur = conn.execute(
        """INSERT INTO directory_imports (user_id, source_name, as_of, created_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (user_id, source_name, as_of),
    )
    import_id = cur.lastrowid

    clear_directory(conn, user_id, keep_import_id=import_id)

    staff_count = 0
    for legislator in legislators:
        cur = conn.execute(
            """INSERT INTO legislators
                 (user_id, import_id, full_name, chamber, district, party,
                  office_room, office_phone, updated_at)
               VALUES (?,?,?,?,?,?,?,?, datetime('now'))""",
            (user_id, import_id, legislator["full_name"], legislator.get("chamber"),
             legislator.get("district"), legislator.get("party"),
             legislator.get("office_room"), legislator.get("office_phone")),
        )
        legislator_id = cur.lastrowid
        for staff in legislator.get("staff", []):
            cur = conn.execute(
                """INSERT INTO capitol_staff
                     (user_id, legislator_id, import_id, full_name, title, email,
                      phone, is_stale, updated_at)
                   VALUES (?,?,?,?,?,?,?,?, datetime('now'))""",
                (user_id, legislator_id, import_id, staff["full_name"],
                 staff.get("title"), staff.get("email"), staff.get("phone"),
                 1 if (legislator["full_name"], staff["full_name"]) in stale else 0),
            )
            staff_id = cur.lastrowid
            staff_count += 1
            for assignment in staff.get("assignments", []):
                conn.execute(
                    """INSERT OR IGNORE INTO staff_assignments
                         (user_id, staff_id, kind, name) VALUES (?,?,?,?)""",
                    (user_id, staff_id, assignment["kind"], assignment["name"]),
                )

    conn.execute(
        "UPDATE directory_imports SET legislators = ?, staff = ? WHERE id = ?",
        (len(legislators), staff_count, import_id),
    )
    return {"import_id": import_id, "legislators": len(legislators), "staff": staff_count}


def clear_directory(conn, user_id, keep_import_id=None):
    """Drop the firm's directory. Children first, since SQLite's foreign
    keys are on (see get_connection) and the parents are about to go."""
    conn.execute(
        f"""DELETE FROM staff_assignments WHERE {ORG_SCOPE}""", (user_id,))
    conn.execute(
        f"""DELETE FROM capitol_staff WHERE {ORG_SCOPE}""", (user_id,))
    conn.execute(
        f"""DELETE FROM legislators WHERE {ORG_SCOPE}""", (user_id,))
    if keep_import_id is None:
        conn.execute(f"DELETE FROM directory_imports WHERE {ORG_SCOPE}", (user_id,))
    else:
        conn.execute(
            f"DELETE FROM directory_imports WHERE {ORG_SCOPE} AND id != ?",
            (user_id, keep_import_id),
        )


def latest_directory_import(conn, user_id):
    row = conn.execute(
        f"""SELECT id, source_name, as_of, legislators, staff, created_at
              FROM directory_imports WHERE {ORG_SCOPE}
             ORDER BY id DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def search_directory(conn, user_id, query=None, chamber=None, limit=400):
    """The directory, filtered by one search box.

    `query` matches a legislator, a staffer, or an assignment — one box
    rather than three, because "water" and "Wicks" and "Ramirez" are the
    same question asked three ways ("who do I call about this") and
    making the user pick which field they're searching first is asking
    them to know the answer.

    Returns legislators with their staff nested, since that is how the
    page renders and how the user thinks: an office, then the people in
    it.
    """
    where = [_org_scope("l.user_id")]
    params = [user_id]
    if chamber:
        where.append("l.chamber = ?")
        params.append(chamber)
    if query:
        like = f"%{query.strip()}%"
        where.append(
            """(l.full_name LIKE ? OR l.district LIKE ?
                OR EXISTS (SELECT 1 FROM capitol_staff s2
                            WHERE s2.legislator_id = l.id
                              AND (s2.full_name LIKE ? OR s2.title LIKE ? OR s2.email LIKE ?))
                OR EXISTS (SELECT 1 FROM capitol_staff s3
                            JOIN staff_assignments a3 ON a3.staff_id = s3.id
                            WHERE s3.legislator_id = l.id AND a3.name LIKE ?))"""
        )
        params.extend([like] * 6)
    params.append(limit)

    legislators = [dict(row) for row in conn.execute(
        f"""SELECT l.id, l.full_name, l.chamber, l.district, l.party,
                   l.office_room, l.office_phone
              FROM legislators l
             WHERE {' AND '.join(where)}
             ORDER BY l.full_name
             LIMIT ?""",
        params,
    )]
    if not legislators:
        return []

    ids = [row["id"] for row in legislators]
    placeholders = ",".join("?" for _ in ids)
    staff_by_legislator = {}
    for row in conn.execute(
        f"""SELECT id, legislator_id, full_name, title, email, phone, is_stale
              FROM capitol_staff WHERE legislator_id IN ({placeholders})
             ORDER BY full_name""",
        ids,
    ):
        staff_by_legislator.setdefault(row["legislator_id"], []).append({
            "id": row["id"], "full_name": row["full_name"], "title": row["title"],
            "email": row["email"], "phone": row["phone"],
            "is_stale": bool(row["is_stale"]), "assignments": [],
        })

    staff_ids = [s["id"] for group in staff_by_legislator.values() for s in group]
    if staff_ids:
        by_id = {s["id"]: s for group in staff_by_legislator.values() for s in group}
        placeholders = ",".join("?" for _ in staff_ids)
        for row in conn.execute(
            f"""SELECT staff_id, kind, name FROM staff_assignments
                 WHERE staff_id IN ({placeholders}) ORDER BY kind, name""",
            staff_ids,
        ):
            by_id[row["staff_id"]]["assignments"].append(
                {"kind": row["kind"], "name": row["name"]})

    for legislator in legislators:
        staff = staff_by_legislator.get(legislator["id"], [])
        # Mark which staff the query actually hit, and put them first.
        # The office is worth showing whole — you want to see who else is
        # in it — but a search for "water" that lists four names with
        # nothing to say which one handles water has buried its own
        # answer in context.
        if query:
            needle = query.strip().lower()
            for person in staff:
                person["matched"] = _staff_matches(person, needle)
            staff.sort(key=lambda p: (not p["matched"], p["full_name"]))
        legislator["staff"] = staff
    return legislators


def _staff_matches(person, needle):
    haystack = [person.get("full_name"), person.get("title"), person.get("email")]
    haystack += [a["name"] for a in person.get("assignments", [])]
    return any(needle in (value or "").lower() for value in haystack)


def set_staff_stale(conn, user_id, staff_id, is_stale):
    """Flag a contact as out of date. US-I4's whole point: a static
    sheet nobody owns goes wrong silently, and the fix is letting the
    person who just found it wrong say so."""
    conn.execute(
        f"UPDATE capitol_staff SET is_stale = ?, updated_at = datetime('now')"
        f" WHERE id = ? AND {ORG_SCOPE}",
        (1 if is_stale else 0, staff_id, user_id),
    )


def update_staff(conn, user_id, staff_id, fields):
    """Correct a contact in place. The other half of US-I4 — flagging a
    row as wrong is only useful if the person who knows the right answer
    can also type it in."""
    allowed = ("full_name", "title", "email", "phone", "notes")
    sets = {k: v for k, v in (fields or {}).items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(
        f"UPDATE capitol_staff SET {assignments}, is_stale = 0,"
        f" updated_at = datetime('now') WHERE id = ? AND {ORG_SCOPE}",
        (*sets.values(), staff_id, user_id),
    )


def directory_stats(conn, user_id):
    row = conn.execute(
        f"""SELECT (SELECT COUNT(*) FROM legislators WHERE {ORG_SCOPE}) AS legislators,
                   (SELECT COUNT(*) FROM capitol_staff WHERE {ORG_SCOPE}) AS staff,
                   (SELECT COUNT(*) FROM capitol_staff WHERE {ORG_SCOPE} AND is_stale = 1) AS stale""",
        (user_id, user_id, user_id),
    ).fetchone()
    return dict(row)


# ── Clients — one-to-many with a user, unlike flagged_bills (many-to-
# many) or lobbyist_profiles (one-to-one). No cross-checking against
# lobbying_entities yet — existing_filer_id is stored for that future
# use, not acted on here. ──

# ── What the firm is paid, and on what basis (P2-26) ────────────────────
#
# The quarterly forms report a compensation figure per client per period,
# and the client record was the one place that number could live without
# being re-typed into every filing. See the clients table in schema.sql
# for why this is two columns rather than one string, and why no
# quarterly arithmetic lives here.

COMPENSATION_PERIODS = ("monthly", "quarterly", "annual", "hourly", "other")

# Accepts what a person types into a money field: an optional $, digits
# with optional thousands separators, an optional two-or-fewer decimal
# places. Deliberately does NOT accept a range or a formula — "5000-7500"
# is a note, not an amount a filing can report, and storing it here would
# put it somewhere a form will later read as a number.
_MONEY_RE = re.compile(r"^\$?\s*\d{1,3}(,\d{3})*(\.\d{1,2})?$|^\$?\s*\d+(\.\d{1,2})?$")


def normalize_compensation(amount, period):
    """Returns (amount, period, error) with the amount as a plain decimal
    string — no currency symbol, no separators — so it can be summed
    later without re-parsing, and the period as one of
    COMPENSATION_PERIODS or None.

    Blank is always allowed: not every client relationship has a figure
    agreed, and a client created before this field existed has none.
    Blanking the amount blanks the period too — a basis with no number
    is not a fact about anything."""
    amount = (amount or "").strip()
    period = (period or "").strip().lower() or None

    if not amount:
        return None, None, None
    if not _MONEY_RE.match(amount):
        return None, None, ("Compensation should be an amount like 5000 or 5,000.00 — "
                            "put a range or a formula in the notes instead.")
    cleaned = amount.lstrip("$").strip().replace(",", "")
    if period and period not in COMPENSATION_PERIODS:
        return None, None, "Choose how often that compensation applies."
    # An amount with no basis defaults to monthly, which is what a
    # retainer nearly always is — stated here rather than left NULL so a
    # form reading this column never has to guess.
    return cleaned, period or "monthly", None


CLIENT_FIELDS = (
    "name", "bus_addr1", "bus_city", "bus_st", "bus_zip4", "bus_phone",
    "interests", "existing_filer_id", "effective_date", "contract_period",
    "agencies_lobbied", "compensation_amount", "compensation_period", "notes",
)


def _client_values(fields):
    """The write tuple for create/update, in CLIENT_FIELDS order.

    One builder for both, because they drifted: update_client() grew the
    three Form 601 columns and create_client() had to be edited to match,
    and a column added to one and not the other is a field that silently
    can't be set at creation (or silently can't be edited afterwards).

    Compensation is normalized here rather than at the route, so a value
    written by a test or a future importer can't skip the check that a
    form will later depend on."""
    amount, period, error = normalize_compensation(
        fields.get("compensation_amount"), fields.get("compensation_period"))
    if error:
        raise ValueError(error)
    values = {key: (fields.get(key) or None) for key in CLIENT_FIELDS}
    values["name"] = fields.get("name")          # NOT NULL — the caller validates
    values["compensation_amount"] = amount
    values["compensation_period"] = period
    return tuple(values[key] for key in CLIENT_FIELDS)


# ── The people at a client ──────────────────────────────────────────────

def list_client_contacts(conn, user_id, client_id):
    rows = conn.execute(
        f"""SELECT id, client_id, name, title, email, phone, is_primary, created_at
             FROM client_contacts
            WHERE {ORG_SCOPE} AND client_id = ?
            ORDER BY is_primary DESC, name COLLATE NOCASE""",
        (user_id, client_id),
    ).fetchall()
    return [dict(r) for r in rows]


def add_client_contact(conn, user_id, client_id, fields):
    """Raises ValueError on a nameless contact — a row with a title and
    an email and nobody's name is not a person anyone can call."""
    name = (fields.get("name") or "").strip()
    if not name:
        raise ValueError("A contact needs a name.")
    if not get_client(conn, user_id, client_id):
        raise ValueError("No client with that id.")
    is_primary = 1 if fields.get("is_primary") else 0
    if is_primary:
        conn.execute("UPDATE client_contacts SET is_primary = 0 WHERE client_id = ?", (client_id,))
    conn.execute(
        """INSERT INTO client_contacts
             (user_id, client_id, name, title, email, phone, is_primary, created_at)
           VALUES (?,?,?,?,?,?,?, datetime('now'))""",
        (user_id, client_id, name, (fields.get("title") or "").strip() or None,
         (fields.get("email") or "").strip() or None,
         (fields.get("phone") or "").strip() or None, is_primary),
    )
    conn.commit()
    return list_client_contacts(conn, user_id, client_id)


def set_primary_contact(conn, user_id, client_id, contact_id):
    """At most one primary per client, enforced here rather than by a
    constraint — SQLite can't express "at most one row per client with
    this flag". Clearing first and setting second means the two writes
    can't leave two primaries behind even if the second matches nothing."""
    if not get_client(conn, user_id, client_id):
        raise ValueError("No client with that id.")
    conn.execute("UPDATE client_contacts SET is_primary = 0 WHERE client_id = ?", (client_id,))
    conn.execute(
        f"UPDATE client_contacts SET is_primary = 1 WHERE id = ? AND client_id = ? AND {ORG_SCOPE}",
        (contact_id, client_id, user_id),
    )
    conn.commit()
    return list_client_contacts(conn, user_id, client_id)


def delete_client_contact(conn, user_id, client_id, contact_id):
    cur = conn.execute(
        f"DELETE FROM client_contacts WHERE id = ? AND client_id = ? AND {ORG_SCOPE}",
        (contact_id, client_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_client_notes(conn, user_id, client_id, notes):
    """The client-level equivalent of set_bill_notes. Empty string stores
    NULL so "never written" and "cleared" read the same downstream."""
    cur = conn.execute(
        f"UPDATE clients SET notes = ? WHERE id = ? AND {ORG_SCOPE}",
        ((notes or "").strip() or None, client_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_hearings_for_client(conn, user_id, client_id, today=None):
    """Every still-to-come hearing across the bills this client is
    assigned to, soonest first.

    The client-scoped cut of list_hearings_for_flagged_bills: same
    "still upcoming" test against the same California date (see
    today_in_california), narrowed to one client's bills. On the client
    record this is the answer to "what is coming up for them", which
    previously required reading the bill table row by row."""
    today = today or today_in_california()
    rows = conn.execute(
        f"""SELECT h.bill_id, h.date, h.time, h.event_type, h.location, h.description,
                  b.state, b.bill_number, b.title, l.position
             FROM bill_hearings h
             JOIN bill_client_links l ON l.bill_id = h.bill_id AND {_org_scope("l.user_id")}
             JOIN bills b ON b.id = h.bill_id
            WHERE l.client_id = ? AND h.date >= ?
            ORDER BY h.date, h.time""",
        (user_id, client_id, today),
    ).fetchall()
    out = []
    for row in rows:
        hearing = dict(row)
        hearing["days_until"] = _days_between(today, hearing["date"])
        out.append(hearing)
    return out


def create_client(conn, user_id, fields):
    columns = ", ".join(CLIENT_FIELDS)
    placeholders = ", ".join("?" for _ in CLIENT_FIELDS)
    cur = conn.execute(
        f"""INSERT INTO clients (user_id, {columns}, created_at)
            VALUES (?, {placeholders}, datetime('now'))""",
        (user_id, *_client_values(fields)),
    )
    return cur.lastrowid


def list_clients(conn, user_id):
    rows = conn.execute(
        f"SELECT * FROM clients WHERE {ORG_SCOPE} ORDER BY name", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_client(conn, user_id, client_id):
    """Scoped to user_id, same reasoning as delete_client — one account
    can't view another's client just by guessing/incrementing an id."""
    row = conn.execute(
        f"SELECT * FROM clients WHERE id = ? AND {ORG_SCOPE}", (client_id, user_id)
    ).fetchone()
    return dict(row) if row else None


def get_client_bills(conn, user_id, client_id):
    """Every bill this client is currently linked to, with its own
    position — the reverse direction of clients_for_bills() above (that
    one goes bill -> clients; this one goes client -> bills), for the
    client detail page."""
    rows = conn.execute(
        f"""SELECT b.id AS bill_id, b.state, b.bill_number, b.title,
                  b.status_label, b.status_date, b.url, l.position, l.effective_date
           FROM bill_client_links l
           JOIN bills b ON b.id = l.bill_id
           WHERE {_org_scope("l.user_id")} AND l.client_id = ?
           ORDER BY b.bill_number""",
        (user_id, client_id),
    ).fetchall()
    return [dict(r) for r in rows]


def update_client(conn, user_id, client_id, fields):
    """Scoped to user_id, same reasoning as delete_client. Exists so a
    client created before a column did (or before the user had that
    information handy) can still have it filled in later — without this,
    every field could only ever be set at creation time, leaving every
    already-existing client permanently gapped.

    Writes CLIENT_FIELDS through the same builder create_client uses, so
    a column added to the record can't end up settable at creation and
    not afterwards, or the reverse."""
    assignments = ", ".join(f"{key} = ?" for key in CLIENT_FIELDS)
    conn.execute(
        f"UPDATE clients SET {assignments} WHERE id = ? AND {ORG_SCOPE}",
        (*_client_values(fields), client_id, user_id),
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
    conn.execute(f"DELETE FROM bill_client_links WHERE client_id = ? AND {ORG_SCOPE}", (client_id, user_id))
    # A saved search pointing at this client keeps running — it's the
    # user's query, and only the auto-assign target goes with the client.
    conn.execute(
        f"UPDATE saved_searches SET client_id = NULL WHERE client_id = ? AND {ORG_SCOPE}",
        (client_id, user_id),
    )
    conn.execute(f"DELETE FROM clients WHERE id = ? AND {ORG_SCOPE}", (client_id, user_id))



# ── The firm's lobbyists — Form 601's Part I. Separate from users
# because a firm registers lobbyists who may not have a login here, and
# an account holder (an assistant, an associate) isn't necessarily a
# registered lobbyist. See org_lobbyists in schema.sql. ──

def list_org_lobbyists(conn, user_id):
    """The roster for this user's firm, in the order it was entered —
    which is the order it will appear on the form, and therefore an order
    the user can reason about."""
    rows = conn.execute(
        """SELECT l.id, l.name, l.cert_id, l.user_id, l.created_at
           FROM org_lobbyists l
           WHERE l.org_id = (SELECT org_id FROM users WHERE id = ?)
           ORDER BY l.id""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_org_lobbyist(conn, user_id, name, cert_id=None):
    """Raises ValueError (safe to show the user) on a blank name or an
    account with no organization — the latter can't happen after
    _backfill_organizations, and failing loudly beats writing a roster
    row nobody can ever read back."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter the lobbyist's name.")
    org_id = org_id_for_user(conn, user_id)
    if not org_id:
        raise ValueError("This account isn't attached to a firm yet.")
    cur = conn.execute(
        """INSERT INTO org_lobbyists (org_id, name, cert_id, created_at)
           VALUES (?,?,?,datetime('now'))""",
        (org_id, name, (cert_id or "").strip() or None),
    )
    return cur.lastrowid


def delete_org_lobbyist(conn, user_id, lobbyist_id):
    cur = conn.execute(
        """DELETE FROM org_lobbyists
           WHERE id = ? AND org_id = (SELECT org_id FROM users WHERE id = ?)""",
        (lobbyist_id, user_id),
    )
    return cur.rowcount > 0


# ── Saved searches — a query the daily job re-runs, reporting what's new
# since the last run. See the table comments in schema.sql for why this
# exists at all: without it, monitoring only ever covers bills someone
# has already flagged, and the bill that hurts a client is the one
# introduced last week that nobody has noticed. ──

# ── Saved views on the flagged list (P2-24) ─────────────────────────────
#
# A view is a named filter composition, stored as the page's own query
# string. See saved_views in schema.sql for why that is a text column
# rather than one column per dimension, and why it is the firm's rather
# than the individual's.

MAX_SAVED_VIEWS = 30


def create_saved_view(conn, user_id, name, query):
    """Save the current filter composition under a name, or replace the
    query on a view of that name the firm already has.

    Replace rather than reject on a name collision: "UCSA — Thursday
    call" is a standing view whose filters get adjusted, and making the
    user delete it first to re-save it is a worse answer than the
    UNIQUE constraint's error message."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the view a name.")
    if len(name) > 60:
        raise ValueError("That name is too long — 60 characters at most.")
    # The query string as the page composed it, minus its leading '?'.
    query = (query or "").lstrip("?").strip()

    existing = conn.execute(
        f"SELECT id FROM saved_views WHERE {ORG_SCOPE} AND name = ?", (user_id, name)
    ).fetchone()
    if existing:
        conn.execute("UPDATE saved_views SET query = ? WHERE id = ?", (query, existing["id"]))
        conn.commit()
        return list_saved_views(conn, user_id)

    count = conn.execute(
        f"SELECT COUNT(*) AS n FROM saved_views WHERE {ORG_SCOPE}", (user_id,)
    ).fetchone()["n"]
    if count >= MAX_SAVED_VIEWS:
        raise ValueError(f"At most {MAX_SAVED_VIEWS} saved views.")

    conn.execute(
        """INSERT INTO saved_views (user_id, name, query, created_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (user_id, name, query),
    )
    conn.commit()
    return list_saved_views(conn, user_id)


def list_saved_views(conn, user_id):
    rows = conn.execute(
        f"""SELECT id, name, query, created_at FROM saved_views
             WHERE {ORG_SCOPE} ORDER BY name COLLATE NOCASE""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_saved_view(conn, user_id, view_id):
    cur = conn.execute(
        f"DELETE FROM saved_views WHERE {ORG_SCOPE} AND id = ?", (user_id, view_id)
    )
    conn.commit()
    return cur.rowcount > 0


def create_saved_search(conn, user_id, name, query, client_id=None):
    """Raises ValueError (safe to show the user) on a blank name/query,
    a client that isn't theirs, or a name they've already used."""
    name = (name or "").strip()
    query = (query or "").strip()
    if not name:
        raise ValueError("Give this search a name.")
    if not query:
        raise ValueError("There's no search to save.")
    if client_id:
        owns = conn.execute(
            f"SELECT 1 FROM clients WHERE id = ? AND {ORG_SCOPE}", (client_id, user_id)
        ).fetchone()
        if not owns:
            raise ValueError("That client doesn't belong to your account.")
    existing = conn.execute(
        f"SELECT id FROM saved_searches WHERE {ORG_SCOPE} AND name = ?", (user_id, name)
    ).fetchone()
    if existing:
        raise ValueError(f"You already have a saved search called \u201c{name}\u201d.")
    cur = conn.execute(
        """INSERT INTO saved_searches (user_id, name, query, client_id, created_at)
           VALUES (?,?,?,?,datetime('now'))""",
        (user_id, name, query, client_id or None),
    )
    return cur.lastrowid


def delete_saved_search(conn, user_id, saved_search_id):
    """The recorded matches go with it. They only exist to answer "what's
    new for this search", so they mean nothing once the search is gone —
    unlike position_history, which is a record of what the firm did."""
    owns = conn.execute(
        f"SELECT 1 FROM saved_searches WHERE id = ? AND {ORG_SCOPE}", (saved_search_id, user_id)
    ).fetchone()
    if not owns:
        return False
    conn.execute("DELETE FROM saved_search_matches WHERE saved_search_id = ?", (saved_search_id,))
    conn.execute(f"DELETE FROM saved_searches WHERE id = ? AND {ORG_SCOPE}", (saved_search_id, user_id))
    return True


def list_saved_searches(conn, user_id):
    """This user's saved searches with the client's name resolved and a
    count of matches found since they last looked — what the search page
    lists, and what makes a saved search worth opening."""
    rows = conn.execute(
        f"""SELECT s.id, s.name, s.query, s.client_id, s.created_at, s.last_run_at,
                  c.name AS client_name,
                  (SELECT COUNT(*) FROM saved_search_matches m
                    WHERE m.saved_search_id = s.id AND m.reported = 0) AS new_match_count
           FROM saved_searches s
           LEFT JOIN clients c ON c.id = s.client_id
           WHERE {_org_scope("s.user_id")}
           ORDER BY s.name""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_saved_searches_for_run(conn):
    """Every saved search across every account, for the daily job. Not
    user-scoped on purpose — this is the one caller that runs on nobody's
    behalf in particular."""
    return [dict(r) for r in conn.execute(
        "SELECT id, user_id, name, query, client_id FROM saved_searches ORDER BY id"
    )]


def record_saved_search_matches(conn, saved_search_id, rows, seen_at=None):
    """Store this run's results and return only the ones never seen
    before, newest run first.

    The whole point of the table is this diff. A saved search for
    "artificial intelligence" matches 119 bills every morning; what the
    user needs to hear about is the one that wasn't there yesterday.

    INSERT OR IGNORE against the UNIQUE(saved_search_id, bill_id) rather
    than a SELECT-then-INSERT: two runs racing (the local launchd job and
    a hosted cron hitting the same disk) would otherwise both decide a
    bill was new and report it twice."""
    seen_at = seen_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_matches = []
    for row in rows:
        bill_id = row.get("bill_id")
        if not bill_id:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO saved_search_matches
                 (saved_search_id, bill_id, bill_number, title, last_action, first_seen_at)
               VALUES (?,?,?,?,?,?)""",
            (saved_search_id, bill_id, row.get("bill_number"), row.get("title"),
             row.get("last_action"), seen_at),
        )
        if cur.rowcount:
            new_matches.append({
                "bill_id": bill_id,
                "bill_number": row.get("bill_number"),
                "title": row.get("title"),
                "last_action": row.get("last_action"),
            })
    conn.execute(
        "UPDATE saved_searches SET last_run_at = ? WHERE id = ?", (seen_at, saved_search_id)
    )
    return new_matches


def list_unreported_matches(conn, user_id):
    """New matches this user hasn't been told about yet, grouped by saved
    search — what the digest email reports and what the search page
    counts. Ordered oldest-first within a search so a digest reads in the
    order things appeared."""
    rows = conn.execute(
        f"""SELECT m.id, m.saved_search_id, m.bill_id, m.bill_number, m.title,
                  m.last_action, m.first_seen_at, s.name AS search_name, s.client_id,
                  c.name AS client_name
           FROM saved_search_matches m
           JOIN saved_searches s ON s.id = m.saved_search_id
           LEFT JOIN clients c ON c.id = s.client_id
           WHERE {_org_scope("s.user_id")} AND m.reported = 0
           ORDER BY s.name, m.first_seen_at, m.id""",
        (user_id,),
    ).fetchall()
    grouped = {}
    for row in rows:
        entry = grouped.setdefault(row["saved_search_id"], {
            "saved_search_id": row["saved_search_id"],
            "name": row["search_name"],
            "client_name": row["client_name"],
            "matches": [],
        })
        entry["matches"].append({
            "id": row["id"], "bill_id": row["bill_id"], "bill_number": row["bill_number"],
            "title": row["title"], "last_action": row["last_action"],
            "first_seen_at": row["first_seen_at"],
        })
    return list(grouped.values())


def mark_matches_reported(conn, match_ids):
    """Flipped only after a digest has actually gone out, so an SMTP
    outage postpones the news rather than losing it."""
    if not match_ids:
        return 0
    placeholders = ",".join("?" for _ in match_ids)
    cur = conn.execute(
        f"UPDATE saved_search_matches SET reported = 1 WHERE id IN ({placeholders})",
        tuple(match_ids),
    )
    return cur.rowcount


def mark_search_seen(conn, user_id, saved_search_id):
    """The in-app equivalent of a digest going out: opening a saved
    search is seeing its new matches, so the count clears."""
    cur = conn.execute(
        f"""UPDATE saved_search_matches SET reported = 1
           WHERE saved_search_id = (
             SELECT id FROM saved_searches WHERE id = ? AND {ORG_SCOPE}
           )""",
        (saved_search_id, user_id),
    )
    return cur.rowcount



# ── Bill-to-client links — many-to-many, since a bill can matter to
# more than one client. ──

VALID_POSITIONS = ("support", "oppose", "watch")


def link_bill_to_client(conn, user_id, bill_id, client_id, position="watch",
                        effective_date=None):
    """Raises ValueError (safe to show the user) if the client isn't
    actually theirs, the bill isn't actually one they've flagged, or
    position isn't one of the three allowed values — all checked
    explicitly rather than trusted from the request, since a bare
    foreign key can't express "belongs to the same user".

    Doubles as the "change position later" path: called again for a
    link that already exists, it updates position on the existing row
    instead of leaving it untouched — same endpoint handles both
    assigning a client to a bill and changing its stance afterward.

    Every call that actually changes something also appends to
    position_history. A re-save of the same position with the same
    effective date appends nothing: the user picked the value that was
    already there, and a log that records non-events is a log nobody
    reads.

    effective_date defaults to today in California — the position is in
    force from the day it was set unless the user says otherwise. See the
    column comment in schema.sql for why the two dates are separate."""
    if position not in VALID_POSITIONS:
        raise ValueError("Position must be support, oppose, or watch.")
    owns_client = conn.execute(
        f"SELECT 1 FROM clients WHERE id = ? AND {ORG_SCOPE}", (client_id, user_id)
    ).fetchone()
    if not owns_client:
        raise ValueError("That client doesn't belong to your account.")
    has_flagged = conn.execute(
        f"SELECT 1 FROM flagged_bills WHERE {ORG_SCOPE} AND archived_at IS NULL AND bill_id = ?",
        (user_id, bill_id),
    ).fetchone()
    if not has_flagged:
        raise ValueError("Flag this bill before assigning it to a client.")

    existing = conn.execute(
        f"""SELECT position, effective_date FROM bill_client_links
           WHERE {ORG_SCOPE} AND bill_id = ? AND client_id = ?""",
        (user_id, bill_id, client_id),
    ).fetchone()
    # An existing link keeps its effective date unless the caller sends a
    # new one; a brand-new one starts today. Changing the position
    # without saying otherwise moves the date with it — the new stance
    # took effect when it was taken, not when the client was first added.
    if effective_date is None:
        if existing is None or existing["position"] != position:
            effective_date = today_in_california()
        else:
            effective_date = existing["effective_date"]

    conn.execute(
        """INSERT INTO bill_client_links
             (user_id, bill_id, client_id, position, effective_date, linked_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(user_id, bill_id, client_id) DO UPDATE SET
             position=excluded.position, effective_date=excluded.effective_date""",
        (user_id, bill_id, client_id, position, effective_date),
    )

    if existing is not None and existing["position"] == position \
            and existing["effective_date"] == effective_date:
        return
    record_position_change(
        conn, user_id, bill_id, client_id,
        from_position=existing["position"] if existing else None,
        to_position=position,
        effective_date=effective_date,
    )


def unlink_bill_from_client(conn, user_id, bill_id, client_id):
    """Take a client off a bill. The link row goes; the history of it
    doesn't — a removal is recorded with to_position NULL, since "we
    stopped holding a position in September" is exactly the kind of thing
    someone later has to account for."""
    existing = conn.execute(
        f"""SELECT position FROM bill_client_links
           WHERE {ORG_SCOPE} AND bill_id = ? AND client_id = ?""",
        (user_id, bill_id, client_id),
    ).fetchone()
    conn.execute(
        f"DELETE FROM bill_client_links WHERE {ORG_SCOPE} AND bill_id = ? AND client_id = ?",
        (user_id, bill_id, client_id),
    )
    if existing is not None:
        record_position_change(
            conn, user_id, bill_id, client_id,
            from_position=existing["position"], to_position=None,
            effective_date=today_in_california(),
        )


def record_position_change(conn, user_id, bill_id, client_id, from_position,
                           to_position, effective_date=None, changed_at=None):
    """Append one row to position_history. Append-only by construction —
    nothing in this module updates or deletes from that table."""
    # The name as it read at the time, alongside the id — see the column
    # comment in schema.sql. A client deleted later leaves a record that
    # still says who it was about.
    name_row = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
    conn.execute(
        """INSERT INTO position_history
             (user_id, bill_id, client_id, client_name, from_position, to_position,
              effective_date, changed_at, changed_by)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            user_id, bill_id, client_id, name_row["name"] if name_row else None,
            from_position, to_position, effective_date,
            changed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            user_id,
        ),
    )


def list_position_history(conn, user_id, bill_id=None, client_id=None):
    """This user's position changes, newest first — for the panel on the
    bill report (one bill, every client) and on the client record (one
    client, every bill). At least one of bill_id/client_id is expected;
    with neither, this is the whole account's history, which is what the
    tests read and what a future export would want.

    Joins the names in rather than returning bare ids: every caller
    renders "Anthropic PBC on CA SB1159", and the alternative is three
    round trips per row. The client name falls back to the copy stored on
    the row itself when the client has since been deleted — the record of
    a position has to outlive the client it was held for."""
    where = [_org_scope("h.user_id")]
    params = [user_id]
    if bill_id is not None:
        where.append("h.bill_id = ?")
        params.append(bill_id)
    if client_id is not None:
        where.append("h.client_id = ?")
        params.append(client_id)
    rows = conn.execute(
        f"""SELECT h.id, h.bill_id, h.client_id, h.from_position, h.to_position,
                   h.effective_date, h.changed_at, h.changed_by,
                   COALESCE(c.name, h.client_name) AS client_name,
                   b.state, b.bill_number,
                   u.email AS changed_by_email
            FROM position_history h
            LEFT JOIN clients c ON c.id = h.client_id
            LEFT JOIN bills b ON b.id = h.bill_id
            LEFT JOIN users u ON u.id = h.changed_by
            WHERE {' AND '.join(where)}
            ORDER BY h.changed_at DESC, h.id DESC""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def clients_for_bills(conn, user_id, bill_ids):
    """bill_id -> [{id, name}, ...] for every bill in bill_ids, scoped
    to this user's own links and clients."""
    if not bill_ids:
        return {}
    placeholders = ",".join("?" * len(bill_ids))
    rows = conn.execute(
        f"""SELECT l.bill_id, c.id AS client_id, c.name, l.position, l.effective_date
            FROM bill_client_links l JOIN clients c ON c.id = l.client_id
            WHERE {_org_scope("l.user_id")} AND l.bill_id IN ({placeholders})""",
        (user_id, *bill_ids),
    ).fetchall()
    by_bill = {}
    for r in rows:
        by_bill.setdefault(r["bill_id"], []).append({
            "id": r["client_id"], "name": r["name"], "position": r["position"],
            "effective_date": r["effective_date"],
        })
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
    # Against California's date, not date('now')'s UTC — hosted on Render
    # the latter rolls over mid-afternoon Pacific and would drop a hearing
    # happening this afternoon out of "upcoming" while it's still ahead of
    # the user. Same cut the calendar makes (list_hearings_for_flagged_bills),
    # so the two screens can't disagree about what's still to come.
    result["upcoming_hearings"] = [
        dict(r) for r in conn.execute(
            """SELECT event_type, date, time, location, description
               FROM bill_hearings WHERE bill_id = ? AND date >= ?
               ORDER BY date, time""",
            (bill_id, today_in_california()),
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
    flag_row = conn.execute(
        f"SELECT notes, archived_at FROM flagged_bills WHERE {ORG_SCOPE} AND bill_id = ?",
        (user_id, bill_id),
    ).fetchone()
    result["flagged"] = flag_row is not None and flag_row["archived_at"] is None
    # A third state alongside flagged/not: archived (P1-16). The client
    # links and notes below are unaffected by which of the three this
    # is — archiving never touched them — so this only changes which
    # action button the report page shows.
    result["archived"] = flag_row is not None and flag_row["archived_at"] is not None
    # Empty string rather than None so the textarea binds cleanly, and
    # only ever this user's own note — see flagged_bills.notes.
    result["notes"] = (flag_row["notes"] if flag_row else None) or ""
    # Every position this user has ever held on this bill, for the panel
    # under the assignments. Newest first, and it outlives the
    # assignments themselves — a client removed from the bill still
    # appears here, which is the whole reason the table is append-only.
    result["position_history"] = list_position_history(conn, user_id, bill_id=bill_id)
    # Whether THIS person has muted digest mail about this bill. Their
    # own setting, not the firm's — see digest_mutes in schema.sql.
    result["digest_muted"] = bill_id in list_digest_muted_bill_ids(conn, user_id)
    return result


def set_bill_notes(conn, user_id, bill_id, notes):
    """Save this user's own note on a bill they've flagged.

    Scoped to a flag rather than to the bill: unflagging drops the note
    with the rest of that user's context, and a bill nobody has flagged
    has nowhere to keep one — the route treats that as an error rather
    than silently discarding what someone typed."""
    cur = conn.execute(
        f"UPDATE flagged_bills SET notes = ? WHERE {ORG_SCOPE} AND bill_id = ?",
        (notes or None, user_id, bill_id),
    )
    if cur.rowcount == 0:
        raise ValueError("Flag this bill before adding notes to it.")
    return notes or ""


def mark_bill_viewed(conn, user_id, bill_id, viewed_at=None):
    """Record that this PERSON has now looked at this bill, clearing its
    unread dot for them (see _unread_counts_for_bills).

    Per user, not per firm — and the one thing on the flagged list that
    still is. The flag says "we track this bill"; the view says "I have
    read it", and one lobbyist opening a bill must not clear the dot for
    the colleague who hasn't. Hence its own table (bill_views) rather
    than a column on the now org-owned flag.

    Stamped in UTC in the same 'YYYY-MM-DDTHH:MM:SSZ' shape
    record_bill_changes writes detected_at in — the two are compared as
    plain strings, so they have to agree on format and on zone. A
    California date would be wrong here for once: this isn't a deadline,
    it's an instant, and it's only ever measured against another instant
    recorded by the refresh job.

    Recorded whether or not the bill is flagged. Reading a bill nobody
    tracks is ordinary (a search result opens the same page), it costs
    one row, and it means the dot is already right if the firm flags that
    bill later in the same sitting."""
    viewed_at = viewed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO bill_views (user_id, bill_id, last_viewed_at) VALUES (?,?,?)
           ON CONFLICT(user_id, bill_id) DO UPDATE SET last_viewed_at = excluded.last_viewed_at""",
        (user_id, bill_id, viewed_at),
    )
    return True


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


# ── Position letters — see letter_drafts.py for what a new one is
# seeded from. Storage only here: this module knows a letter has a
# subject and a body, not how either is worded. ──

def create_letter(conn, user_id, fields):
    """Store a new letter. Everything but subject/body is context
    recorded for later reading — see the letters table comment in
    schema.sql for why the names are stored beside the ids."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        """INSERT INTO letters
             (user_id, bill_id, bill_label, client_id, client_name, position,
              subject, body, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            user_id, fields.get("bill_id"), fields.get("bill_label"),
            fields.get("client_id"), fields.get("client_name"), fields.get("position"),
            fields.get("subject") or "Untitled letter", fields.get("body") or "",
            now, now,
        ),
    )
    return cur.lastrowid


def update_letter(conn, user_id, letter_id, subject, body):
    """Only ever the two fields the user types into. The bill/client
    context is what the letter was written about and doesn't change
    because someone edited a paragraph."""
    cur = conn.execute(
        f"""UPDATE letters SET subject = ?, body = ?, updated_at = ?
           WHERE id = ? AND {ORG_SCOPE}""",
        (
            subject or "Untitled letter", body or "",
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            letter_id, user_id,
        ),
    )
    return cur.rowcount > 0


def get_letter(conn, user_id, letter_id):
    row = conn.execute(
        f"SELECT * FROM letters WHERE id = ? AND {ORG_SCOPE}", (letter_id, user_id)
    ).fetchone()
    return dict(row) if row else None


def list_letters(conn, user_id, bill_id=None, client_id=None):
    """Newest-edited first. Filtered by bill for the panel on the bill
    report, by client for the one on the client record, and unfiltered
    for Draft > Letters itself."""
    where = [ORG_SCOPE]
    params = [user_id]
    if bill_id is not None:
        where.append("bill_id = ?")
        params.append(bill_id)
    if client_id is not None:
        where.append("client_id = ?")
        params.append(client_id)
    rows = conn.execute(
        f"""SELECT id, bill_id, bill_label, client_id, client_name, position,
                   subject, created_at, updated_at
            FROM letters WHERE {' AND '.join(where)}
            ORDER BY updated_at DESC, id DESC""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_letter(conn, user_id, letter_id):
    cur = conn.execute(
        f"DELETE FROM letters WHERE id = ? AND {ORG_SCOPE}", (letter_id, user_id)
    )
    return cur.rowcount > 0


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
        f"SELECT * FROM prepared_filings WHERE id = ? AND {ORG_SCOPE}", (filing_id, user_id)
    ).fetchone()
    return _row_to_prepared_filing(row) if row else None


def list_prepared_filings(conn, user_id):
    rows = conn.execute(
        f"SELECT * FROM prepared_filings WHERE {ORG_SCOPE} ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    return [_row_to_prepared_filing(r) for r in rows]


def delete_prepared_filing(conn, user_id, filing_id):
    """Scoped to user_id, same reasoning as delete_client/unflag_bill —
    never trust a client-supplied ID alone for a per-user record. No
    cascade needed (unlike delete_client): nothing else references a
    prepared_filings row."""
    conn.execute(f"DELETE FROM prepared_filings WHERE id = ? AND {ORG_SCOPE}", (filing_id, user_id))


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
            f"""UPDATE prepared_filings
               SET field_data = ?, pdf_field_data_hash = NULL,
                   status = 'draft', signed_name = NULL, confirmed_accurate = 0,
                   signed_at = NULL, signed_by = NULL
               WHERE id = ? AND {ORG_SCOPE}""",
            (json.dumps(new_field_data), filing_id, user_id),
        )
    else:
        conn.execute(
            f"""UPDATE prepared_filings
               SET field_data = ?, client_row_ids = ?, pdf_field_data_hash = NULL,
                   status = 'draft', signed_name = NULL, confirmed_accurate = 0,
                   signed_at = NULL, signed_by = NULL
               WHERE id = ? AND {ORG_SCOPE}""",
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
        f"UPDATE prepared_filings SET trigger_date = ?, due_date = ? WHERE id = ? AND {ORG_SCOPE}",
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
        f"UPDATE prepared_filings SET pdf_field_data_hash = ? WHERE id = ? AND {ORG_SCOPE}",
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
        f"""UPDATE prepared_filings
           SET status = 'ready_to_file', signed_name = ?, confirmed_accurate = 1,
               signed_at = datetime('now'), signed_by = ?
           WHERE id = ? AND {ORG_SCOPE}""",
        # signed_by is the account; signed_name is what they typed. They
        # are usually the same person, and the point of recording both is
        # the case where they are not — a filing belongs to the firm now,
        # so "prepared by one, signed off by another" can happen.
        (signed_name, user_id, filing_id, user_id),
    )
    return get_prepared_filing(conn, user_id, filing_id)


# ── Dashboard — the signed-in landing page's single read. ──
#
# Pure composition of readers that already exist above (flagged bills,
# hearings, prepared filings, clients) plus one new query for the
# change feed. No new tables, no LegiScan call: everything here is
# already in the database because the daily refresh job put it there.
# The composition lives in Python rather than in the page's JS because
# every deadline on this page has to be counted from California's date
# (today_in_california), same reason _next_hearings_for_bills already
# precomputes days_until instead of letting the browser subtract.

# How far ahead the "hearings coming up" tile looks. Two weeks rather
# than one: a lobbyist's preparation window for a committee hearing is
# longer than the "Hearing this week" filter /flagged already offers,
# and a tile that only ever said 0 or 1 wouldn't be worth a quarter of
# the row.
HEARING_HORIZON_DAYS = 14

# A filing inside this many days is worth interrupting the user about.
# Matches the .due-chip.soon threshold the disclosures list already uses.
FILING_SOON_DAYS = 14


def recent_bill_changes(conn, user_id, limit=8, since=None):
    """The newest changes the refresh job recorded across this user's
    flagged bills, newest first — the dashboard's activity feed, and
    (with `since`) the search page's "bills that moved this week".

    Unlike _latest_changes_for_bills (one row per bill, for a table
    column), this is a flat chronological feed across all of them: the
    same bill can appear twice if it moved twice, because "what has
    happened lately" is the question here, not "where does each bill
    stand". Empty on a database that predates bill_change_events, or
    before the first refresh run finds anything move.

    `since` is an ISO date, inclusive. Note what it can and cannot mean:
    the refresh job only visits bills somebody flagged, so "moved this
    week" is always "moved among the bills we watch" and never a claim
    about the Legislature at large. The screen says so in those words
    rather than implying a completeness this data doesn't have."""
    clause = "AND date(c.detected_at) >= date(?)" if since else ""
    params = [user_id] + ([since] if since else []) + [limit]
    rows = conn.execute(
        f"""SELECT c.bill_id, c.detected_at, c.change_type, c.summary, c.description,
                  c.event_date, b.state, b.bill_number, b.title
           FROM bill_change_events c
           JOIN flagged_bills f ON f.bill_id = c.bill_id
              AND {_org_scope("f.user_id")} AND f.archived_at IS NULL
           JOIN bills b ON b.id = c.bill_id
           WHERE 1=1 {clause}
           ORDER BY c.detected_at DESC, c.id DESC
           LIMIT ?""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def days_ago_in_california(days):
    """An ISO date `days` before today in Sacramento — the left edge of a
    "this week" window, measured on the same clock every other date cut
    in this app uses (see today_in_california)."""
    return (datetime.strptime(today_in_california(), "%Y-%m-%d")
            - timedelta(days=days)).strftime("%Y-%m-%d")


def _attention_items(flagged, filings, today):
    """The one merged queue the dashboard leads with: everything with a
    deadline attached, from whichever part of the app it came from,
    ordered by how soon it bites.

    The point of merging is that these three kinds of work are only ever
    urgent relative to each other, and until now lived on three separate
    pages — a filing overdue by two days and a hearing on Thursday could
    not be compared without opening both. `days` is the single sort key
    that makes them comparable; a bill with no client assigned has no
    date at all, so it sorts last (None → +inf) and appears as cleanup
    once the dated work is clear rather than as an interruption."""
    items = []

    for filing in filings:
        days = filing.get("days_until_due")
        if filing["status"] != "draft" or days is None or days > FILING_SOON_DAYS:
            continue
        label = f"Form {filing['form_type']}"
        if filing.get("period_label"):
            label += f" — {filing['period_label']}"
        items.append({
            "kind": "filing",
            "days": days,
            "title": label,
            "detail": "Prepared filing, not yet signed off",
            "href": f"/disclosures/review?id={filing['id']}",
        })

    for bill in flagged:
        hearing = bill.get("next_hearing")
        if hearing and hearing.get("days_until") is not None and hearing["days_until"] <= 7:
            items.append({
                "kind": "hearing",
                "days": hearing["days_until"],
                "title": f"{bill['state']} {bill['bill_number']}",
                "detail": hearing.get("description") or hearing.get("event_type") or "Hearing scheduled",
                "date": hearing.get("date"),
                "time": hearing.get("time"),
                "location": hearing.get("location"),
                "href": f"/report?bill_id={bill['bill_id']}",
            })
        if not bill.get("assigned_clients"):
            items.append({
                "kind": "unassigned",
                "days": None,
                "title": f"{bill['state']} {bill['bill_number']}",
                "detail": "No client assigned",
                "href": f"/report?bill_id={bill['bill_id']}",
            })

    items.sort(key=lambda i: (i["days"] is None, i["days"] if i["days"] is not None else 0))
    return items


def _client_rollup(flagged, clients):
    """Per-client counts of this user's flagged bills, split by the
    position they took on each — plus a synthetic "Unassigned" entry.

    Built from the assigned_clients already attached to each flagged
    bill rather than by re-querying bill_client_links, so the rollup can
    never disagree with the list the same page is showing. Every client
    appears, including ones with no bills yet: a client the firm has
    signed but hasn't linked to anything is exactly the gap this is
    meant to make visible."""
    rollup = {
        c["id"]: {"client_id": c["id"], "name": c["name"],
                  "support": 0, "oppose": 0, "watch": 0, "total": 0}
        for c in clients
    }
    unassigned = 0
    for bill in flagged:
        assigned = bill.get("assigned_clients") or []
        if not assigned:
            unassigned += 1
            continue
        for link in assigned:
            entry = rollup.get(link["id"])
            if not entry:
                continue
            position = link.get("position") or "watch"
            if position in ("support", "oppose", "watch"):
                entry[position] += 1
            entry["total"] += 1

    rows = sorted(rollup.values(), key=lambda r: (-r["total"], r["name"].lower()))
    return {"clients": rows, "unassigned": unassigned}


def dashboard_summary(conn, user_id, today=None):
    """Everything the dashboard renders, in one call.

    One endpoint rather than the page firing four fetches at four
    existing APIs: the tiles, the attention queue and the client rollup
    are each derived from more than one of those sources, so splitting
    them client-side would mean the page can't draw anything until all
    four land, and would give three different answers if a refresh ran
    between them."""
    today = today or today_in_california()
    flagged = list_flagged_bills(conn, user_id, today)
    hearings = list_hearings_for_flagged_bills(conn, user_id, today)
    filings = list_prepared_filings(conn, user_id)
    clients = list_clients(conn, user_id)

    # _row_to_prepared_filing counts days_until_due from
    # today_in_california() directly, with no way to pass a date in — fine
    # on the disclosures list, which has only that one kind of deadline,
    # but here it would mean the filing countdowns and the hearing
    # countdowns were measured from two different clocks whenever `today`
    # is supplied (which is how these are tested). Recomputed against the
    # resolved `today` so every number on this page agrees.
    for filing in filings:
        if filing.get("due_date"):
            filing["days_until_due"] = _days_between(today, filing["due_date"])

    # Only dated hearings can be counted against a horizon; an undated
    # one is real (the calendar shows it) but can't be "within 14 days".
    upcoming = hearings["upcoming"]
    hearings_soon = sum(
        1 for h in upcoming
        if h.get("date") and (_days_between(today, h["date"]) or 0) <= HEARING_HORIZON_DAYS
    )

    drafts = [f for f in filings if f["status"] == "draft"]
    dated_drafts = sorted(
        (f for f in drafts if f.get("days_until_due") is not None),
        key=lambda f: f["days_until_due"],
    )
    rollup = _client_rollup(flagged, clients)

    return {
        "today": today,
        "stats": {
            "flagged": len(flagged),
            "clients": len(clients),
            "hearings_soon": hearings_soon,
            "hearing_horizon_days": HEARING_HORIZON_DAYS,
            "unassigned": rollup["unassigned"],
            "filing_drafts": len(drafts),
            # None when no draft carries a due date yet — the tile says so
            # in words rather than showing a countdown it can't compute.
            "nearest_due_days": dated_drafts[0]["days_until_due"] if dated_drafts else None,
        },
        "attention": _attention_items(flagged, filings, today),
        "recent": recent_bill_changes(conn, user_id),
        "hearings": upcoming[:5],
        "by_client": rollup["clients"],
        "unassigned": rollup["unassigned"],
    }
