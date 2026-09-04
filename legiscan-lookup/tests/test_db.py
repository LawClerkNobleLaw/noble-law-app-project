"""
Tests for db.py's flagging and client functions — flag_bill/archive_flagged_bill
(and how they touch the shared watchlist underneath), create_client/
get_client/update_client/delete_client, and link_bill_to_client's
ownership/flagged-first validation.
"""

import pytest

import db
from conftest import insert_bill, insert_user


# ── flag_bill / archive_flagged_bill ────────────────────────────────

def test_flag_bill_adds_to_flagged_and_watchlist(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)

    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    assert bill_id in db.list_flagged_bill_ids_for_user(conn, user_id)
    assert bill_id in db.list_watchlist_bill_ids(conn)


def test_flag_bill_is_idempotent(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)

    db.flag_bill(conn, user_id, bill_id)
    db.flag_bill(conn, user_id, bill_id)  # flagging twice shouldn't error or duplicate
    conn.commit()

    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM flagged_bills WHERE user_id = ? AND bill_id = ?",
        (user_id, bill_id),
    ).fetchone()
    assert rows["n"] == 1


def test_flagging_by_two_users_keeps_bill_on_watchlist_until_both_archive(conn):
    bill_id = insert_bill(conn)
    user_a = insert_user(conn, email="a@example.com")
    user_b = insert_user(conn, email="b@example.com")

    db.flag_bill(conn, user_a, bill_id)
    db.flag_bill(conn, user_b, bill_id)
    conn.commit()

    db.archive_flagged_bill(conn, user_a, bill_id)
    conn.commit()
    # user_b still has it actively flagged — the daily refresh job should
    # keep refreshing this bill, so it must still be on the shared watchlist.
    assert bill_id in db.list_watchlist_bill_ids(conn)

    db.archive_flagged_bill(conn, user_b, bill_id)
    conn.commit()
    # Nobody has it actively flagged anymore — no point spending daily
    # LegiScan quota on it.
    assert bill_id not in db.list_watchlist_bill_ids(conn)


def test_archive_flagged_bill_preserves_bill_client_links_and_notes(conn):
    """P1-16: archiving is not deleting. The old unflag_bill DELETEd the
    flagged_bills row and every bill_client_links row hanging off it —
    this is the fix, and the whole point of the finding."""
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()
    db.link_bill_to_client(conn, user_id, bill_id, client_id, "support")
    db.set_bill_notes(conn, user_id, bill_id, "Testified in support, June hearing.")
    conn.commit()

    db.archive_flagged_bill(conn, user_id, bill_id)
    conn.commit()

    links = conn.execute(
        "SELECT COUNT(*) AS n FROM bill_client_links WHERE user_id = ? AND bill_id = ?",
        (user_id, bill_id),
    ).fetchone()
    assert links["n"] == 1
    row = conn.execute(
        "SELECT notes, archived_at FROM flagged_bills WHERE user_id = ? AND bill_id = ?",
        (user_id, bill_id),
    ).fetchone()
    assert row["notes"] == "Testified in support, June hearing."
    assert row["archived_at"] is not None


def test_archive_flagged_bill_hides_it_from_the_active_list(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    db.archive_flagged_bill(conn, user_id, bill_id)
    conn.commit()

    assert bill_id not in db.list_flagged_bill_ids_for_user(conn, user_id)
    archived = db.list_archived_bills(conn, user_id)
    assert [r["bill_id"] for r in archived] == [bill_id]


def test_archived_bill_no_longer_reads_as_tracked_in_search(conn):
    """tracking_for_bills is what search results (P2-27) use to mark a row
    already flagged. An archived bill isn't being tracked anymore, so it
    shouldn't come back annotated as if it still were."""
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    db.archive_flagged_bill(conn, user_id, bill_id)
    conn.commit()

    assert db.tracking_for_bills(conn, user_id, [bill_id]) == {}


def test_flag_bill_restores_an_archived_bill(conn):
    """Re-flagging is the whole restore path (see flag_bill's ON CONFLICT
    clause) — no separate "restore" function, because there's nothing to
    do besides clear archived_at back to NULL."""
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()
    db.link_bill_to_client(conn, user_id, bill_id, client_id, "oppose")
    conn.commit()
    db.archive_flagged_bill(conn, user_id, bill_id)
    conn.commit()

    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    assert bill_id in db.list_flagged_bill_ids_for_user(conn, user_id)
    assert db.list_archived_bills(conn, user_id) == []
    links = conn.execute(
        "SELECT position FROM bill_client_links WHERE user_id = ? AND bill_id = ?",
        (user_id, bill_id),
    ).fetchone()
    assert links["position"] == "oppose"


def test_archive_flagged_bill_scoped_to_the_right_user(conn):
    bill_id = insert_bill(conn)
    user_a = insert_user(conn, email="a@example.com")
    user_b = insert_user(conn, email="b@example.com")
    db.flag_bill(conn, user_a, bill_id)
    db.flag_bill(conn, user_b, bill_id)
    conn.commit()

    db.archive_flagged_bill(conn, user_a, bill_id)
    conn.commit()

    assert bill_id not in db.list_flagged_bill_ids_for_user(conn, user_a)
    assert bill_id in db.list_flagged_bill_ids_for_user(conn, user_b)


# ── create_client / get_client / update_client / delete_client ─────

def test_create_client_then_get_client(conn):
    user_id = insert_user(conn)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp", "interests": "widgets"})
    conn.commit()

    client = db.get_client(conn, user_id, client_id)
    assert client["name"] == "Acme Corp"
    assert client["interests"] == "widgets"


def test_get_client_scoped_to_owner(conn):
    user_a = insert_user(conn, email="a@example.com")
    user_b = insert_user(conn, email="b@example.com")
    client_id = db.create_client(conn, user_a, {"name": "Acme Corp"})
    conn.commit()

    # user_b guessing/incrementing user_a's client id must not work.
    assert db.get_client(conn, user_b, client_id) is None
    assert db.get_client(conn, user_a, client_id) is not None


def test_list_clients_only_returns_the_caller_own(conn):
    user_a = insert_user(conn, email="a@example.com")
    user_b = insert_user(conn, email="b@example.com")
    db.create_client(conn, user_a, {"name": "Acme Corp"})
    db.create_client(conn, user_b, {"name": "Other Co"})
    conn.commit()

    names = {c["name"] for c in db.list_clients(conn, user_a)}
    assert names == {"Acme Corp"}


def test_update_client_changes_fields(conn):
    user_id = insert_user(conn)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()

    db.update_client(conn, user_id, client_id, {"name": "Acme Corp International", "interests": "gadgets"})
    conn.commit()

    client = db.get_client(conn, user_id, client_id)
    assert client["name"] == "Acme Corp International"
    assert client["interests"] == "gadgets"


def test_client_bus_phone_round_trips_through_create_and_update(conn):
    # bus_phone was added alongside the Add Client form's CAL-ACCESS
    # autofill (see CLIENTS_BODY) — no CAL-ACCESS filer record has a
    # phone number at all, so this is always a plain manually-entered
    # field, but it still needs to actually persist/reload like every
    # other client field.
    user_id = insert_user(conn)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp", "bus_phone": "(916) 555-0100"})
    conn.commit()

    assert db.get_client(conn, user_id, client_id)["bus_phone"] == "(916) 555-0100"

    db.update_client(conn, user_id, client_id, {"name": "Acme Corp", "bus_phone": "(916) 555-0199"})
    conn.commit()

    assert db.get_client(conn, user_id, client_id)["bus_phone"] == "(916) 555-0199"


def test_update_client_scoped_to_owner_is_a_no_op_for_other_users(conn):
    user_a = insert_user(conn, email="a@example.com")
    user_b = insert_user(conn, email="b@example.com")
    client_id = db.create_client(conn, user_a, {"name": "Acme Corp"})
    conn.commit()

    db.update_client(conn, user_b, client_id, {"name": "Hijacked Name"})
    conn.commit()

    assert db.get_client(conn, user_a, client_id)["name"] == "Acme Corp"


def test_delete_client_removes_it(conn):
    user_id = insert_user(conn)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()

    db.delete_client(conn, user_id, client_id)
    conn.commit()

    assert db.get_client(conn, user_id, client_id) is None


def test_delete_client_with_bill_links_does_not_raise(conn):
    # Real bug this guards against (see delete_client's own docstring):
    # deleting a client that's actually linked to a bill used to crash
    # with an unhandled FOREIGN KEY constraint error.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()
    db.link_bill_to_client(conn, user_id, bill_id, client_id, "support")
    conn.commit()

    db.delete_client(conn, user_id, client_id)  # must not raise
    conn.commit()

    assert db.get_client(conn, user_id, client_id) is None
    links = conn.execute(
        "SELECT COUNT(*) AS n FROM bill_client_links WHERE client_id = ?", (client_id,)
    ).fetchone()
    assert links["n"] == 0


# ── link_bill_to_client ──────────────────────────────────────────────

def test_link_bill_to_client_requires_flagging_first(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)  # note: never flagged
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()

    with pytest.raises(ValueError):
        db.link_bill_to_client(conn, user_id, bill_id, client_id, "support")


def test_link_bill_to_client_requires_an_active_flag_not_an_archived_one(conn):
    """An archived flag still exists (P1-16 doesn't delete it), but a new
    client can't be assigned to a bill nobody's actively tracking anymore
    — restore it first."""
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    conn.commit()
    db.archive_flagged_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()

    with pytest.raises(ValueError):
        db.link_bill_to_client(conn, user_id, bill_id, client_id, "support")


def test_link_bill_to_client_requires_owning_the_client(conn):
    user_a = insert_user(conn, email="a@example.com")
    user_b = insert_user(conn, email="b@example.com")
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_a, bill_id)
    conn.commit()
    client_id = db.create_client(conn, user_b, {"name": "Someone Else's Client"})
    conn.commit()

    with pytest.raises(ValueError):
        db.link_bill_to_client(conn, user_a, bill_id, client_id, "support")


def test_link_bill_to_client_rejects_invalid_position(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()

    with pytest.raises(ValueError):
        db.link_bill_to_client(conn, user_id, bill_id, client_id, "not-a-real-position")


def test_link_bill_to_client_then_change_position(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()

    db.link_bill_to_client(conn, user_id, bill_id, client_id, "watch")
    conn.commit()
    bills = db.get_client_bills(conn, user_id, client_id)
    assert bills[0]["position"] == "watch"

    # Calling it again for the same (bill, client) changes the
    # existing link's position instead of erroring or duplicating.
    db.link_bill_to_client(conn, user_id, bill_id, client_id, "oppose")
    conn.commit()
    bills = db.get_client_bills(conn, user_id, client_id)
    assert len(bills) == 1
    assert bills[0]["position"] == "oppose"


def test_get_client_bills_includes_url(conn):
    # Regression test: get_client_bills previously omitted bills.url
    # entirely, even though it's a plain column already selected by
    # list_flagged_bills for the /flagged page's own "View" link. That
    # silently starved the client detail page's bill rows of anywhere
    # to send a "View on LegiScan" link, even when the bill row itself
    # had a url on file.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn, url="https://legiscan.com/CA/bill/SB1/2026")
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()

    db.link_bill_to_client(conn, user_id, bill_id, client_id, "watch")
    conn.commit()
    bills = db.get_client_bills(conn, user_id, client_id)
    assert bills[0]["url"] == "https://legiscan.com/CA/bill/SB1/2026"


def test_unlink_bill_from_client(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()
    db.link_bill_to_client(conn, user_id, bill_id, client_id, "support")
    conn.commit()

    db.unlink_bill_from_client(conn, user_id, bill_id, client_id)
    conn.commit()
    assert db.get_client_bills(conn, user_id, client_id) == []


# ── get_bill_report row ordering ────────────────────────────────────

def test_get_bill_report_status_history_is_newest_first(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    conn.executemany(
        "INSERT INTO bill_status_history (bill_id, date, chamber, action) VALUES (?, ?, ?, ?)",
        [
            (bill_id, "2026-01-05", "Senate", "Introduced"),
            (bill_id, "2026-03-20", "Assembly", "Passed"),
            (bill_id, "2026-02-10", "Senate", "Amended"),
        ],
    )
    conn.commit()

    report = db.get_bill_report(conn, user_id, bill_id)

    assert [h["date"] for h in report["history"]] == ["2026-03-20", "2026-02-10", "2026-01-05"]


def test_get_bill_report_upcoming_hearings_stay_soonest_first(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    conn.executemany(
        "INSERT INTO bill_hearings (bill_id, event_type, date, time, location, description) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (bill_id, "Hearing", "2099-03-20", "10:00", "Room 1", "Later hearing"),
            (bill_id, "Hearing", "2099-01-05", "09:00", "Room 2", "Soonest hearing"),
            (bill_id, "Hearing", "2099-02-10", "14:00", "Room 3", "Middle hearing"),
        ],
    )
    conn.commit()

    report = db.get_bill_report(conn, user_id, bill_id)

    assert [h["date"] for h in report["upcoming_hearings"]] == ["2099-01-05", "2099-02-10", "2099-03-20"]


def test_get_bill_report_flagged_reflects_whether_this_user_flagged_it(conn):
    # /api/report now upserts a bill straight from LegiScan on first
    # view (see app.py) — a bill can exist in `bills` without this user
    # having flagged it at all, which is exactly what happens when the
    # merged /lookup search sends someone to a report for a bill nobody
    # has flagged yet. report['flagged'] is how the page tells that
    # apart from "flagged, no client assigned" (an empty
    # assigned_clients list alone can't distinguish the two).
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)

    report = db.get_bill_report(conn, user_id, bill_id)
    assert report["flagged"] is False

    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    report = db.get_bill_report(conn, user_id, bill_id)
    assert report["flagged"] is True


# ── list_flagged_bills' latest_activity_date ────────────────────────

def test_list_flagged_bills_includes_latest_activity_date(conn):
    # Backs the /flagged page's "Most recent activity" sort option (see
    # FLAGGED_BODY) — the newest bill_status_history date for each
    # flagged bill, not bills.status_date (LegiScan's own reported
    # status date, a different column entirely).
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    conn.executemany(
        "INSERT INTO bill_status_history (bill_id, date, chamber, action) VALUES (?, ?, ?, ?)",
        [
            (bill_id, "2026-01-05", "Senate", "Introduced"),
            (bill_id, "2026-03-20", "Assembly", "Passed"),
        ],
    )
    conn.commit()

    rows = db.list_flagged_bills(conn, user_id)

    assert len(rows) == 1
    assert rows[0]["latest_activity_date"] == "2026-03-20"


def test_list_flagged_bills_latest_activity_date_is_none_without_history(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    rows = db.list_flagged_bills(conn, user_id)

    assert rows[0]["latest_activity_date"] is None


# ── per-user notes on a flagged bill ────────────────────────────────

def test_bill_notes_round_trip_through_the_report(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    assert db.get_bill_report(conn, user_id, bill_id)["notes"] == ""

    db.set_bill_notes(conn, user_id, bill_id, "Met with the author's office Tuesday.")
    conn.commit()

    assert db.get_bill_report(conn, user_id, bill_id)["notes"] == "Met with the author's office Tuesday."


def test_bill_notes_are_private_to_each_user(conn):
    # Stored against the flag, not the bill — two firms tracking the same
    # bill must not see each other's working notes.
    mine = insert_user(conn)
    theirs = insert_user(conn, email="someone@example.com")
    bill_id = insert_bill(conn)
    db.flag_bill(conn, mine, bill_id)
    db.flag_bill(conn, theirs, bill_id)
    db.set_bill_notes(conn, mine, bill_id, "Ours: oppose unless amended.")
    conn.commit()

    assert db.get_bill_report(conn, mine, bill_id)["notes"] == "Ours: oppose unless amended."
    assert db.get_bill_report(conn, theirs, bill_id)["notes"] == ""


def test_bill_notes_require_the_bill_to_be_flagged(conn):
    # There's no per-user row to hang a note on otherwise, and accepting
    # the text just to drop it would be worse than refusing it.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    conn.commit()

    with pytest.raises(ValueError):
        db.set_bill_notes(conn, user_id, bill_id, "Never saved.")


def test_bill_notes_clear_back_to_empty(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    db.set_bill_notes(conn, user_id, bill_id, "Something.")
    conn.commit()

    db.set_bill_notes(conn, user_id, bill_id, "")
    conn.commit()

    assert db.get_bill_report(conn, user_id, bill_id)["notes"] == ""


def test_archiving_and_restoring_keeps_the_notes(conn):
    # P1-16: the note lives on the flag, and archiving no longer deletes
    # that row — so unlike the old unflag_bill, a note survives an
    # archive/restore round trip instead of coming back empty.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    db.set_bill_notes(conn, user_id, bill_id, "Working note.")
    conn.commit()

    db.archive_flagged_bill(conn, user_id, bill_id)
    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    assert db.get_bill_report(conn, user_id, bill_id)["notes"] == "Working note."


def test_report_upcoming_hearings_use_california_today(conn):
    # Was date('now') — UTC, which rolls over mid-afternoon Pacific and
    # dropped a hearing happening this afternoon out of "upcoming" while
    # it was still ahead of the user. Must match the calendar's own cut.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    today = db.today_in_california()
    conn.execute(
        "INSERT INTO bill_hearings (bill_id, event_type, date, time) VALUES (?, 'Hearing', ?, '13:30')",
        (bill_id, today),
    )
    conn.commit()

    report = db.get_bill_report(conn, user_id, bill_id)

    assert [h["date"] for h in report["upcoming_hearings"]] == [today]


# ── the hearing calendar's upcoming/past split ──────────────────────
#
# Every test here passes `today` explicitly rather than letting the
# function reach for the real clock, so the fixtures can sit either side
# of a fixed date and the suite doesn't start failing when the dates it
# hard-codes drift into the past.

def _insert_hearings(conn, bill_id, rows):
    conn.executemany(
        "INSERT INTO bill_hearings (bill_id, event_type, date, time, location, description) "
        "VALUES (?, 'Hearing', ?, ?, 'Room 1', NULL)",
        [(bill_id, date, time) for date, time in rows],
    )
    conn.commit()


def test_calendar_splits_hearings_around_today(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, bill_id, [
        ("2026-08-13", "09:30"),
        ("2026-09-10", "13:30"),
        ("2026-04-02", "08:30"),
    ])

    result = db.list_hearings_for_flagged_bills(conn, user_id, today="2026-09-02")

    assert [h["date"] for h in result["upcoming"]] == ["2026-09-10"]
    assert [h["date"] for h in result["past"]] == ["2026-08-13", "2026-04-02"]


def test_calendar_counts_a_hearing_today_as_upcoming(conn):
    # The boundary is >=, not >: a hearing at 1:30pm is still something to
    # prepare for at 8am the same morning.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, bill_id, [("2026-09-02", "13:30")])

    result = db.list_hearings_for_flagged_bills(conn, user_id, today="2026-09-02")

    assert [h["date"] for h in result["upcoming"]] == ["2026-09-02"]
    assert result["past"] == []


def test_calendar_sorts_each_half_in_its_own_direction(conn):
    # Upcoming runs soonest-first, past runs newest-first, and the day
    # grouping in calendar_body.html buckets *consecutive* same-date rows
    # — so same-day hearings must stay adjacent, ordered by time.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, bill_id, [
        ("2026-09-10", "14:00"),
        ("2026-09-10", "09:00"),
        ("2026-09-20", "10:00"),
        ("2026-08-01", "10:00"),
        ("2026-08-20", "10:00"),
    ])

    result = db.list_hearings_for_flagged_bills(conn, user_id, today="2026-09-02")

    assert [(h["date"], h["time"]) for h in result["upcoming"]] == [
        ("2026-09-10", "09:00"), ("2026-09-10", "14:00"), ("2026-09-20", "10:00"),
    ]
    assert [h["date"] for h in result["past"]] == ["2026-08-20", "2026-08-01"]


def test_calendar_treats_an_undated_hearing_as_upcoming(conn):
    # LegiScan saying a hearing exists without saying when is news, not
    # history — it sorts to the top of upcoming rather than into the past.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, bill_id, [("2026-09-10", "13:30"), (None, None)])

    result = db.list_hearings_for_flagged_bills(conn, user_id, today="2026-09-02")

    assert [h["date"] for h in result["upcoming"]] == [None, "2026-09-10"]
    assert result["past"] == []


def test_calendar_reports_flagged_count_even_with_no_hearings(conn):
    # Drives the empty state that names what was actually checked ("No
    # upcoming hearings on your 2 flagged bills"), so it has to be right
    # precisely when there are no hearing rows to count from.
    user_id = insert_user(conn)
    db.flag_bill(conn, user_id, insert_bill(conn, bill_id=1, bill_number="SB1"))
    db.flag_bill(conn, user_id, insert_bill(conn, bill_id=2, bill_number="SB2"))
    conn.commit()

    result = db.list_hearings_for_flagged_bills(conn, user_id, today="2026-09-02")

    assert result["flagged_count"] == 2
    assert result["upcoming"] == []
    assert result["past"] == []


def test_calendar_only_covers_the_callers_own_flagged_bills(conn):
    mine = insert_user(conn)
    theirs = insert_user(conn, email="someone@example.com")
    my_bill = insert_bill(conn, bill_id=1, bill_number="SB1")
    their_bill = insert_bill(conn, bill_id=2, bill_number="SB2")
    db.flag_bill(conn, mine, my_bill)
    db.flag_bill(conn, theirs, their_bill)
    _insert_hearings(conn, my_bill, [("2026-09-10", "09:00")])
    _insert_hearings(conn, their_bill, [("2026-09-11", "09:00")])

    result = db.list_hearings_for_flagged_bills(conn, mine, today="2026-09-02")

    assert [h["bill_number"] for h in result["upcoming"]] == ["SB1"]
    assert result["flagged_count"] == 1


# ── list_flagged_bills' next_hearing ────────────────────────────────

def test_flagged_bill_next_hearing_is_the_soonest_still_to_come(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, bill_id, [
        ("2026-09-20", "10:00"),
        ("2026-09-10", "13:30"),
        ("2026-08-01", "09:00"),  # past — must not win
    ])

    rows = db.list_flagged_bills(conn, user_id, today="2026-09-02")

    assert rows[0]["next_hearing"]["date"] == "2026-09-10"
    assert rows[0]["next_hearing"]["days_until"] == 8


def test_flagged_bill_next_hearing_is_none_when_only_past_ones_exist(conn):
    # The column has to say "No date set" rather than show a stale date —
    # a bill whose only hearing already happened has no next action.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, bill_id, [("2026-08-01", "09:00")])

    rows = db.list_flagged_bills(conn, user_id, today="2026-09-02")

    assert rows[0]["next_hearing"] is None


def test_flagged_bill_hearing_today_counts_with_zero_days_until(conn):
    # Drives the "Today" urgency chip — and 0 has to survive the round
    # trip as 0, not be flattened into a falsy no-hearing case.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, bill_id, [("2026-09-02", "13:30")])

    rows = db.list_flagged_bills(conn, user_id, today="2026-09-02")

    assert rows[0]["next_hearing"]["days_until"] == 0


def test_flagged_bill_undated_hearing_does_not_become_a_next_action(conn):
    # Unlike the calendar, which shows undated hearings: a hearing with
    # no date can't answer "when do I have to act".
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, bill_id, [(None, None)])

    rows = db.list_flagged_bills(conn, user_id, today="2026-09-02")

    assert rows[0]["next_hearing"] is None


def test_flagged_bills_next_hearing_does_not_leak_between_bills(conn):
    # One query covers every flagged bill at once, so the grouping is the
    # part worth pinning: each bill must get its own soonest hearing.
    user_id = insert_user(conn)
    first = insert_bill(conn, bill_id=1, bill_number="SB1")
    second = insert_bill(conn, bill_id=2, bill_number="SB2")
    third = insert_bill(conn, bill_id=3, bill_number="SB3")
    for bill_id in (first, second, third):
        db.flag_bill(conn, user_id, bill_id)
    _insert_hearings(conn, first, [("2026-09-25", "10:00")])
    _insert_hearings(conn, second, [("2026-09-05", "10:00"), ("2026-09-30", "10:00")])
    conn.commit()

    by_number = {r["bill_number"]: r for r in db.list_flagged_bills(conn, user_id, today="2026-09-02")}

    assert by_number["SB1"]["next_hearing"]["date"] == "2026-09-25"
    assert by_number["SB2"]["next_hearing"]["date"] == "2026-09-05"
    assert by_number["SB3"]["next_hearing"] is None


def test_days_between_counts_calendar_days_and_survives_junk(conn):
    assert db._days_between("2026-09-02", "2026-09-02") == 0
    assert db._days_between("2026-09-02", "2026-09-03") == 1
    assert db._days_between("2026-09-02", "2026-08-30") == -3
    # Across a month boundary and a leap day, where naive arithmetic slips.
    assert db._days_between("2026-08-30", "2026-09-02") == 3
    assert db._days_between("2028-02-27", "2028-03-01") == 3
    assert db._days_between("2026-09-02", None) is None
    assert db._days_between("2026-09-02", "not-a-date") is None


# ── change detection and bill_change_events ─────────────────────────

def _synced_bill(conn, bill_id=1, status_code=1, status_label="Introduced"):
    """A bill already in the DB, so snapshot_bill_state has something to
    return — a first sighting deliberately isn't a change."""
    insert_bill(conn, bill_id=bill_id)
    conn.execute(
        "UPDATE bills SET status_code = ?, status_label = ?, status_date = '2026-01-05' WHERE id = ?",
        (status_code, status_label, bill_id),
    )
    conn.commit()
    return bill_id


def test_diff_reports_a_status_change_with_its_parts_split_out(conn):
    bill_id = _synced_bill(conn)
    before = db.snapshot_bill_state(conn, bill_id)

    changes = db.diff_bill_state(before, {
        "status_code": 4, "status_label": "Enrolled", "status_date": "2026-08-28",
    })

    assert len(changes) == 1
    assert changes[0]["change_type"] == "status"
    # summary drives the chip, event_date its date, description the email.
    assert changes[0]["summary"] == "Enrolled"
    assert changes[0]["event_date"] == "2026-08-28"
    assert "Introduced" in changes[0]["description"]
    assert "Enrolled" in changes[0]["description"]


def test_diff_of_a_bill_never_seen_before_is_not_a_change(conn):
    # snapshot_bill_state returns None for an unknown bill, and a first
    # sighting must not land in the digest or the change log as news.
    assert db.snapshot_bill_state(conn, 999) is None
    assert db.diff_bill_state(None, {"status_code": 4, "status_label": "Enrolled"}) == []


def test_diff_reports_a_new_hearing_and_a_new_amendment(conn):
    bill_id = _synced_bill(conn)
    before = db.snapshot_bill_state(conn, bill_id)

    changes = db.diff_bill_state(before, {
        "status_code": 1, "status_label": "Introduced",
        "amendments": [{"amendment_id": 77, "chamber": "Senate", "date": "2026-06-25"}],
        "hearings": [{"date": "2026-09-10", "time": "13:30", "event_type": "Hearing"}],
    })

    by_type = {c["change_type"]: c for c in changes}
    assert by_type["amendment"]["summary"] == "Amended"
    assert by_type["amendment"]["event_date"] == "2026-06-25"
    assert by_type["hearing"]["summary"] == "Hearing set"
    assert by_type["hearing"]["event_date"] == "2026-09-10"


def test_diff_ignores_amendments_and_hearings_already_on_record(conn):
    # The refresh runs daily against a full re-fetch, so "unchanged" is
    # the overwhelmingly common case — re-reporting it would make every
    # digest fire every day.
    bill_id = _synced_bill(conn)
    conn.execute(
        "INSERT INTO bill_amendments (bill_id, amendment_id, chamber, date) VALUES (?, 77, 'Senate', '2026-06-25')",
        (bill_id,),
    )
    conn.execute(
        "INSERT INTO bill_hearings (bill_id, event_type, date, time) VALUES (?, 'Hearing', '2026-09-10', '13:30')",
        (bill_id,),
    )
    conn.commit()
    before = db.snapshot_bill_state(conn, bill_id)

    changes = db.diff_bill_state(before, {
        "status_code": 1, "status_label": "Introduced",
        "amendments": [{"amendment_id": 77, "chamber": "Senate", "date": "2026-06-25"}],
        "hearings": [{"date": "2026-09-10", "time": "13:30", "event_type": "Hearing"}],
    })

    assert changes == []


def test_record_bill_changes_appends_and_is_a_no_op_when_empty(conn):
    bill_id = _synced_bill(conn)

    assert db.record_bill_changes(conn, bill_id, []) == 0
    written = db.record_bill_changes(conn, bill_id, [
        {"change_type": "status", "summary": "Enrolled",
         "description": "Status changed.", "event_date": "2026-08-28"},
    ], detected_at="2026-08-29T06:00:00Z")
    conn.commit()

    assert written == 1
    rows = conn.execute("SELECT * FROM bill_change_events WHERE bill_id = ?", (bill_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["summary"] == "Enrolled"
    assert rows[0]["event_date"] == "2026-08-28"
    assert rows[0]["detected_at"] == "2026-08-29T06:00:00Z"


def test_flagged_bill_last_change_is_the_most_recently_detected_one(conn):
    user_id = insert_user(conn)
    bill_id = _synced_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    db.record_bill_changes(conn, bill_id, [
        {"change_type": "amendment", "summary": "Amended",
         "description": "New amendment.", "event_date": "2026-06-25"},
    ], detected_at="2026-06-26T06:00:00Z")
    db.record_bill_changes(conn, bill_id, [
        {"change_type": "status", "summary": "Enrolled",
         "description": "Status changed.", "event_date": "2026-08-28"},
    ], detected_at="2026-08-29T06:00:00Z")
    conn.commit()

    rows = db.list_flagged_bills(conn, user_id, today="2026-09-02")

    assert rows[0]["last_change"]["summary"] == "Enrolled"
    assert rows[0]["last_change"]["event_date"] == "2026-08-28"


def test_flagged_bill_last_change_ranks_a_status_move_over_a_hearing(conn):
    # One refresh commonly finds several changes at the same instant, so
    # time alone can't pick the headline — without a rank it came down to
    # insertion order, and a scheduled hearing (whose event_date is in the
    # FUTURE) would win and print a next-month date under "Last change".
    user_id = insert_user(conn)
    bill_id = _synced_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    db.record_bill_changes(conn, bill_id, [
        {"change_type": "status", "summary": "Enrolled",
         "description": "Status changed.", "event_date": "2026-08-28"},
        {"change_type": "hearing", "summary": "Hearing set",
         "description": "Hearing scheduled.", "event_date": "2026-09-30"},
    ], detected_at="2026-08-29T06:00:00Z")
    conn.commit()

    change = db.list_flagged_bills(conn, user_id, today="2026-09-02")[0]["last_change"]

    assert change["summary"] == "Enrolled"
    # The other change in that run is reported, not silently dropped.
    assert change["also_count"] == 1


def test_flagged_bill_last_change_does_not_count_earlier_runs_as_also(conn):
    # also_count means "more in the same run", not "more ever" — an older
    # change is history, not part of today's headline.
    user_id = insert_user(conn)
    bill_id = _synced_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    db.record_bill_changes(conn, bill_id, [
        {"change_type": "amendment", "summary": "Amended",
         "description": "New amendment.", "event_date": "2026-06-25"},
    ], detected_at="2026-06-26T06:00:00Z")
    db.record_bill_changes(conn, bill_id, [
        {"change_type": "status", "summary": "Enrolled",
         "description": "Status changed.", "event_date": "2026-08-28"},
    ], detected_at="2026-08-29T06:00:00Z")
    conn.commit()

    change = db.list_flagged_bills(conn, user_id, today="2026-09-02")[0]["last_change"]

    assert change["summary"] == "Enrolled"
    assert change["also_count"] == 0


def test_flagged_bill_last_change_is_none_before_any_refresh_has_seen_it(conn):
    # The state every existing database is in the day this ships: the
    # column has to fall back rather than render an empty cell.
    user_id = insert_user(conn)
    bill_id = _synced_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    conn.commit()

    rows = db.list_flagged_bills(conn, user_id, today="2026-09-02")

    assert rows[0]["last_change"] is None


def test_flagged_bills_last_change_does_not_leak_between_bills(conn):
    user_id = insert_user(conn)
    first = _synced_bill(conn, bill_id=1)
    second = _synced_bill(conn, bill_id=2)
    conn.execute("UPDATE bills SET bill_number = 'SB2' WHERE id = 2")
    for bill_id in (first, second):
        db.flag_bill(conn, user_id, bill_id)
    db.record_bill_changes(conn, first, [
        {"change_type": "status", "summary": "Enrolled",
         "description": "Status changed.", "event_date": "2026-08-28"},
    ], detected_at="2026-08-29T06:00:00Z")
    conn.commit()

    by_id = {r["bill_id"]: r for r in db.list_flagged_bills(conn, user_id, today="2026-09-02")}

    assert by_id[first]["last_change"]["summary"] == "Enrolled"
    assert by_id[second]["last_change"] is None


def test_today_in_california_is_an_iso_date(conn):
    # Compared straight against bill_hearings.date, which is TEXT in ISO
    # form — the format is the contract, so it's worth pinning.
    today = db.today_in_california()

    assert len(today) == 10
    assert today[4] == "-" and today[7] == "-"
