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
        # LegiScan's own amendment documents for this bill — separate from
        # `history` (which is status/procedural events, not the amendment
        # text itself).
        "amendments": [
            {
                "amendment_id": a.get("amendment_id"),
                "chamber": a.get("chamber"),
                "date": a.get("date"),
                "title": a.get("title") or a.get("description"),
                "description": a.get("description"),
                "adopted": bool(a.get("adopted")),
                "url": a.get("state_link") or a.get("url"),
            }
            for a in bill.get("amendments", [])
        ],
        # LegiScan's `calendar` array — scheduled committee/floor events,
        # which is where hearing dates actually live.
        "hearings": [
            {
                "event_type": c.get("type"),
                "date": c.get("date"),
                "time": c.get("time"),
                "location": c.get("location"),
                "description": c.get("description"),
            }
            for c in bill.get("calendar", [])
        ],
        # LegiScan's own vote index for this bill — roll_call_id, the
        # tally, and which chamber, already broken out per-vote by
        # LegiScan itself (no separate getRollCall call needed for
        # this level of detail).
        "votes": [
            {
                "roll_call_id": v.get("roll_call_id"),
                "chamber": v.get("chamber"),
                "date": v.get("date"),
                "description": v.get("desc"),
                "yea": v.get("yea"),
                "nay": v.get("nay"),
                "nv": v.get("nv"),
                "absent": v.get("absent"),
                "total": v.get("total"),
                "passed": bool(v.get("passed")),
            }
            for v in bill.get("votes", [])
        ],
    }


def get_bill_detail(bill_id):
    """Full bill detail by LegiScan's internal bill_id — one API call."""
    detail = legiscan_call("getBill", id=bill_id)
    if detail.get("status") != "OK":
        raise RuntimeError(f"LegiScan getBill failed: {detail}")
    return shape_bill(detail["bill"])


def lookup_bill(bill_number):
    """Resolve a human bill number (e.g. 'SB122') to full detail.
    getBill needs LegiScan's internal bill_id, so this searches first —
    this is the live single-lookup path the existing app uses.

    California only, on purpose — this app is built for CA lobbyists,
    so the state was dropped from the search entirely rather than left
    as a field nobody but a CA lobbyist would ever change."""
    bill_number = bill_number.strip().upper()

    search = legiscan_call("getSearch", state="CA", bill=bill_number)
    if search.get("status") != "OK":
        raise RuntimeError(f"LegiScan search failed: {search}")

    # getSearch can return the same bill number from more than one past
    # session (bill numbers get reused every two-year session), and
    # dict/JSON key order isn't a reliable stand-in for "best match" — so
    # pick explicitly by LegiScan's own relevance score instead of just
    # taking whatever happened to come first. Ties still favor whichever
    # sorts first, but that's now an explicit, visible choice rather than
    # an accident of dict iteration order.
    results = search.get("searchresult", {})
    candidates = [v for k, v in results.items() if k != "summary"]
    if not candidates:
        raise RuntimeError(f"No bill found for CA {bill_number}.")
    match = max(candidates, key=lambda v: int(v.get("relevance") or 0))

    return get_bill_detail(match["bill_id"])


def search_bills(query, page=1):
    """Free-text bill search (Discover) — unlike lookup_bill(), which
    resolves one known bill number to its full detail, this is for "I
    don't know the bill number, just what it's about." Returns
    lightweight rows straight from getSearch's own result, not a full
    getBill call per row (that would be one LegiScan API call per
    search result, which doesn't scale to a results list).

    California only, same reasoning as lookup_bill().

    Field names below were checked against a live getSearch response
    for query='housing', not guessed from the API docs — searchresult
    entries already come back with exactly these keys (relevance,
    bill_id, bill_number, title, last_action, last_action_date), so
    this only picks the subset the results list actually shows rather
    than renaming anything."""
    search = legiscan_call("getSearch", state="CA", query=query, page=page)
    if search.get("status") != "OK":
        raise RuntimeError(f"LegiScan search failed: {search}")

    results = search.get("searchresult", {})
    summary = results.get("summary", {})
    rows = [
        {
            "bill_id": r.get("bill_id"),
            "bill_number": r.get("bill_number"),
            "title": r.get("title"),
            "relevance": r.get("relevance"),
            "last_action": r.get("last_action"),
            "last_action_date": r.get("last_action_date"),
        }
        for k, r in results.items()
        if k != "summary"
    ]
    rows.sort(key=lambda r: int(r["relevance"] or 0), reverse=True)
    return {
        "results": rows,
        "page": summary.get("page_current"),
        "page_total": summary.get("page_total"),
        "count": summary.get("count"),
    }
