"""
Tests for the search page's start state (P2-28).

Two of the three fillers are pure front-end (recent searches live in
localStorage; the interest chips are split out of a field /api/clients
already returns), so what there is to test on this side is the third:
"moved this week", and the window it means.

The claim that window makes is the thing worth pinning down. The refresh
job only visits bills somebody flagged, so this can never be a statement
about the Legislature at large — and a panel that implied otherwise
would be the kind of quiet overclaim the 601's "Known gaps" card exists
to avoid.
"""

import db
from conftest import insert_bill, insert_user


def _change(description="Status changed.", change_type="status", summary="Enrolled"):
    return {"change_type": change_type, "summary": summary,
            "description": description, "event_date": "2026-09-01"}


def _firm(conn, *emails):
    org_id = db.create_organization(conn, "Noble Law")
    ids = []
    for email in emails:
        user_id = insert_user(conn, email=email)
        conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (org_id, user_id))
        ids.append(user_id)
    conn.commit()
    return ids


def _flagged(conn, user_id, bill_id, number):
    insert_bill(conn, bill_id=bill_id, bill_number=number)
    db.flag_bill(conn, user_id, bill_id)
    return bill_id


# ── The window ─────────────────────────────────────────────────────────

def test_a_change_inside_the_window_is_reported(conn):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id, 1, "SB1")
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2026-09-01")
    conn.commit()

    changes = db.recent_bill_changes(conn, user_id, since="2026-08-27")

    assert [c["bill_number"] for c in changes] == ["SB1"]


def test_a_change_older_than_the_window_is_not(conn):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id, 1, "SB1")
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2026-08-20")
    conn.commit()

    assert db.recent_bill_changes(conn, user_id, since="2026-08-27") == []


def test_the_window_edge_is_inclusive(conn):
    """"Moved this week" that silently drops the seventh day would be
    off by one on exactly the day a user checks before a Monday
    hearing."""
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id, 1, "SB1")
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2026-08-27")
    conn.commit()

    assert len(db.recent_bill_changes(conn, user_id, since="2026-08-27")) == 1


def test_a_timestamp_is_compared_as_a_date(conn):
    """detected_at carries a time; the window is a date. An afternoon
    change on the boundary day still counts."""
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id, 1, "SB1")
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2026-08-27 16:42:00")
    conn.commit()

    assert len(db.recent_bill_changes(conn, user_id, since="2026-08-27")) == 1


def test_no_window_still_returns_the_dashboard_feed(conn):
    """The dashboard calls this without `since` and must be unaffected."""
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id, 1, "SB1")
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2020-01-01")
    conn.commit()

    assert len(db.recent_bill_changes(conn, user_id)) == 1


# ── What the window can and cannot claim ───────────────────────────────

def test_only_bills_the_firm_flagged_are_included(conn):
    """The load-bearing scope. The refresh job never visits an unflagged
    bill, so a change on one cannot appear here — and the panel says
    "among the bills your firm already tracks" for exactly this
    reason."""
    user_id = insert_user(conn)
    flagged = _flagged(conn, user_id, 1, "SB1")
    unflagged = insert_bill(conn, bill_id=2, bill_number="SB2")
    db.record_bill_changes(conn, flagged, [_change()], detected_at="2026-09-01")
    db.record_bill_changes(conn, unflagged, [_change()], detected_at="2026-09-01")
    conn.commit()

    changes = db.recent_bill_changes(conn, user_id, since="2026-08-27")

    assert [c["bill_number"] for c in changes] == ["SB1"]


def test_a_colleagues_flag_counts_as_the_firms(conn):
    mine, theirs = _firm(conn, "a@firm.com", "b@firm.com")
    bill_id = _flagged(conn, mine, 1, "SB1")
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2026-09-01")
    conn.commit()

    assert len(db.recent_bill_changes(conn, theirs, since="2026-08-27")) == 1


def test_another_firms_movement_is_invisible(conn):
    (mine,) = _firm(conn, "a@firm.com")
    (outsider,) = _firm(conn, "elsewhere@other.com")
    bill_id = _flagged(conn, mine, 1, "SB1")
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2026-09-01")
    conn.commit()

    assert db.recent_bill_changes(conn, outsider, since="2026-08-27") == []


# ── Ordering and shape ─────────────────────────────────────────────────

def test_newest_first(conn):
    user_id = insert_user(conn)
    for bill_id, number, when in ((1, "SB1", "2026-08-28"), (2, "SB2", "2026-09-02")):
        _flagged(conn, user_id, bill_id, number)
        db.record_bill_changes(conn, bill_id, [_change()], detected_at=when)
    conn.commit()

    changes = db.recent_bill_changes(conn, user_id, since="2026-08-27")

    assert [c["bill_number"] for c in changes] == ["SB2", "SB1"]


def test_a_bill_that_moved_twice_returns_both_changes(conn):
    """The feed is chronological, not one-row-per-bill — the page
    collapses to one line per bill itself, because a bill that moved
    three times is still one thing to look at."""
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id, 1, "SB1")
    db.record_bill_changes(conn, bill_id, [_change("Amended.")], detected_at="2026-08-28")
    db.record_bill_changes(conn, bill_id, [_change("Hearing set.")], detected_at="2026-09-02")
    conn.commit()

    changes = db.recent_bill_changes(conn, user_id, since="2026-08-27")

    assert [c["description"] for c in changes] == ["Hearing set.", "Amended."]


def test_each_row_carries_enough_to_render_a_line(conn):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id, 1, "SB1159")
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2026-09-01")
    conn.commit()

    row = db.recent_bill_changes(conn, user_id, since="2026-08-27")[0]

    for key in ("bill_id", "state", "bill_number", "title", "summary", "description"):
        assert row[key] is not None, f"{key} missing"


def test_the_limit_is_honoured(conn):
    user_id = insert_user(conn)
    for n in range(1, 6):
        _flagged(conn, user_id, n, f"SB{n}")
        db.record_bill_changes(conn, n, [_change()], detected_at="2026-09-01")
    conn.commit()

    assert len(db.recent_bill_changes(conn, user_id, limit=3, since="2026-08-27")) == 3


# ── The window's left edge ─────────────────────────────────────────────

def test_days_ago_is_measured_on_californias_clock(conn):
    """Every other date cut in this app is Pacific (see
    today_in_california); a "this week" edge measured in UTC would move
    a day early through the working afternoon."""
    today = db.today_in_california()

    assert db.days_ago_in_california(0) == today
    assert db.days_ago_in_california(7) < today
    # Exactly seven days, not six or eight.
    from datetime import date
    delta = date.fromisoformat(today) - date.fromisoformat(db.days_ago_in_california(7))
    assert delta.days == 7


# ── Which index, chosen before the search rather than after ────────────
#
# The three "Search in" tabs used to render inside #results, so on a
# fresh page they did not exist: the only route to full bill text was to
# run a title search you didn't want and then switch. The choice of
# index is part of the question, so it belongs beside the box.

import app


def _lookup():
    return app.LOOKUP_BODY


def test_the_mode_tabs_are_on_the_page_before_any_search():
    body = _lookup()
    assert 'id="mode-tabs"' in body
    # In the search card, above the input — not in the results area,
    # which is empty until a search has run.
    assert body.index('id="mode-tabs"') < body.index('<input id="q"')
    assert body.index('id="mode-tabs"') < body.index('id="results"')


def test_the_mode_tabs_are_rendered_at_load():
    assert 'renderModeTabs();' in _lookup()


def test_the_results_rail_no_longer_carries_its_own_copy_of_the_tabs():
    """Two sets of the same three tabs, one of them the stale one, is
    how a control starts contradicting itself."""
    assert 'modeFiltersHtml' not in _lookup()
    assert _lookup().count('function modeTabsHtml') == 1


def test_a_mode_states_how_much_it_can_answer_for():
    """A mode that only holds 40 bills should say 40 before it is
    picked, not in a footnote under the empty result."""
    body = _lookup()
    assert "/api/corpus" in body
    assert "function modeCoverage" in body


def test_an_empty_title_search_points_at_whatever_index_can_answer():
    assert "function alternativesHtml" in _lookup()
    assert "searchMeta.alternatives" in _lookup()


# ── The search as a URL ────────────────────────────────────────────────
#
# Five things decide what is on the search page — the query, the index,
# the sessions, the sort and three filter groups — and only the query
# was ever in the URL, only on the way in. So a search couldn't be sent
# to a colleague, couldn't be bookmarked, and Back out of a bill landed
# on a page that had forgotten the narrowing that found it.

def test_the_page_reads_its_whole_state_out_of_the_url():
    body = _lookup()
    assert "function readUrlState" in body
    assert "function searchStateUrl" in body
    for param in ("'mode'", "'session'", "'sort'", "'tracked'"):
        assert param in body


def test_back_and_forward_are_handled():
    body = _lookup()
    assert "'popstate'" in body
    assert "applyUrlState(true)" in body


def test_a_refinement_rewrites_the_entry_and_a_new_question_adds_one():
    """Twelve history entries for one triage pass makes Back useless as
    a way out of the page; one per question asked is the point of it."""
    body = _lookup()
    assert "history[push ? 'pushState' : 'replaceState']" in body
    assert "syncUrl(!keepFilters)" in body


def test_the_back_link_a_bill_carries_is_the_whole_search():
    assert "const backHref = encodeURIComponent(searchStateUrl());" in _lookup()


def test_walking_back_through_searches_spends_no_api_calls():
    body = _lookup()
    assert "const resultCache = new Map()" in body
    assert "fromHistory && resultCache.has(cacheKey)" in body
