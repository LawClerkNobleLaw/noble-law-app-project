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
