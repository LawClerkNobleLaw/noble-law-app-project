"""
build_bill_corpus.py — fills and refreshes the searchable bill corpus
(`bill_texts`), which is what lets /lookup search a bill's operative
text instead of LegiScan's index of titles and summaries.

Run it directly:

    python3 build_bill_corpus.py              # top up, default budget
    python3 build_bill_corpus.py --budget 500 # spend at most 500 calls
    python3 build_bill_corpus.py --all-types  # resolutions too, not just AB/SB
    python3 build_bill_corpus.py --dry-run    # say what it would fetch
    python3 build_bill_corpus.py --reparse    # re-derive citations, no API calls

Deliberately a separate script from refresh_watchlist.py rather than a
step inside it. That job is a nightly must-run whose whole point is the
digest email: it touches the handful of bills this firm tracks, and if
it gets slow or hits an error the firm doesn't hear about a hearing.
This one walks the entire session, is measured in thousands of API
calls, and is fine to stop halfway through and finish tomorrow. Bolting
the second onto the first would put the digest behind a 4,000-call
queue.

── The budget, which is the whole design constraint ──

Measured live on 2026-09-04: the CA 2025-26 session has 5,060 bills,
4,243 of them AB/SB. Building the corpus costs two calls per bill — one
getBill to find the current document, one getBillText to fetch it — so
a first full build is ~8,500 calls against a free tier of 30,000 a
month. That is affordable exactly once, and ruinous if it happens by
accident every night.

So two things keep it bounded:

  * getMasterList returns all 5,060 bills WITH their change_hash in a
    single call. Comparing that against what the corpus already holds
    means the nightly cost is two calls per bill that actually moved,
    not per bill that exists.

  * --budget caps the calls any one run will spend and the script stops
    cleanly when it runs out, having committed everything up to that
    point. A first build can then be spread across several days without
    anyone having to track where it got to — the change_hash comparison
    IS the bookmark, so an interrupted run and a resumed one are the
    same code path.

Every run also derives each bill's code citations from the text it just
stored (see code_sections.py), and sweeps up any bill whose text was
already here but whose citations weren't parsed yet. That pass spends no
API calls, so it runs in full before a single call of the budget is
touched — and --reparse re-derives the whole corpus after a parser
change without refetching a byte.

Resolutions (ACR/SR/AJR/…) are skipped by default. They are ~16% of the
session and almost never the subject of a client position; --all-types
includes them for the firm that wants the completeness.
"""

import argparse
import os
import time

import bill_text
import code_sections
import db
import legiscan_client


# Where a run's log goes. Same directory and same reasoning as
# refresh_watchlist.py's — one file per job, not a shared generic name.
LOG_DIR = os.path.join(db.DB_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "build_bill_corpus.log")

# Two API calls per bill: getBill, then getBillText for its current
# document. Named rather than inlined as `2` because the budget
# arithmetic below is the point of the whole module.
CALLS_PER_BILL = 2

# A default that a stray cron entry or a fat-fingered manual run cannot
# turn into a quota incident, and that still gets a first build done in
# about a week of daily runs. Raise it explicitly for a deliberate
# backfill; the nightly top-up never comes close to it.
DEFAULT_BUDGET = 1200

# The measure types a lobbying practice actually takes positions on.
SUBSTANTIVE_PREFIXES = ("AB", "SB")


def log(message):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%b %-d, %-I:%M%p").lower()
    with open(LOG_PATH, "a") as f:
        f.write(f"{stamp} — {message}\n")
    # flush=True for the same reason refresh_watchlist.py's log() does
    # it: this may run as a background thread inside app.py rather than
    # as its own short-lived process.
    print(message, flush=True)


def measure_prefix(bill_number):
    """'AB 1234' / 'ABX11' -> 'AB'. Leading letters only, so the
    extraordinary-session forms (ABX, SBX) group with their own type
    rather than with AB/SB."""
    return "".join(c for c in (bill_number or "") if c.isalpha()).upper()


def current_session_id():
    """The session to index: California's newest non-special one.

    Special sessions get their own session_id and their own bills, and
    indexing them alongside the regular session would put an ABX1 next
    to an AB1 in the results with nothing to tell them apart. The
    regular session is what "current session" means everywhere else in
    this app (see legiscan_client.YEAR_CURRENT_SESSION).
    """
    payload = legiscan_client.legiscan_call("getSessionList", state="CA")
    sessions = [s for s in payload.get("sessions", []) if not s.get("special")]
    if not sessions:
        raise RuntimeError("LegiScan returned no regular CA sessions")
    return max(sessions, key=lambda s: int(s.get("year_start") or 0))


def master_list(session_id):
    """Every bill in the session, with change_hash — one API call."""
    payload = legiscan_client.legiscan_call("getMasterList", id=session_id)
    master = payload.get("masterlist") or {}
    return [row for key, row in master.items() if key != "session"]


def needs_fetching(bills, indexed, all_types=False):
    """The bills whose text the corpus doesn't have, or has a stale
    version of. Ordered so a budget-limited run makes visible progress:
    bills already indexed but changed come first (a stale row is a
    search returning last month's text as if it were current, which is
    worse than a search missing a bill), then the never-indexed ones."""
    stale, missing = [], []
    for row in bills:
        if not all_types and measure_prefix(row.get("number")) not in SUBSTANTIVE_PREFIXES:
            continue
        bill_id = row.get("bill_id")
        if bill_id is None:
            continue
        if bill_id not in indexed:
            missing.append(row)
        elif indexed[bill_id] != row.get("change_hash"):
            stale.append(row)
    return stale + missing


def index_one(conn, bill_id):
    """getBill + getBillText for one bill -> one corpus row.

    Returns the number of API calls actually spent, so the caller's
    budget reflects what was used rather than what was planned — a bill
    with no text document costs one call, not two, and the run gets
    that call back.
    """
    detail = legiscan_client.legiscan_call("getBill", id=bill_id)
    if detail.get("status") != "OK":
        raise RuntimeError(f"LegiScan getBill failed: {detail}")
    bill = detail["bill"]
    document = bill_text.current_document(bill.get("texts"))
    if not document:
        # A real and unremarkable state — a just-introduced bill can
        # exist in the master list before its text is posted. Nothing is
        # written, so the next run will try again rather than caching
        # the absence as if it were the answer.
        return 1
    body, _mime, byte_size = bill_text.fetch_document(document.get("doc_id"))
    db.upsert_bill_text(conn, bill_text.searchable_row(bill, document, body, byte_size))
    # Derived from the text just stored, in the same transaction — the
    # citations and the text they came from should never be separately
    # true. Costs no API call (see code_sections.py).
    db.replace_bill_code_sections(conn, bill.get("bill_id"), code_sections.extract(body))
    conn.commit()
    return 2


def parse_pending(conn):
    """Derive citations for every corpus row that has text but no parse.
    Returns how many were done. Spends no API calls, so the caller runs
    this before anything that has a budget."""
    pending = db.bills_needing_section_parse(conn)
    for bill_id, body in pending:
        db.replace_bill_code_sections(conn, bill_id, code_sections.extract(body))
    if pending:
        conn.commit()
    return len(pending)


def main(budget=DEFAULT_BUDGET, all_types=False, dry_run=False, reparse=False):
    db.init_db()
    conn = db.get_connection()
    try:
        if reparse:
            # Re-derive from text already held — for a parser change.
            # Deliberately does not touch `body` or `change_hash`, so
            # nothing is refetched.
            conn.execute("UPDATE bill_texts SET sections_parsed_at = NULL")
            conn.commit()

        session = current_session_id()
        bills = master_list(session["session_id"])
        indexed = db.indexed_change_hashes(conn)
        queue = needs_fetching(bills, indexed, all_types=all_types)

        log(
            f"{session.get('session_name')}: {len(bills)} bill(s) in session, "
            f"{len(indexed)} indexed, {len(queue)} to fetch "
            f"(~{len(queue) * CALLS_PER_BILL} calls), budget {budget}"
        )
        if dry_run:
            return {"queued": len(queue), "indexed": 0, "errors": 0, "spent": 0}

        # Free pass first: any bill whose text is already here but whose
        # citations aren't derived yet. Costs no budget, so it runs in
        # full before a single call is spent — including after a parser
        # change, where clearing sections_parsed_at re-derives the whole
        # corpus without refetching a byte of it.
        reparsed = parse_pending(conn)
        if reparsed:
            log(f"parsed citations for {reparsed} bill(s) already held (no API calls)")

        spent = 0
        done = 0
        errors = []
        for row in queue:
            if spent + CALLS_PER_BILL > budget:
                log(f"budget reached — stopping at {done} bill(s); rerun to continue")
                break
            try:
                spent += index_one(conn, row["bill_id"])
                done += 1
            except Exception as e:
                # Same reasoning as refresh_watchlist.py's loop: roll
                # back so a half-written bill can't ride along on the
                # next successful commit. Budget still counts the calls,
                # which were spent whether or not they landed.
                conn.rollback()
                spent += CALLS_PER_BILL
                errors.append(f"bill {row.get('bill_id')} ({row.get('number')}): {e}")

        stats = db.corpus_stats(conn)
        log(
            f"indexed {done} bill(s), {len(errors)} error(s), {spent} call(s) spent; "
            f"corpus now {stats['bills']} bill(s), {stats['bytes'] / 1_000_000:.0f}MB of source HTML"
        )
        for err in errors[:20]:
            log(f"  error — {err}")
        return {"queued": len(queue), "indexed": done, "errors": len(errors), "spent": spent}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help=f"max LegiScan calls to spend (default {DEFAULT_BUDGET})")
    parser.add_argument("--all-types", action="store_true",
                        help="index resolutions too, not just AB/SB")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be fetched, spend nothing")
    parser.add_argument("--reparse", action="store_true",
                        help="re-derive code citations from text already held (no API calls)")
    args = parser.parse_args()
    main(budget=args.budget, all_types=args.all_types, dry_run=args.dry_run,
         reparse=args.reparse)
