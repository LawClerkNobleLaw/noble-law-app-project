"""
Tests for db.tracking_for_bills — the annotation that lets a search
results page say which bills the user already tracks (P2-27), and which
the inline flag control on those rows is keyed off (P1-10).

The input is a list of LegiScan bill_ids straight off a search response,
most of which this app has never seen. The properties that matter are
that unknown ids cost nothing and produce nothing, and that one user's
flags never leak into another's results.
"""

import db
from conftest import insert_bill, insert_user


def test_untracked_bills_are_simply_absent(conn):
    """A broad search is mostly bills nobody here has flagged. Those come
    back with no entry at all rather than a row of falses."""
    user_id = insert_user(conn)

    assert db.tracking_for_bills(conn, user_id, [901, 902, 903]) == {}


def test_a_flagged_bill_comes_back_with_its_clients(conn):
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    db.flag_bill(conn, user_id, 1)
    client_id = db.create_client(conn, user_id, {"name": "Anthropic PBC"})
    db.link_bill_to_client(conn, user_id, 1, client_id, "oppose")
    conn.commit()

    tracking = db.tracking_for_bills(conn, user_id, [1, 902])
    assert set(tracking) == {1}
    assert tracking[1]["flagged"] is True
    assert tracking[1]["clients"] == [
        {"id": client_id, "name": "Anthropic PBC", "position": "oppose",
         "effective_date": db.today_in_california()},
    ]


def test_flagged_with_no_client_still_counts_as_tracked(conn):
    """"Tracked, no client assigned" is a different answer from "not
    tracked", and the row says so."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)
    conn.commit()

    tracking = db.tracking_for_bills(conn, user_id, [1])
    assert tracking[1] == {"flagged": True, "clients": []}


def test_another_users_flags_do_not_appear(conn):
    user_id = insert_user(conn)
    other = insert_user(conn, email="other@example.com")
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, other, 1)
    conn.commit()

    assert db.tracking_for_bills(conn, user_id, [1]) == {}


def test_empty_id_list_does_no_work(conn):
    user_id = insert_user(conn)

    assert db.tracking_for_bills(conn, user_id, []) == {}
