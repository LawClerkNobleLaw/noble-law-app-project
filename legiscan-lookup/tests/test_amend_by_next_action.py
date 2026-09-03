"""
Tests for the user's own amendment deadline reaching the flagged list's
Next action column (bills.amend_by_date -> list_flagged_bills).

The field itself is old — a bare, unexplained date input on the bill
report. What's new is that it now means something outside that one card:
it's counted against California's date the same way a hearing is, and a
bill whose only dated obligation is a deadline the user typed sorts in
with the bills that have hearings instead of falling to the tail.

`today` is pinned in every test for the same reason test_dashboard.py
pins it: a suite whose expectations drift with the calendar starts
failing on a date nobody chose.
"""

import db
from conftest import insert_bill, insert_user

TODAY = "2026-09-02"


def _flagged_row(conn, user_id, bill_id):
    rows = db.list_flagged_bills(conn, user_id, today=TODAY)
    return next(r for r in rows if r["bill_id"] == bill_id)


def test_amend_by_date_comes_back_with_a_countdown(conn):
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1")
    db.flag_bill(conn, user_id, 1)
    db.set_bill_amend_by_date(conn, 1, "2026-09-09")

    row = _flagged_row(conn, user_id, 1)
    assert row["amend_by_date"] == "2026-09-09"
    assert row["amend_by_days_until"] == 7


def test_amend_by_date_today_counts_as_zero_not_missing(conn):
    """The distinction the column depends on: 0 renders a "Today" chip,
    None renders "No date set"."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1")
    db.flag_bill(conn, user_id, 1)
    db.set_bill_amend_by_date(conn, 1, TODAY)

    assert _flagged_row(conn, user_id, 1)["amend_by_days_until"] == 0


def test_past_amend_by_date_stops_counting(conn):
    """A deadline that's already gone isn't a next action. The date stays
    on the row (it's still the user's own entry, and the bill report
    still shows it); only the countdown goes away, so the column doesn't
    advertise a negative number of days to prepare."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1")
    db.flag_bill(conn, user_id, 1)
    db.set_bill_amend_by_date(conn, 1, "2026-08-01")

    row = _flagged_row(conn, user_id, 1)
    assert row["amend_by_date"] == "2026-08-01"
    assert row["amend_by_days_until"] is None


def test_no_amend_by_date_leaves_both_fields_empty(conn):
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1")
    db.flag_bill(conn, user_id, 1)

    row = _flagged_row(conn, user_id, 1)
    assert row["amend_by_date"] is None
    assert row["amend_by_days_until"] is None


def test_clearing_the_date_clears_the_countdown(conn):
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1")
    db.flag_bill(conn, user_id, 1)
    db.set_bill_amend_by_date(conn, 1, "2026-09-09")
    db.set_bill_amend_by_date(conn, 1, "")

    row = _flagged_row(conn, user_id, 1)
    assert row["amend_by_date"] is None
    assert row["amend_by_days_until"] is None
