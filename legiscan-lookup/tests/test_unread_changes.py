"""
Tests for the flagged list's unread dot — the last outstanding piece of
P0-2 (db._unread_counts_for_bills + db.mark_bill_viewed).

The rule under test is a comparison of two timestamps written by two
different callers: bill_change_events.detected_at, appended by the daily
refresh job, against flagged_bills.last_viewed_at, written when the user
opens the bill's report. Both are stamped in UTC in the same
'YYYY-MM-DDTHH:MM:SSZ' shape and compared as plain strings, so every test
here pins both explicitly rather than letting "now" decide — a suite that
depended on wall-clock ordering would be flaky by construction.
"""

import db
from conftest import insert_bill, insert_user


def _change(conn, bill_id, detected_at, summary="Enrolled"):
    db.record_bill_changes(
        conn, bill_id,
        [{"change_type": "status", "summary": summary, "description": summary, "event_date": None}],
        detected_at=detected_at,
    )
    conn.commit()


def _row(conn, user_id, bill_id):
    return next(r for r in db.list_flagged_bills(conn, user_id) if r["bill_id"] == bill_id)


def test_never_opened_counts_every_recorded_change(conn):
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)
    _change(conn, 1, "2026-09-01T06:00:00Z")
    _change(conn, 1, "2026-09-02T06:00:00Z", summary="Amended")

    row = _row(conn, user_id, 1)
    assert row["last_viewed_at"] is None
    assert row["unread_count"] == 2


def test_opening_the_bill_clears_the_dot(conn):
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)
    _change(conn, 1, "2026-09-01T06:00:00Z")

    assert db.mark_bill_viewed(conn, user_id, 1, viewed_at="2026-09-01T09:00:00Z") is True
    conn.commit()

    assert _row(conn, user_id, 1)["unread_count"] == 0


def test_a_change_after_the_visit_is_unread_again(conn):
    """The dot has to come back, or it's a one-time dismissal rather than
    a marker of what the user has actually seen."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)
    _change(conn, 1, "2026-09-01T06:00:00Z")
    db.mark_bill_viewed(conn, user_id, 1, viewed_at="2026-09-01T09:00:00Z")
    conn.commit()
    _change(conn, 1, "2026-09-02T06:00:00Z", summary="Amended")

    assert _row(conn, user_id, 1)["unread_count"] == 1


def test_unread_is_per_user_not_per_bill(conn):
    """Two firms tracking the same bill read it on their own schedules."""
    reader = insert_user(conn, email="reader@example.com")
    other = insert_user(conn, email="other@example.com")
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, reader, 1)
    db.flag_bill(conn, other, 1)
    _change(conn, 1, "2026-09-01T06:00:00Z")
    db.mark_bill_viewed(conn, reader, 1, viewed_at="2026-09-01T09:00:00Z")
    conn.commit()

    assert _row(conn, reader, 1)["unread_count"] == 0
    assert _row(conn, other, 1)["unread_count"] == 1


def test_bill_with_no_recorded_changes_has_no_dot(conn):
    """The common case on a database that predates bill_change_events:
    nothing recorded, so nothing unread — not "everything unread"."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)

    assert _row(conn, user_id, 1)["unread_count"] == 0


def test_marking_an_unflagged_bill_is_a_no_op(conn):
    """Opening the report for a bill you don't track is ordinary — a
    search result opens the same page — and there's no dot to clear."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1)

    assert db.mark_bill_viewed(conn, user_id, 1) is False


def test_unflagging_forgets_that_the_bill_was_read(conn):
    """last_viewed_at lives on the flag, so re-flagging a bill later
    starts from "you haven't looked at this", which is true — the user
    stopped tracking it and whatever happened since is news again."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)
    _change(conn, 1, "2026-09-01T06:00:00Z")
    db.mark_bill_viewed(conn, user_id, 1, viewed_at="2026-09-01T09:00:00Z")
    conn.commit()

    db.unflag_bill(conn, user_id, 1)
    db.flag_bill(conn, user_id, 1)
    conn.commit()

    row = _row(conn, user_id, 1)
    assert row["last_viewed_at"] is None
    assert row["unread_count"] == 1
