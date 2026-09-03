"""
Tests for the dashboard's one read, db.dashboard_summary (see
DASHBOARD_BODY in app.py for the page it feeds).

Nothing here is new storage — the dashboard is a composition of readers
that already existed (list_flagged_bills, list_hearings_for_flagged_bills,
list_prepared_filings, list_clients) plus recent_bill_changes. So what's
worth testing is the composition itself: that the merged attention queue
orders three different kinds of deadline against each other correctly,
that every countdown on the page is measured from one date, and that the
per-client rollup agrees with the flagged list it's derived from.

Every test pins `today` explicitly rather than letting
today_in_california() answer: a suite whose expectations drift with the
calendar would start failing on a date nobody chose.
"""

import db
from conftest import insert_bill, insert_user

TODAY = "2026-09-02"


def _flag(conn, user_id, bill_id, number):
    insert_bill(conn, bill_id=bill_id, bill_number=number)
    db.flag_bill(conn, user_id, bill_id)


def _hearing(conn, bill_id, date, time=None, location=None):
    conn.execute(
        """INSERT INTO bill_hearings (bill_id, event_type, date, time, location, description)
           VALUES (?, 'hearing', ?, ?, ?, 'Committee hearing')""",
        (bill_id, date, time, location),
    )
    conn.commit()


def _change(conn, bill_id, detected_at, summary, change_type="status"):
    conn.execute(
        """INSERT INTO bill_change_events (bill_id, detected_at, change_type, summary, description)
           VALUES (?, ?, ?, ?, ?)""",
        (bill_id, detected_at, change_type, summary, summary),
    )
    conn.commit()


def _filing(conn, user_id, due_date=None):
    filing_id = db.create_prepared_filing(conn, user_id, "601", None, {"name": "x"})
    if due_date:
        db.set_prepared_filing_deadline(conn, user_id, filing_id, "2026-08-01", due_date)
    return filing_id


# ── The tiles ──

def test_summary_of_an_empty_account_is_all_zeros_not_an_error(conn):
    user_id = insert_user(conn)

    summary = db.dashboard_summary(conn, user_id, today=TODAY)

    assert summary["stats"]["flagged"] == 0
    assert summary["stats"]["clients"] == 0
    assert summary["stats"]["hearings_soon"] == 0
    assert summary["stats"]["nearest_due_days"] is None
    assert summary["attention"] == []
    assert summary["by_client"] == []


def test_hearings_tile_counts_only_the_next_fortnight(conn):
    user_id = insert_user(conn)
    _flag(conn, user_id, 1, "AB1")
    _flag(conn, user_id, 2, "AB2")
    _hearing(conn, 1, "2026-09-05")   # 3 days out — counts
    _hearing(conn, 2, "2026-10-15")   # 43 days out — does not

    summary = db.dashboard_summary(conn, user_id, today=TODAY)

    assert summary["stats"]["hearings_soon"] == 1
    # The panel below the tiles still lists it — the horizon is the
    # tile's question ("what's imminent"), not a filter on the calendar.
    assert len(summary["hearings"]) == 2


def test_nearest_due_days_picks_the_soonest_draft_and_goes_negative_when_overdue(conn):
    user_id = insert_user(conn)
    _filing(conn, user_id, due_date="2026-09-20")
    _filing(conn, user_id, due_date="2026-08-31")

    stats = db.dashboard_summary(conn, user_id, today=TODAY)["stats"]

    assert stats["filing_drafts"] == 2
    assert stats["nearest_due_days"] == -2


def test_filing_countdowns_are_measured_from_the_same_date_as_the_hearings(conn):
    # _row_to_prepared_filing counts from today_in_california() on its
    # own; the dashboard has to override that, or the page would compare
    # a filing measured from the real today against a hearing measured
    # from the date it was asked about.
    user_id = insert_user(conn)
    _filing(conn, user_id, due_date="2026-09-09")

    stats = db.dashboard_summary(conn, user_id, today=TODAY)["stats"]

    assert stats["nearest_due_days"] == 7


# ── The merged attention queue ──

def test_attention_queue_orders_filings_and_hearings_against_each_other(conn):
    user_id = insert_user(conn)
    client_id = db.create_client(conn, user_id, {"name": "Acme"})
    for bill_id, number in ((1, "AB1"), (2, "AB2")):
        _flag(conn, user_id, bill_id, number)
        db.link_bill_to_client(conn, user_id, bill_id, client_id)
    _hearing(conn, 1, "2026-09-06")            # 4 days out
    _hearing(conn, 2, "2026-09-03")            # tomorrow
    _filing(conn, user_id, due_date="2026-08-31")  # 2 days overdue

    items = db.dashboard_summary(conn, user_id, today=TODAY)["attention"]

    assert [i["days"] for i in items] == [-2, 1, 4]
    assert [i["kind"] for i in items] == ["filing", "hearing", "hearing"]


def test_attention_queue_puts_undated_cleanup_after_everything_with_a_date(conn):
    user_id = insert_user(conn)
    _flag(conn, user_id, 1, "AB1")   # no client assigned, no hearing
    _flag(conn, user_id, 2, "AB2")
    client_id = db.create_client(conn, user_id, {"name": "Acme"})
    db.link_bill_to_client(conn, user_id, 2, client_id)
    _hearing(conn, 2, "2026-09-06")

    items = db.dashboard_summary(conn, user_id, today=TODAY)["attention"]

    assert [i["kind"] for i in items] == ["hearing", "unassigned"]
    assert items[-1]["days"] is None


def test_attention_queue_ignores_hearings_and_filings_that_are_still_far_off(conn):
    user_id = insert_user(conn)
    client_id = db.create_client(conn, user_id, {"name": "Acme"})
    _flag(conn, user_id, 1, "AB1")
    db.link_bill_to_client(conn, user_id, 1, client_id)
    _hearing(conn, 1, "2026-09-30")                # 28 days out
    _filing(conn, user_id, due_date="2026-12-01")  # 90 days out

    assert db.dashboard_summary(conn, user_id, today=TODAY)["attention"] == []


def test_a_signed_off_filing_is_no_longer_something_to_act_on(conn):
    user_id = insert_user(conn)
    filing_id = _filing(conn, user_id, due_date="2026-09-03")
    conn.execute("UPDATE prepared_filings SET status = 'ready_to_file' WHERE id = ?", (filing_id,))
    conn.commit()

    summary = db.dashboard_summary(conn, user_id, today=TODAY)

    assert summary["attention"] == []
    assert summary["stats"]["filing_drafts"] == 0


# ── The activity feed ──

def test_recent_changes_are_newest_first_and_scoped_to_this_users_bills(conn):
    user_id = insert_user(conn)
    other_id = insert_user(conn, email="someone-else@example.com")
    _flag(conn, user_id, 1, "AB1")
    insert_bill(conn, bill_id=2, bill_number="AB2")
    db.flag_bill(conn, other_id, 2)
    _change(conn, 1, "2026-09-01 08:00:00", "Older change")
    _change(conn, 1, "2026-09-02 08:00:00", "Newer change")
    _change(conn, 2, "2026-09-02 09:00:00", "Not this user's bill")

    recent = db.dashboard_summary(conn, user_id, today=TODAY)["recent"]

    assert [r["summary"] for r in recent] == ["Newer change", "Older change"]


def test_one_bill_moving_twice_appears_twice_in_the_feed(conn):
    # Unlike the flagged table's Last change column (one row per bill),
    # this is a chronological feed — "what happened lately", not "where
    # does each bill stand".
    user_id = insert_user(conn)
    _flag(conn, user_id, 1, "AB1")
    _change(conn, 1, "2026-09-02 08:00:00", "Amended", change_type="amendment")
    _change(conn, 1, "2026-09-02 08:00:00", "Re-referred to committee")

    recent = db.dashboard_summary(conn, user_id, today=TODAY)["recent"]

    assert len(recent) == 2


# ── The client rollup ──

def test_client_rollup_counts_positions_and_keeps_clients_with_no_bills(conn):
    user_id = insert_user(conn)
    acme = db.create_client(conn, user_id, {"name": "Acme"})
    db.create_client(conn, user_id, {"name": "Nobody Yet"})
    for bill_id, number in ((1, "AB1"), (2, "AB2"), (3, "AB3")):
        _flag(conn, user_id, bill_id, number)
    db.link_bill_to_client(conn, user_id, 1, acme, position="support")
    db.link_bill_to_client(conn, user_id, 2, acme, position="oppose")

    summary = db.dashboard_summary(conn, user_id, today=TODAY)
    by_name = {c["name"]: c for c in summary["by_client"]}

    assert by_name["Acme"]["support"] == 1
    assert by_name["Acme"]["oppose"] == 1
    assert by_name["Acme"]["total"] == 2
    assert by_name["Nobody Yet"]["total"] == 0
    # The third bill is linked to nobody, and is counted once — as the
    # gap the "Needs a client" tile reports, not against any client.
    assert summary["unassigned"] == 1
    assert summary["stats"]["unassigned"] == 1


def test_a_bill_shared_by_two_clients_counts_for_both_and_is_not_unassigned(conn):
    user_id = insert_user(conn)
    acme = db.create_client(conn, user_id, {"name": "Acme"})
    beta = db.create_client(conn, user_id, {"name": "Beta"})
    _flag(conn, user_id, 1, "AB1")
    db.link_bill_to_client(conn, user_id, 1, acme, position="support")
    db.link_bill_to_client(conn, user_id, 1, beta, position="watch")

    summary = db.dashboard_summary(conn, user_id, today=TODAY)
    by_name = {c["name"]: c for c in summary["by_client"]}

    assert by_name["Acme"]["support"] == 1
    assert by_name["Beta"]["watch"] == 1
    assert summary["unassigned"] == 0


def test_summary_never_leaks_another_users_flagged_bills_or_clients(conn):
    user_id = insert_user(conn)
    other_id = insert_user(conn, email="someone-else@example.com")
    insert_bill(conn, bill_id=1, bill_number="AB1")
    db.flag_bill(conn, other_id, 1)
    db.create_client(conn, other_id, {"name": "Their Client"})

    summary = db.dashboard_summary(conn, user_id, today=TODAY)

    assert summary["stats"]["flagged"] == 0
    assert summary["by_client"] == []
