#!/usr/bin/env python3
"""
refresh_watchlist.py — the daily watch-list refresh job.

Runs once, does its work, and exits — it is NOT a server, and it does not
run continuously. A macOS launchd job (see launchd/, set up by
refresh.sh) starts this once a day.

What it does, in order:
  1. Reads the list of watched bill IDs out of db/billwatch.db.
  2. Calls LegiScan once per watched bill (one query each — see the quota
     math below).
  3. Before saving, snapshots what the bill's row already looked like
     (db.snapshot_bill_state) and diffs it against the fresh LegiScan
     response (db.diff_bill_state) — status change, new amendment,
     newly scheduled hearing, new vote — then saves the fresh
     status/sponsors/history/amendments/hearings/votes via the same
     db.upsert_bill() the live app uses when a bill is first added.
  4. Once every watched bill's been checked, hands the whole day's
     changes to digest.py, which emails one "what changed" digest per
     user per day — only to users who actually have a change on one of
     THEIR flagged bills, nobody else.
  5. Appends one plain-English line to logs/refresh.log summarizing the
     run, so it can be checked without reading any code.

Quota math: LegiScan's free tier is 30,000 queries/month. Even a 200-bill
watch list checked once a day for 30 days is 6,000 queries/month — about
20% of the free tier, with a lot of room before this needs a paid plan.

Reuses legiscan_client.py (the same "talk to LegiScan" code the live app
uses) and db.py (the same database code) — nothing here is duplicated.
"""

import os
import time

import db
import digest
from legiscan_client import get_bill_detail

# db.DB_DIR (not a path relative to this file) so the log lives on the
# same disk as the SQLite file — on Render, the code checkout gets
# rebuilt on every deploy but the persistent disk db.DB_DIR points at
# doesn't (see db.py's own module docstring); a log path relative to
# __file__ would silently lose its whole history on every redeploy.
# Named refresh_watchlist.log (not just "refresh.log") because
# refresh_calaccess.py's own LOG_DIR resolves to this exact same
# directory once BILLWATCH_DATA_DIR is set — a shared generic filename
# would have the two jobs interleave into one file.
LOG_DIR = os.path.join(db.DB_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "refresh_watchlist.log")


def log(message):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%b %-d, %-I:%M%p").lower()
    with open(LOG_PATH, "a") as f:
        f.write(f"{stamp} — {message}\n")
    # flush=True — see refresh_calaccess.py's log() for why this matters
    # once this runs as a background thread inside app.py on Render
    # rather than as its own short-lived local process.
    print(message, flush=True)


def refresh_one(conn, bill_id):
    """Returns (changed, digest_changes). `changed` is True if LegiScan's
    own change_hash moved at all (same meaning as before — drives the
    "N changed" count in the log line). `digest_changes` is the more
    specific status/amendment/hearing/vote breakdown the daily digest
    email is built from — not the same thing as `changed`, since a
    change_hash bump can happen for reasons this app doesn't have a
    sentence for (e.g. a text-only correction), and would otherwise show
    up as a change with nothing to say about it.

    Snapshotting has to happen BEFORE upsert_bill() — that call replaces
    the old rows outright, so this is the only point where "old" and
    "new" both still exist to compare. Raises on any LegiScan/network
    error (caller decides how to count it)."""
    before = conn.execute("SELECT status_code, change_hash FROM bills WHERE id = ?", (bill_id,)).fetchone()
    before_state = db.snapshot_bill_state(conn, bill_id)
    bill = get_bill_detail(bill_id)
    digest_changes = db.diff_bill_state(before_state, bill)
    db.upsert_bill(conn, bill)
    db.touch_watchlist(conn, bill_id)
    conn.commit()
    changed = (not before) or before["change_hash"] != bill.get("change_hash")
    return changed, digest_changes


def main():
    db.init_db()
    conn = db.get_connection()
    try:
        bill_ids = db.list_watchlist_bill_ids(conn)
        if not bill_ids:
            log("checked 0 bills — watch list is empty")
            return {"checked": 0, "changed": 0, "errors": 0, "digest": None}

        changed_count = 0
        error_count = 0
        errors = []
        changes_by_bill = {}
        for bill_id in bill_ids:
            try:
                changed, digest_changes = refresh_one(conn, bill_id)
                if changed:
                    changed_count += 1
                if digest_changes:
                    changes_by_bill[bill_id] = digest_changes
            except Exception as e:
                # refresh_one() may have already run some of upsert_bill()'s
                # DELETE/INSERT statements before raising — those writes are
                # still pending in this shared connection's open transaction.
                # Without rolling back here, they'd sit uncommitted until the
                # next bill in this loop succeeds and calls conn.commit(),
                # which would silently persist this bill's half-written,
                # corrupted state right along with it.
                conn.rollback()
                error_count += 1
                errors.append(f"bill {bill_id}: {e}")

        summary = f"checked {len(bill_ids)} bill(s), {changed_count} changed, {error_count} error(s)"
        log(summary)
        for err in errors:
            log(f"  error — {err}")

        digest_summary = digest.send_all_digests(conn, changes_by_bill)
        log(
            f"digest: {digest_summary['sent']} sent, "
            f"{digest_summary['not_configured']} not sent (SMTP unconfigured), "
            f"{digest_summary['skipped']} skipped (no changes), "
            f"{digest_summary['errors']} error(s)"
        )
        return {
            "checked": len(bill_ids),
            "changed": changed_count,
            "errors": error_count,
            "digest": digest_summary,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    main()
