"""
Tests for the client record's remaining gaps (P2-26): compensation,
contacts, notes, and that client's upcoming hearings.

Compensation is the load-bearing one. Form 615 doesn't exist in this app
yet, and when it does it will report a figure per client per period — so
what matters is that the column holds something a form can *report*: a
plain decimal it can sum, plus a separate basis it can convert. A field
that accepted "$5,000/month" or "5000-7500" would look filled in and be
useless to the form that reads it, which is worse than being empty.
"""

import db
from conftest import insert_bill, insert_user


def _client(conn, user_id, **fields):
    fields.setdefault("name", "University of California Student Association")
    return db.create_client(conn, user_id, fields)


def _firm(conn, *emails):
    org_id = db.create_organization(conn, "Noble Law")
    ids = []
    for email in emails:
        user_id = insert_user(conn, email=email)
        conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (org_id, user_id))
        ids.append(user_id)
    conn.commit()
    return ids


# ── Compensation ───────────────────────────────────────────────────────

def test_an_amount_is_stored_as_a_plain_decimal(conn):
    """No currency symbol, no separators — so a filing can sum it
    without re-parsing what a human happened to type."""
    user_id = insert_user(conn)
    client_id = _client(conn, user_id, compensation_amount="$5,000.00",
                        compensation_period="monthly")

    client = db.get_client(conn, user_id, client_id)

    assert client["compensation_amount"] == "5000.00"
    assert client["compensation_period"] == "monthly"


def test_the_basis_is_kept_separate_from_the_number(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id, compensation_amount="7500",
                        compensation_period="quarterly")

    client = db.get_client(conn, user_id, client_id)

    assert (client["compensation_amount"], client["compensation_period"]) == ("7500", "quarterly")


def test_an_amount_with_no_basis_is_recorded_as_monthly(conn):
    """Stated rather than left NULL, so a form reading this column never
    has to guess. A retainer is nearly always monthly."""
    user_id = insert_user(conn)
    client_id = _client(conn, user_id, compensation_amount="5000")

    assert db.get_client(conn, user_id, client_id)["compensation_period"] == "monthly"


def test_a_range_is_refused_by_name(conn):
    """"5000-7500" is a note, not an amount a filing can report. Storing
    it would put a non-number where a form will read a number."""
    user_id = insert_user(conn)

    for bad in ("5000-7500", "about 5k", "$150/hr", "5,00", "1.234"):
        try:
            _client(conn, user_id, name=f"Client {bad}", compensation_amount=bad)
        except ValueError as err:
            assert "amount" in str(err)
        else:
            raise AssertionError(f"expected {bad!r} to be refused")


def test_an_unknown_basis_is_refused(conn):
    user_id = insert_user(conn)

    try:
        _client(conn, user_id, compensation_amount="5000", compensation_period="fortnightly")
    except ValueError:
        pass
    else:
        raise AssertionError("expected a ValueError")


def test_blank_compensation_is_always_allowed(conn):
    """Not every relationship has a figure agreed, and every client that
    predates the column has none."""
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)

    client = db.get_client(conn, user_id, client_id)
    assert client["compensation_amount"] is None
    assert client["compensation_period"] is None


def test_clearing_the_amount_clears_the_basis(conn):
    """A basis with no number is not a fact about anything."""
    user_id = insert_user(conn)
    client_id = _client(conn, user_id, compensation_amount="5000", compensation_period="quarterly")

    db.update_client(conn, user_id, client_id, {"name": "UCSA", "compensation_amount": ""})

    client = db.get_client(conn, user_id, client_id)
    assert client["compensation_amount"] is None
    assert client["compensation_period"] is None


def test_every_field_is_settable_at_creation_and_afterwards(conn):
    """The drift this guards against is real: update_client() grew the
    three Form 601 columns and create_client() had to be edited to
    match. A column added to one and not the other is a field that
    silently can't be set at creation, or silently can't be edited."""
    user_id = insert_user(conn)
    filled = {key: "x" for key in db.CLIENT_FIELDS}
    filled["name"] = "UCSA"
    filled["compensation_amount"] = "5000"
    filled["compensation_period"] = "annual"
    filled["effective_date"] = "2026-01-01"

    created = db.get_client(conn, user_id, db.create_client(conn, user_id, filled))
    edited_id = db.create_client(conn, user_id, {"name": "Later"})
    db.update_client(conn, user_id, edited_id, filled)
    edited = db.get_client(conn, user_id, edited_id)

    for key in db.CLIENT_FIELDS:
        assert created[key] is not None, f"{key} could not be set at creation"
        assert edited[key] == created[key], f"{key} does not round-trip through update"


# ── Contacts ───────────────────────────────────────────────────────────

def test_a_client_can_hold_several_contacts(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)

    db.add_client_contact(conn, user_id, client_id, {"name": "Dana Reyes", "title": "General Counsel"})
    contacts = db.add_client_contact(conn, user_id, client_id, {"name": "Amara Osei", "title": "Policy Director"})

    assert [c["name"] for c in contacts] == ["Amara Osei", "Dana Reyes"]


def test_a_nameless_contact_is_refused(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)

    try:
        db.add_client_contact(conn, user_id, client_id, {"title": "GC", "email": "gc@ucsa.example"})
    except ValueError:
        pass
    else:
        raise AssertionError("a title and an email and nobody's name is not a person")


def test_exactly_one_contact_can_be_primary(conn):
    """SQLite can't express "at most one row per client with this flag",
    so this is enforced in db.py and has to be tested there."""
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    db.add_client_contact(conn, user_id, client_id, {"name": "Dana Reyes", "is_primary": True})
    contacts = db.add_client_contact(conn, user_id, client_id, {"name": "Amara Osei", "is_primary": True})

    assert [c["name"] for c in contacts if c["is_primary"]] == ["Amara Osei"]


def test_the_primary_contact_sorts_first(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    db.add_client_contact(conn, user_id, client_id, {"name": "Amara Osei"})
    db.add_client_contact(conn, user_id, client_id, {"name": "Zoe Tran"})
    contacts = db.list_client_contacts(conn, user_id, client_id)
    zoe = next(c["id"] for c in contacts if c["name"] == "Zoe Tran")

    contacts = db.set_primary_contact(conn, user_id, client_id, zoe)

    assert [c["name"] for c in contacts] == ["Zoe Tran", "Amara Osei"]


def test_promoting_a_contact_demotes_the_old_primary(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    db.add_client_contact(conn, user_id, client_id, {"name": "Dana Reyes", "is_primary": True})
    contacts = db.add_client_contact(conn, user_id, client_id, {"name": "Amara Osei"})
    amara = next(c["id"] for c in contacts if c["name"] == "Amara Osei")

    contacts = db.set_primary_contact(conn, user_id, client_id, amara)

    assert sum(1 for c in contacts if c["is_primary"]) == 1
    assert next(c["name"] for c in contacts if c["is_primary"]) == "Amara Osei"


def test_contacts_belong_to_the_firm(conn):
    mine, theirs = _firm(conn, "a@firm.com", "b@firm.com")
    client_id = _client(conn, mine)
    db.add_client_contact(conn, mine, client_id, {"name": "Dana Reyes"})

    assert [c["name"] for c in db.list_client_contacts(conn, theirs, client_id)] == ["Dana Reyes"]


def test_another_firms_contacts_are_invisible(conn):
    (mine,) = _firm(conn, "a@firm.com")
    (outsider,) = _firm(conn, "elsewhere@other.com")
    client_id = _client(conn, mine)
    db.add_client_contact(conn, mine, client_id, {"name": "Dana Reyes"})

    assert db.list_client_contacts(conn, outsider, client_id) == []


def test_a_contact_cannot_be_added_to_someone_elses_client(conn):
    (mine,) = _firm(conn, "a@firm.com")
    (outsider,) = _firm(conn, "elsewhere@other.com")
    client_id = _client(conn, mine)

    try:
        db.add_client_contact(conn, outsider, client_id, {"name": "Trojan Horse"})
    except ValueError:
        pass
    else:
        raise AssertionError("expected a ValueError")


def test_removing_a_contact_leaves_the_others(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    db.add_client_contact(conn, user_id, client_id, {"name": "Keep"})
    contacts = db.add_client_contact(conn, user_id, client_id, {"name": "Drop"})
    drop = next(c["id"] for c in contacts if c["name"] == "Drop")

    assert db.delete_client_contact(conn, user_id, client_id, drop) is True
    assert [c["name"] for c in db.list_client_contacts(conn, user_id, client_id)] == ["Keep"]


# ── Notes ──────────────────────────────────────────────────────────────

def test_notes_round_trip(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)

    assert db.set_client_notes(conn, user_id, client_id, "$150/hr up to 40hrs, then $120.") is True

    assert db.get_client(conn, user_id, client_id)["notes"] == "$150/hr up to 40hrs, then $120."


def test_cleared_notes_read_the_same_as_never_written(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    db.set_client_notes(conn, user_id, client_id, "something")

    db.set_client_notes(conn, user_id, client_id, "   ")

    assert db.get_client(conn, user_id, client_id)["notes"] is None


def test_notes_cannot_be_written_to_someone_elses_client(conn):
    (mine,) = _firm(conn, "a@firm.com")
    (outsider,) = _firm(conn, "elsewhere@other.com")
    client_id = _client(conn, mine)

    assert db.set_client_notes(conn, outsider, client_id, "hello") is False
    assert db.get_client(conn, mine, client_id)["notes"] is None


# ── That client's upcoming hearings ────────────────────────────────────

def _hearing(conn, bill_id, date, description="Senate Judiciary Hearing", time="09:30"):
    conn.execute(
        """INSERT INTO bill_hearings (bill_id, event_type, date, time, location, description)
           VALUES (?, 'Hearing', ?, ?, '1021 O Street, Room 2100', ?)""",
        (bill_id, date, time, description),
    )
    conn.commit()


def test_hearings_come_back_soonest_first_across_every_bill(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    for bill_id, number, date in ((1, "SB1", "2026-09-20"), (2, "SB2", "2026-09-10")):
        insert_bill(conn, bill_id=bill_id, bill_number=number)
        db.flag_bill(conn, user_id, bill_id)
        db.link_bill_to_client(conn, user_id, bill_id, client_id, position="oppose")
        _hearing(conn, bill_id, date)

    hearings = db.list_hearings_for_client(conn, user_id, client_id, today="2026-09-03")

    assert [h["bill_number"] for h in hearings] == ["SB2", "SB1"]
    assert [h["days_until"] for h in hearings] == [7, 17]


def test_a_hearing_already_past_is_not_upcoming(conn):
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)
    db.link_bill_to_client(conn, user_id, 1, client_id)
    _hearing(conn, 1, "2026-08-20")

    assert db.list_hearings_for_client(conn, user_id, client_id, today="2026-09-03") == []


def test_a_hearing_today_is_still_upcoming(conn):
    """Same cut the calendar makes — a hearing at 1:30pm is not history
    at 9am, and the countdown is measured off California's date."""
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)
    db.link_bill_to_client(conn, user_id, 1, client_id)
    _hearing(conn, 1, "2026-09-03")

    hearings = db.list_hearings_for_client(conn, user_id, client_id, today="2026-09-03")

    assert [h["days_until"] for h in hearings] == [0]


def test_only_this_clients_bills_appear(conn):
    user_id = insert_user(conn)
    mine = _client(conn, user_id, name="UCSA")
    other = _client(conn, user_id, name="Sierra Club CA")
    for bill_id, number, client_id in ((1, "SB1", mine), (2, "SB2", other)):
        insert_bill(conn, bill_id=bill_id, bill_number=number)
        db.flag_bill(conn, user_id, bill_id)
        db.link_bill_to_client(conn, user_id, bill_id, client_id)
        _hearing(conn, bill_id, "2026-09-10")

    hearings = db.list_hearings_for_client(conn, user_id, mine, today="2026-09-03")

    assert [h["bill_number"] for h in hearings] == ["SB1"]


def test_the_position_travels_with_the_hearing(conn):
    """The row says which stance this hearing is being attended with —
    without it the panel is a date list, not a briefing."""
    user_id = insert_user(conn)
    client_id = _client(conn, user_id)
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, user_id, 1)
    db.link_bill_to_client(conn, user_id, 1, client_id, position="oppose")
    _hearing(conn, 1, "2026-09-10")

    assert db.list_hearings_for_client(conn, user_id, client_id, today="2026-09-03")[0]["position"] == "oppose"
