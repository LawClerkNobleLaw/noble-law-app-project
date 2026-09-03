"""
legiscan_client.py — the one place that knows how to talk to LegiScan.

This has no web-server code and no database code in it at all — just:
find the API key, make the HTTP call, and shape a raw bill response into
clean fields. Both app.py (the live single-lookup web app) and
refresh_watchlist.py (the daily watch-list job) import this file instead of
each having their own copy.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from urllib.request import urlopen

import config

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
    """Kept as a function (not just `config.LEGISCAN_API_KEY` inline at
    every call site) for the one existing caller outside this module —
    app.py's main() used to call this to decide whether to print a
    startup warning; the actual env var read/~/.zshrc fallback now
    lives in config.py, the one place all of this app's environment
    variables are read from."""
    return config.LEGISCAN_API_KEY


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


# ── what a search row can say about itself ──────────────────────────
#
# getSearch's rows are thin: bill_id, bill_number, title, relevance,
# last_action, last_action_date, url (checked against a live response,
# not guessed from the docs — see _shape_search_rows). No sponsor, no
# committee, no session field, and no matched-text snippet.
#
# So the three things below are DERIVED from what is there, rather than
# fetched. Each is one string operation on a field already in hand, and
# together they are what the search page filters on — which is the whole
# reason the filters can exist without a getBill call per row.

# The session a result belongs to, off the end of its own LegiScan URL:
# https://legiscan.com/CA/bill/SB813/2025 -> "2025". California runs
# two-year sessions, so this is the session's FIRST year — a bill from
# the 2025-26 session reads 2025 whether it moved in 2025 or 2026.
_URL_YEAR = re.compile(r"/(\d{4})/?$")


def _session_year(url):
    match = _URL_YEAR.search(url or "")
    return int(match.group(1)) if match else None


# Chamber and measure type off the bill number's letter prefix. In
# California the prefix is unambiguous: A... originates in the Assembly,
# S... in the Senate, and the letters after that say what kind of
# measure it is. A lobbyist tracking legislation usually wants bills and
# constitutional amendments and not the resolutions, which is a
# distinction the search page could never make before.
_PREFIX = re.compile(r"^([A-Z]+)")

_MEASURE_TYPES = {
    "AB": "Bill", "SB": "Bill",
    "ACA": "Constitutional amendment", "SCA": "Constitutional amendment",
}


def _prefix_of(bill_number):
    match = _PREFIX.match((bill_number or "").upper())
    return match.group(1) if match else ""


def _chamber(bill_number):
    prefix = _prefix_of(bill_number)
    if prefix.startswith("A"):
        return "Assembly"
    if prefix.startswith("S"):
        return "Senate"
    return None


def _measure_type(bill_number):
    """Anything that isn't a bill or a constitutional amendment is a
    resolution — concurrent (ACR/SCR), joint (AJR/SJR) or single-house
    (AR/SR). Named that way rather than by its three-letter prefix
    because the prefix is what the user is already filtering out."""
    prefix = _prefix_of(bill_number)
    if not prefix:
        return None
    return _MEASURE_TYPES.get(prefix, "Resolution")


def _shape_search_rows(results):
    """searchresult dict (each entry keyed by an opaque result id, plus
    one 'summary' key to skip) -> the lightweight row list both
    search_bills() and smart_search() return: bill_id, bill_number,
    title, relevance, last_action, last_action_date, straight off
    getSearch's own result shape — no per-row getBill call, see
    search_bills()'s own docstring for why. Field names were checked
    against a live getSearch response for query='housing', not guessed
    from the API docs.

    Plus session_year, chamber and measure_type, each derived from a
    field already in the row (see above) rather than fetched — these are
    what the search page's filters run on.

    Sorted by LegiScan's own relevance score, not left in whatever
    order the dict/JSON happened to iterate in — same reasoning as
    lookup_bill()'s own relevance pick, just across every candidate
    instead of collapsing to one."""
    rows = [
        {
            "bill_id": r.get("bill_id"),
            "bill_number": r.get("bill_number"),
            "title": r.get("title"),
            "relevance": r.get("relevance"),
            "last_action": r.get("last_action"),
            "last_action_date": r.get("last_action_date"),
            "url": r.get("url"),
            "session_year": _session_year(r.get("url")),
            "chamber": _chamber(r.get("bill_number")),
            "measure_type": _measure_type(r.get("bill_number")),
        }
        for k, r in results.items()
        if k != "summary"
    ]
    rows.sort(key=lambda r: int(r["relevance"] or 0), reverse=True)
    return rows


# LegiScan's own `year` codes for getSearch. Left as their numbers
# rather than renamed, so the value that goes on the wire is the value
# in the code. Note that getSearch's DEFAULT already behaves as
# CURRENT_SESSION — checked live: an unqualified search for "artificial
# intelligence" and year=2 return the identical 119, all from the
# 2025-26 session. Passing it explicitly makes the default a stated
# choice rather than an inherited one, and gives ALL_SESSIONS something
# to be the opposite of.
YEAR_CURRENT_SESSION = 2
YEAR_ALL_SESSIONS = 1

# How many 50-row pages a page-load search will pull. Filters and sorts
# that only cover the first page of three are worse than none — they
# report "3 results" meaning "3 of the 50 I happen to be holding" — so
# the search page asks for the whole result set and filters that.
#
# The cost is real and bounded: four getSearch calls, issued
# concurrently so the wall-clock is one call's, and never a getBill
# (rows are shaped straight from the search response). Four pages covers
# every result for the queries a lobbyist actually runs — "artificial
# intelligence" is 150 rows, "cannabis licensing" 71 — and anything
# broader is truncated with the UI saying so rather than quietly
# filtering a slice.
#
# The daily saved-search job does NOT use this: it keeps the one-page
# default, since it is looking for what is newly relevant, not building
# a filterable list, and it runs once per saved search per morning.
SEARCH_PAGE_CAP = 4


def _search_call(query, page, year):
    params = {"state": "CA", "query": query, "page": page}
    if year:
        params["year"] = year
    search = legiscan_call("getSearch", **params)
    if search.get("status") != "OK":
        raise RuntimeError(f"LegiScan search failed: {search}")
    return search.get("searchresult", {})


def _search_result(results, rows, complete=True, reported_count=None):
    """The one return shape every search path here produces.

    `count` is the number of rows actually in hand, NOT LegiScan's own
    summary count — those disagree. Live, "artificial intelligence"
    reports count=119 while its three pages return 150 distinct
    bill_ids. The page used to print the 119 over a list of 150, so the
    honest number is the one we can stand behind, and LegiScan's
    estimate rides along as `reported_count` for anyone who wants it."""
    summary = results.get("summary", {})
    return {
        "results": rows,
        "page": summary.get("page_current"),
        "page_total": summary.get("page_total"),
        "count": len(rows),
        "reported_count": reported_count if reported_count is not None else summary.get("count"),
        # False means there are matches beyond SEARCH_PAGE_CAP that this
        # response does not contain — so a filter or a sort applied to it
        # is a filter over a prefix, and the page has to say so.
        "complete": complete,
    }


def search_bills(query, page=1, year=None, pages=1):
    """Free-text bill search (Discover) — unlike lookup_bill(), which
    resolves one known bill number to its full detail, this is for "I
    don't know the bill number, just what it's about." Returns
    lightweight rows straight from getSearch's own result, not a full
    getBill call per row (that would be one LegiScan API call per
    search result, which doesn't scale to a results list).

    `pages` > 1 pulls that many pages of the same search and returns
    them as one de-duplicated list, so the caller can filter and sort
    across the whole result set instead of across whichever 50 rows
    LegiScan put first. Pages after the first are fetched concurrently —
    they are independent GETs and urlopen releases the GIL, so four
    pages cost about what one does in wall-clock. `page` is ignored when
    `pages` > 1: paging and holding-it-all are two different modes.

    California only, same reasoning as lookup_bill()."""
    first = _search_call(query, 1 if pages > 1 else page, year)
    rows = _shape_search_rows(first)
    if pages <= 1:
        return _search_result(first, rows)

    page_total = int(first.get("summary", {}).get("page_total") or 1)
    wanted = min(page_total, pages)
    if wanted > 1:
        with ThreadPoolExecutor(max_workers=wanted - 1) as pool:
            for extra in pool.map(lambda n: _search_call(query, n, year), range(2, wanted + 1)):
                rows.extend(_shape_search_rows(extra))

    # LegiScan pages a result set that is still moving underneath us, so
    # the same bill can legitimately arrive on two pages. Keep the first
    # (higher-relevance) copy of each.
    seen = set()
    deduped = []
    for row in rows:
        if row["bill_id"] in seen:
            continue
        seen.add(row["bill_id"])
        deduped.append(row)
    deduped.sort(key=lambda r: int(r["relevance"] or 0), reverse=True)
    return _search_result(first, deduped, complete=page_total <= pages)


def smart_search(query, page=1, year=None, pages=1):
    """The merged /lookup search: one function that figures out whether
    `query` looks like a bill number ('SB122', or even a bare '122' —
    anything with a digit in it) or free text ('housing licensing'),
    and searches LegiScan accordingly, always as a results list.

    Unlike lookup_bill(), a digit query never collapses to one "best"
    match — getSearch can return the same bill number from more than
    one past session (bill numbers get reused every two-year session),
    and the whole point of merging lookup into a search page is to show
    all of them and let the user pick, not guess on their behalf. Same
    lightweight per-row shape as search_bills() either way (no per-row
    getBill call — see that function's own docstring for why); a full
    getBill only happens once the user picks a specific bill (see
    /api/report in app.py).

    A digit query that comes back with zero bill-number matches but
    also has non-digit characters in it (e.g. someone typed "housing
    2024" or a bill number with a typo) falls back to a free-text
    search of that same raw query, on the theory that a literal
    zero-results bill lookup usually means the query wasn't actually a
    bill number after all — a pure-digit query (no letters at all)
    skips that fallback, since there's no free-text signal in it to
    search on.

    California only, same reasoning as lookup_bill()/search_bills()."""
    query = (query or "").strip()
    has_digit = any(ch.isdigit() for ch in query)
    has_non_digit = any(not ch.isdigit() for ch in query)

    if not has_digit:
        return search_bills(query, page=page, year=year, pages=pages)

    # A bill-number search is one precise call and is never paged:
    # every bill sharing the number comes back at once (checked live —
    # "72" returns AB72/SB72/ACR72/... together), so there is no second
    # page to pull and `pages` has nothing to do here. It is also NOT
    # year-filtered: someone typing a bill number wants that bill, and
    # silently hiding the 2023 one that shares its number would be the
    # search lying about what exists.
    search = legiscan_call("getSearch", state="CA", bill=query.upper())
    if search.get("status") != "OK":
        raise RuntimeError(f"LegiScan search failed: {search}")
    results = search.get("searchresult", {})
    rows = _shape_search_rows(results)
    if rows or not has_non_digit:
        return _search_result(results, rows)
    return search_bills(query, page=page, year=year, pages=pages)
