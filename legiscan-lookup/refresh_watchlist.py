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
  3. Saves the fresh status/sponsors/history via the same db.upsert_bill()
     the live app uses when a bill is first added.
  4. Appends one plain-English line to logs/refresh.log summarizing the
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
from legiscan_client import get_bill_detail

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_PATH = os.path.join(LOG_DIR, "refresh.log")


def log(message):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%b %-d, %-I:%M%p").lower()
    with open(LOG_PATH, "a") as f:
        f.write(f"{stamp} — {message}\n")
    print(message)


def refresh_one(conn, bill_id):
    """Returns True if the bill's status actually changed, False if not,
    raises on any LegiScan/network error (caller decides how to count it)."""
    before = conn.execute("SELECT status_code, change_hash FROM bills WHERE id = ?", (bill_id,)).fetchone()
    bill = get_bill_detail(bill_id)
    db.upsert_bill(conn, bill)
    db.touch_watchlist(conn, bill_id)
    conn.commit()
    changed = (not before) or before["change_hash"] != bill.get("change_hash")
    return changed


def main():
    db.init_db()
    conn = db.get_connection()
    try:
        bill_ids = db.list_watchlist_bill_ids(conn)
        if not bill_ids:
            log("checked 0 bills — watch list is empty")
            return

        changed_count = 0
        error_count = 0
        errors = []
        for bill_id in bill_ids:
            try:
                if refresh_one(conn, bill_id):
                    changed_count += 1
            except Exception as e:
                error_count += 1
                errors.append(f"bill {bill_id}: {e}")

        summary = f"checked {len(bill_ids)} bill(s), {changed_count} changed, {error_count} error(s)"
        log(summary)
        for err in errors:
            log(f"  error — {err}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
