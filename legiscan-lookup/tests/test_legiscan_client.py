"""
Tests for legiscan_client.smart_search() — the merged /lookup search's
one entry point, deciding between a bill-number search and a free-text
one depending on the query itself, then shaping results the same way
search_bills() already does. Every LegiScan call is faked by
monkeypatching legiscan_client.legiscan_call() directly (smart_search()
and search_bills() both call it by that module-level name), so none of
these hit the real network.

Fake getSearch responses below match the real shape checked against a
live response (see search_bills()'s own docstring): a dict keyed by an
opaque result id, plus one 'summary' key alongside the results to skip.
"""

import legiscan_client


def _search_result(bill_id, bill_number, title, relevance="1000", last_action=None, last_action_date=None):
    return {
        "bill_id": bill_id,
        "bill_number": bill_number,
        "title": title,
        "relevance": relevance,
        "last_action": last_action,
        "last_action_date": last_action_date,
    }


def _ok_response(rows, page_current=1, page_total=1, count=None):
    results = {str(i): row for i, row in enumerate(rows)}
    results["summary"] = {
        "page_current": page_current,
        "page_total": page_total,
        "count": count if count is not None else len(rows),
    }
    return {"status": "OK", "searchresult": results}


def test_smart_search_bare_digit_query_returns_multiple_bill_types(monkeypatch):
    # "122" alone (no letters) still has a digit, so this should search
    # by bill= rather than free text — and, unlike lookup_bill(), never
    # collapse to a single "best" match: LegiScan can return AB122,
    # SB122, and ACR122 all matching a bare numeric query, and the
    # merged search page should show all of them for the user to pick.
    calls = []

    def fake_legiscan_call(op, **params):
        calls.append((op, params))
        assert params.get("bill") == "122"
        return _ok_response([
            _search_result(1, "AB122", "An Assembly bill.", relevance="900"),
            _search_result(2, "SB122", "A Senate bill.", relevance="1000"),
            _search_result(3, "ACR122", "An Assembly concurrent resolution.", relevance="500"),
        ])

    monkeypatch.setattr(legiscan_client, "legiscan_call", fake_legiscan_call)

    result = legiscan_client.smart_search("122")

    assert len(calls) == 1
    assert [r["bill_number"] for r in result["results"]] == ["SB122", "AB122", "ACR122"]  # sorted by relevance


def test_smart_search_full_bill_number_query_returns_one_result(monkeypatch):
    def fake_legiscan_call(op, **params):
        assert params.get("bill") == "SB122"
        return _ok_response([_search_result(2, "SB122", "A Senate bill.")])

    monkeypatch.setattr(legiscan_client, "legiscan_call", fake_legiscan_call)

    result = legiscan_client.smart_search("sb122")

    assert len(result["results"]) == 1
    assert result["results"][0]["bill_number"] == "SB122"


def test_smart_search_free_text_query_still_works(monkeypatch):
    # No digits at all — should go straight to a query= search, same
    # as search_bills(), never touching bill=.
    calls = []

    def fake_legiscan_call(op, **params):
        calls.append(params)
        assert "bill" not in params
        assert params.get("query") == "housing"
        return _ok_response([
            _search_result(10, "AB10", "Housing element law.", last_action="Introduced"),
            _search_result(11, "SB20", "Housing finance.", last_action="Chaptered"),
        ])

    monkeypatch.setattr(legiscan_client, "legiscan_call", fake_legiscan_call)

    result = legiscan_client.smart_search("housing")

    assert len(calls) == 1
    assert {r["bill_number"] for r in result["results"]} == {"AB10", "SB20"}


def test_smart_search_falls_back_to_free_text_when_digit_query_has_no_bill_matches(monkeypatch):
    # "housing 2024" has both a digit and letters — a zero-result
    # bill-number search should fall back to a free-text search of the
    # exact same raw query string, not just report nothing found.
    calls = []

    def fake_legiscan_call(op, **params):
        calls.append(dict(params))
        if "bill" in params:
            assert params["bill"] == "HOUSING 2024"
            return _ok_response([])
        assert params.get("query") == "housing 2024"
        return _ok_response([_search_result(5, "AB99", "Housing policy for 2024.")])

    monkeypatch.setattr(legiscan_client, "legiscan_call", fake_legiscan_call)

    result = legiscan_client.smart_search("housing 2024")

    assert len(calls) == 2
    assert "bill" in calls[0]
    assert "query" in calls[1]
    assert result["results"][0]["bill_number"] == "AB99"


def test_smart_search_pure_digit_query_with_no_matches_does_not_fall_back(monkeypatch):
    # A query with digits and nothing else has no free-text signal to
    # fall back on — zero bill matches should just mean zero results,
    # not a second call.
    calls = []

    def fake_legiscan_call(op, **params):
        calls.append(dict(params))
        return _ok_response([])

    monkeypatch.setattr(legiscan_client, "legiscan_call", fake_legiscan_call)

    result = legiscan_client.smart_search("9999")

    assert len(calls) == 1
    assert result["results"] == []


# ── derived row fields and multi-page search (P1-11) ─────────────────
#
# getSearch's rows carry no sponsor, no committee, no session field and
# no matched-text snippet (checked live). Chamber, measure type and
# session year are therefore derived from two fields that ARE there —
# the bill number and the result's own LegiScan URL — and these are what
# the search page's filters run on. If the derivation is wrong, every
# filter silently hides the wrong bills, so it is pinned here.


def _row_with_url(bill_id, bill_number, url, **kwargs):
    row = _search_result(bill_id, bill_number, "A bill.", **kwargs)
    row["url"] = url
    return row


def test_search_rows_derive_chamber_type_and_session(monkeypatch):
    monkeypatch.setattr(legiscan_client, "legiscan_call", lambda op, **p: _ok_response([
        _row_with_url(1, "AB1", "https://legiscan.com/CA/bill/AB1/2025", relevance="900"),
        _row_with_url(2, "SB2", "https://legiscan.com/CA/bill/SB2/2023", relevance="800"),
        _row_with_url(3, "SCA4", "https://legiscan.com/CA/bill/SCA4/2025", relevance="700"),
        _row_with_url(4, "ACR9", "https://legiscan.com/CA/bill/ACR9/2025", relevance="600"),
    ]))

    rows = legiscan_client.smart_search("housing")["results"]
    derived = {r["bill_number"]: (r["chamber"], r["measure_type"], r["session_year"]) for r in rows}

    assert derived["AB1"] == ("Assembly", "Bill", 2025)
    assert derived["SB2"] == ("Senate", "Bill", 2023)
    # A constitutional amendment is not a resolution, and a lobbyist
    # filtering resolutions out should not lose it.
    assert derived["SCA4"] == ("Senate", "Constitutional amendment", 2025)
    assert derived["ACR9"] == ("Assembly", "Resolution", 2025)


def test_search_rows_survive_a_missing_url(monkeypatch):
    monkeypatch.setattr(legiscan_client, "legiscan_call", lambda op, **p: _ok_response([
        _search_result(1, "AB1", "No url on this row."),
    ]))
    row = legiscan_client.smart_search("housing")["results"][0]
    assert row["session_year"] is None
    assert row["chamber"] == "Assembly"


def test_search_pulls_every_page_and_dedupes(monkeypatch):
    # The page filters and sorts across the whole result set, so it asks
    # for the whole result set. LegiScan pages a result set that is still
    # moving, so the same bill can arrive on two pages — the higher
    # relevance copy is the one kept.
    pages = {
        1: _ok_response([_row_with_url(1, "AB1", "u/2025", relevance="900"),
                         _row_with_url(2, "AB2", "u/2025", relevance="800")],
                        page_current=1, page_total=3, count=5),
        2: _ok_response([_row_with_url(2, "AB2", "u/2025", relevance="10"),
                         _row_with_url(3, "AB3", "u/2025", relevance="700")],
                        page_current=2, page_total=3),
        3: _ok_response([_row_with_url(4, "AB4", "u/2025", relevance="600")],
                        page_current=3, page_total=3),
    }
    seen = []

    def fake_call(op, **params):
        seen.append(params.get("page"))
        return pages[params["page"]]

    monkeypatch.setattr(legiscan_client, "legiscan_call", fake_call)

    result = legiscan_client.smart_search("housing", pages=4)

    assert sorted(seen) == [1, 2, 3]          # asked for exactly the pages that exist
    assert [r["bill_id"] for r in result["results"]] == [1, 2, 3, 4]
    # count is what we are actually holding, not LegiScan's own summary
    # figure — live, those disagree, and the page prints this one.
    assert result["count"] == 4
    assert result["reported_count"] == 5
    assert result["complete"] is True


def test_search_reports_itself_incomplete_past_the_page_cap(monkeypatch):
    monkeypatch.setattr(legiscan_client, "legiscan_call", lambda op, **p: _ok_response(
        [_row_with_url(p["page"], "AB%d" % p["page"], "u/2025")],
        page_current=p["page"], page_total=31, count=1504,
    ))

    result = legiscan_client.smart_search("housing", pages=legiscan_client.SEARCH_PAGE_CAP)

    # Filters applied to this are filters over a prefix, and the page
    # has to say so rather than presenting it as the whole set.
    assert result["complete"] is False
    assert result["count"] == legiscan_client.SEARCH_PAGE_CAP
    assert result["reported_count"] == 1504


def test_free_text_search_passes_the_year_through(monkeypatch):
    seen = {}

    def fake_call(op, **params):
        seen.update(params)
        return _ok_response([_row_with_url(1, "AB1", "u/2025")])

    monkeypatch.setattr(legiscan_client, "legiscan_call", fake_call)
    legiscan_client.smart_search("housing", year=legiscan_client.YEAR_ALL_SESSIONS)
    assert seen["year"] == legiscan_client.YEAR_ALL_SESSIONS


def test_bill_number_search_is_never_year_filtered(monkeypatch):
    # Someone typing a bill number wants that bill. Hiding the 2023 one
    # that shares its number would be the search lying about what exists.
    seen = {}

    def fake_call(op, **params):
        seen.update(params)
        return _ok_response([_row_with_url(1, "AB122", "u/2023")])

    monkeypatch.setattr(legiscan_client, "legiscan_call", fake_call)
    legiscan_client.smart_search("AB122", year=legiscan_client.YEAR_CURRENT_SESSION, pages=4)
    assert "year" not in seen
    assert seen["bill"] == "AB122"
