"""
Tests for db.py's flagging and client functions — flag_bill/unflag_bill
(and how they touch the shared watchlist underneath), create_client/
get_client/update_client/delete_client, and link_bill_to_client's
ownership/flagged-first validation.
"""

import pytest

import db
from conftest import insert_bill, insert_user


# ── flag_bill / unflag_bill ─────────────────────────────────────────

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


def test_flagging_by_two_users_keeps_bill_on_watchlist_until_both_unflag(conn):
    bill_id = insert_bill(conn)
    user_a = insert_user(conn, email="a@example.com")
    user_b = insert_user(conn, email="b@example.com")

    db.flag_bill(conn, user_a, bill_id)
    db.flag_bill(conn, user_b, bill_id)
    conn.commit()

    db.unflag_bill(conn, user_a, bill_id)
    conn.commit()
    # user_b still has it flagged — the daily refresh job should keep
    # refreshing this bill, so it must still be on the shared watchlist.
    assert bill_id in db.list_watchlist_bill_ids(conn)

    db.unflag_bill(conn, user_b, bill_id)
    conn.commit()
    # Nobody has it flagged anymore — no point spending daily LegiScan
    # quota on it.
    assert bill_id not in db.list_watchlist_bill_ids(conn)


def test_unflag_bill_removes_bill_client_links(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()
    db.link_bill_to_client(conn, user_id, bill_id, client_id, "support")
    conn.commit()

    db.unflag_bill(conn, user_id, bill_id)
    conn.commit()

    links = conn.execute(
        "SELECT COUNT(*) AS n FROM bill_client_links WHERE user_id = ? AND bill_id = ?",
        (user_id, bill_id),
    ).fetchone()
    assert links["n"] == 0


def test_unflag_bill_scoped_to_the_right_user(conn):
    bill_id = insert_bill(conn)
    user_a = insert_user(conn, email="a@example.com")
    user_b = insert_user(conn, email="b@example.com")
    db.flag_bill(conn, user_a, bill_id)
    db.flag_bill(conn, user_b, bill_id)
    conn.commit()

    db.unflag_bill(conn, user_a, bill_id)
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
