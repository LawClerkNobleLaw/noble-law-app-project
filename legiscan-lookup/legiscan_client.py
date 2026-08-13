"""
legiscan_client.py — the one place that knows how to talk to LegiScan.

This has no web-server code and no database code in it at all — just:
find the API key, make the HTTP call, and shape a raw bill response into
clean fields. Both app.py (the live single-lookup web app) and
refresh_watchlist.py (the daily watch-list job) import this file instead of
each having their own copy.
"""

import json
import os
import re
from urllib.parse import urlencode
from urllib.request import urlopen

LEGISCAN_BASE = "https://api.legiscan.com/"

# LegiScan's getBill `status` field — see https://legiscan.com/gaits/documentation
STATUS_LABELS = {
    0: "Prefiled",
    1: "Introduced",
    2: "Engrossed",
    3: "Enrolled",
    4: "Passed",
    5: "Vetoed",
    6: "Failed",
}


def get_api_key():
    key = os.environ.get("LEGISCAN_API_KEY")
    if key:
        return key
    # Fall back to parsing it out of ~/.zshrc, in case this was launched
    # from a shell that never sourced the profile (e.g. double-clicked, or
    # launchd, which doesn't read shell profiles at all).
    zshrc = os.path.expanduser("~/.zshrc")
    try:
        with open(zshrc) as f:
            for line in f:
                m = re.search(r'export\s+LEGISCAN_API_KEY\s*=\s*"?([^"\s]+)"?', line)
                if m:
                    return m.group(1)
    except FileNotFoundError:
        pass
    return None


def legiscan_call(op, **params):
    key = get_api_key()
    if not key:
        raise RuntimeError(
            "No LegiScan API key found. Set LEGISCAN_API_KEY in your "
            "environment (or ~/.zshrc) and try again."
        )
    query = {"key": key, "op": op, **params}
    url = LEGISCAN_BASE + "?" + urlencode(query)
    with urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def shape_bill(bill):
    """Turn LegiScan's raw `bill` object into the clean fields everything
    downstream (the live app, the database, the daily job) actually uses."""
    return {
        "id": bill.get("bill_id"),
        "state": bill.get("state"),
        "bill_number": bill.get("bill_number"),
        "session_label": (bill.get("session") or {}).get("session_name"),
        "title": bill.get("title"),
        "description": bill.get("description"),
        "status_code": bill.get("status"),
        "status_label": STATUS_LABELS.get(bill.get("status"), "In progress"),
        "status_date": bill.get("status_date"),
        "url": bill.get("url"),
        "change_hash": bill.get("change_hash"),
        "sponsors": [
            {"name": s.get("name"), "party": s.get("party"), "role": s.get("role")}
            for s in bill.get("sponsors", [])
        ],
        "history": [
            {"date": h.get("date"), "chamber": h.get("chamber"), "action": h.get("action")}
            for h in bill.get("history", [])
        ],
    }


def get_bill_detail(bill_id):
    """Full bill detail by LegiScan's internal bill_id — one API call."""
    detail = legiscan_call("getBill", id=bill_id)
    if detail.get("status") != "OK":
        raise RuntimeError(f"LegiScan getBill failed: {detail}")
    return shape_bill(detail["bill"])


def lookup_bill(state, bill_number):
    """Resolve a human bill number (e.g. 'CA', 'SB122') to full detail.
    getBill needs LegiScan's internal bill_id, so this searches first —
    this is the live single-lookup path the existing app uses."""
    state = state.strip().upper()
    bill_number = bill_number.strip().upper()

    search = legiscan_call("getSearch", state=state, bill=bill_number)
    if search.get("status") != "OK":
        raise RuntimeError(f"LegiScan search failed: {search}")

    results = search.get("searchresult", {})
    match = None
    for k, v in results.items():
        if k == "summary":
            continue
        match = v
        break
    if not match:
        raise RuntimeError(f"No bill found for {state} {bill_number}.")

    return get_bill_detail(match["bill_id"])
