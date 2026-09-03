"""
Tests for the append-only record of a client's position on a bill
(P1-15): db.link_bill_to_client / unlink_bill_from_client writing to
position_history, and db.list_position_history reading it back.

The question this table exists to answer is "what was our position when
we testified in June?", so the properties worth pinning are the ones that
would make that answer wrong: a change that doesn't get recorded, a
record that gets overwritten, or a record that disappears along with the
thing it describes.
"""

import db
from conftest import insert_bill, insert_user


def _setup(conn, position="watch"):
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    db.flag_bill(conn, user_id, 1)
    client_id = db.create_client(conn, user_id, {"name": "Anthropic PBC"})
    db.link_bill_to_client(conn, user_id, 1, client_id, position)
    conn.commit()
    return user_id, client_id


def test_first_assignment_is_recorded_with_no_previous_position(conn):
    user_id, client_id = _setup(conn, "support")

    history = db.list_position_history(conn, user_id, bill_id=1)
    assert len(history) == 1
    assert history[0]["from_position"] is None
    assert history[0]["to_position"] == "support"
    assert history[0]["client_name"] == "Anthropic PBC"
    assert history[0]["changed_by"] == user_id


def test_a_flip_records_both_ends_of_it(conn):
    user_id, client_id = _setup(conn, "support")
    db.link_bill_to_client(conn, user_id, 1, client_id, "oppose")
    conn.commit()

    history = db.list_position_history(conn, user_id, bill_id=1)
    assert len(history) == 2
    assert (history[0]["from_position"], history[0]["to_position"]) == ("support", "oppose")


def test_resaving_the_same_position_records_nothing(conn):
    """The user picked the value that was already selected. A log that
    fills up with non-events is a log nobody reads."""
    user_id, client_id = _setup(conn, "watch")
    db.link_bill_to_client(conn, user_id, 1, client_id, "watch")
    conn.commit()

    assert len(db.list_position_history(conn, user_id, bill_id=1)) == 1


def test_changing_the_effective_date_alone_is_a_recorded_change(conn):
    """Correcting when a position took effect changes the answer to the
    question this table exists for, so it belongs in the record."""
    user_id, client_id = _setup(conn, "oppose")
    db.link_bill_to_client(conn, user_id, 1, client_id, "oppose", effective_date="2026-06-12")
    conn.commit()

    history = db.list_position_history(conn, user_id, bill_id=1)
    assert len(history) == 2
    assert history[0]["effective_date"] == "2026-06-12"
    assert (history[0]["from_position"], history[0]["to_position"]) == ("oppose", "oppose")


def test_position_change_moves_the_effective_date_by_default(conn):
    """A new stance took effect when it was taken, not when the client
    was first added to the bill."""
    user_id, client_id = _setup(conn, "watch")
    db.link_bill_to_client(conn, user_id, 1, client_id, "watch", effective_date="2026-01-05")
    db.link_bill_to_client(conn, user_id, 1, client_id, "oppose")
    conn.commit()

    current = db.clients_for_bills(conn, user_id, [1])[1][0]
    assert current["position"] == "oppose"
    assert current["effective_date"] == db.today_in_california()


def test_removal_is_recorded_and_survives_the_link(conn):
    user_id, client_id = _setup(conn, "oppose")
    db.unlink_bill_from_client(conn, user_id, 1, client_id)
    conn.commit()

    assert db.clients_for_bills(conn, user_id, [1]) == {}
    history = db.list_position_history(conn, user_id, bill_id=1)
    assert history[0]["from_position"] == "oppose"
    assert history[0]["to_position"] is None


def test_history_outlives_the_client_itself(conn):
    """Deleting a client must neither be blocked by this table nor take
    the record with it — hence the stored name and the deliberate
    absence of a foreign key on client_id."""
    user_id, client_id = _setup(conn, "support")
    db.delete_client(conn, user_id, client_id)
    conn.commit()

    history = db.list_position_history(conn, user_id, bill_id=1)
    assert len(history) == 1
    assert history[0]["client_name"] == "Anthropic PBC"


def test_history_can_be_read_by_client_across_bills(conn):
    """The client record shows one client's stance on every bill, which
    is the same table read the other way round."""
    user_id, client_id = _setup(conn, "support")
    insert_bill(conn, bill_id=2, bill_number="SB813")
    db.flag_bill(conn, user_id, 2)
    db.link_bill_to_client(conn, user_id, 2, client_id, "oppose")
    conn.commit()

    history = db.list_position_history(conn, user_id, client_id=client_id)
    assert {(r["bill_number"], r["to_position"]) for r in history} == {
        ("SB1159", "support"), ("SB813", "oppose"),
    }


def test_history_is_scoped_to_the_user(conn):
    user_id, _ = _setup(conn, "support")
    other = insert_user(conn, email="other@example.com")

    assert db.list_position_history(conn, other) == []
    assert len(db.list_position_history(conn, user_id)) == 1


def test_an_undo_is_recorded_rather_than_erased(conn):
    """Putting a position back is another change, not the removal of one.
    The position genuinely was Oppose for as long as it was, and a log
    that quietly rewrites that is a log that can be argued with."""
    user_id, client_id = _setup(conn, "support")
    db.link_bill_to_client(conn, user_id, 1, client_id, "oppose")
    db.link_bill_to_client(conn, user_id, 1, client_id, "support")
    conn.commit()

    history = db.list_position_history(conn, user_id, bill_id=1)
    assert [(r["from_position"], r["to_position"]) for r in history] == [
        ("oppose", "support"), ("support", "oppose"), (None, "support"),
    ]
