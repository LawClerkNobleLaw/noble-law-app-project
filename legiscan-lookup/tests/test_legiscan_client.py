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
