"""
Tests for the organization above the user (P1-14).

The claim under test is a single sentence: a firm's clients, flagged
bills, positions, filings, letters and saved searches belong to the
firm, while being up to date belongs to the reader. Everything here is
two accounts — colleagues at one firm, and a stranger at another — asking
what each of them can see.

`insert_user` from conftest inserts a user directly, with no
organization, which is deliberately the shape a database mid-migration
has. Those users see only their own rows (see the COALESCE in
db._org_scope), so the existing suite keeps testing what it always did;
the tests here opt in by creating organizations explicitly.
"""

import db
import digest
from conftest import insert_bill, insert_user


def _firm(conn, name, *emails):
    """A firm with one account per email. Returns the user ids."""
    org_id = db.create_organization(conn, name)
    ids = []
    for email in emails:
        user_id = insert_user(conn, email=email)
        conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (org_id, user_id))
        ids.append(user_id)
    conn.commit()
    return ids


def test_a_colleague_sees_the_firms_clients(conn):
    alice, bob = _firm(conn, "Noble Law PC", "alice@noble.example", "bob@noble.example")
    db.create_client(conn, alice, {"name": "Anthropic PBC"})
    conn.commit()

    assert [c["name"] for c in db.list_clients(conn, bob)] == ["Anthropic PBC"]


def test_another_firm_sees_nothing_of_it(conn):
    (alice,) = _firm(conn, "Noble Law PC", "alice@noble.example")
    (rival,) = _firm(conn, "Other Firm LLP", "rival@other.example")
    db.create_client(conn, alice, {"name": "Anthropic PBC"})
    conn.commit()

    assert db.list_clients(conn, rival) == []


def test_a_colleague_sees_the_firms_flagged_bills_and_positions(conn):
    alice, bob = _firm(conn, "Noble Law PC", "alice@noble.example", "bob@noble.example")
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    db.flag_bill(conn, alice, 1)
    client_id = db.create_client(conn, alice, {"name": "Anthropic PBC"})
    db.link_bill_to_client(conn, alice, 1, client_id, "oppose")
    conn.commit()

    rows = db.list_flagged_bills(conn, bob)
    assert [r["bill_number"] for r in rows] == ["SB1159"]
    assert rows[0]["assigned_clients"][0]["position"] == "oppose"


def test_a_colleague_can_change_a_position_and_the_record_names_them(conn):
    """The firm holds the position; the person who moved it is on the
    row. That distinction is the whole point of the layer."""
    alice, bob = _firm(conn, "Noble Law PC", "alice@noble.example", "bob@noble.example")
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    db.flag_bill(conn, alice, 1)
    client_id = db.create_client(conn, alice, {"name": "Anthropic PBC"})
    db.link_bill_to_client(conn, alice, 1, client_id, "support")
    db.link_bill_to_client(conn, bob, 1, client_id, "oppose")
    conn.commit()

    history = db.list_position_history(conn, alice, bill_id=1)
    assert [(h["from_position"], h["to_position"], h["changed_by"]) for h in history] == [
        ("support", "oppose", bob), (None, "support", alice),
    ]


def test_being_up_to_date_stays_personal(conn):
    """One lobbyist opening a bill must not clear their colleague's dot.
    The flag is the firm's; the reading is the reader's."""
    alice, bob = _firm(conn, "Noble Law PC", "alice@noble.example", "bob@noble.example")
    insert_bill(conn, bill_id=1)
    db.flag_bill(conn, alice, 1)
    db.record_bill_changes(
        conn, 1,
        [{"change_type": "status", "summary": "Enrolled", "description": "Enrolled.", "event_date": None}],
        detected_at="2026-09-01T06:00:00Z",
    )
    db.mark_bill_viewed(conn, alice, 1, viewed_at="2026-09-01T09:00:00Z")
    conn.commit()

    def unread(user_id):
        return next(r for r in db.list_flagged_bills(conn, user_id) if r["bill_id"] == 1)["unread_count"]

    assert unread(alice) == 0
    assert unread(bob) == 1


def test_letters_saved_searches_and_filings_are_the_firms(conn):
    alice, bob = _firm(conn, "Noble Law PC", "alice@noble.example", "bob@noble.example")
    db.create_letter(conn, alice, {"subject": "SB1159 — OPPOSE", "body": "…"})
    db.create_saved_search(conn, alice, "AI bills", "artificial intelligence")
    db.create_prepared_filing(conn, alice, "601", None, {"EMAIL": "alice@noble.example"})
    conn.commit()

    assert [l["subject"] for l in db.list_letters(conn, bob)] == ["SB1159 — OPPOSE"]
    assert [s["name"] for s in db.list_saved_searches(conn, bob)] == ["AI bills"]
    assert len(db.list_prepared_filings(conn, bob)) == 1


def test_a_colleagues_sign_off_records_who_signed(conn):
    """The filing is the one place in this app where being able to say
    who attested to what has a statutory consequence."""
    alice, bob = _firm(conn, "Noble Law PC", "alice@noble.example", "bob@noble.example")
    field_data = {"EMAIL": "alice@noble.example"}
    filing_id = db.create_prepared_filing(conn, alice, "601", None, field_data)
    db.mark_prepared_filing_pdf_generated(conn, alice, filing_id)
    conn.commit()

    signed = db.sign_off_prepared_filing(conn, bob, filing_id, "Bob Noble", True)

    assert signed["status"] == "ready_to_file"
    assert signed["signed_name"] == "Bob Noble"
    assert signed["signed_by"] == bob


def test_the_digest_reaches_every_seat_at_the_firm(conn):
    """Both lobbyists are tracking the bill — it's the firm's flag — so
    both should hear that it moved."""
    alice, bob = _firm(conn, "Noble Law PC", "alice@noble.example", "bob@noble.example")
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    db.flag_bill(conn, alice, 1)
    conn.commit()

    changes = {1: [{"change_type": "status", "summary": "Enrolled",
                    "description": "Status moved to Enrolled.", "event_date": None}]}
    assert digest.build_user_digest(conn, alice, changes) is not None
    assert digest.build_user_digest(conn, bob, changes) is not None
    assert {email for _, email in db.list_recipients(conn)} == {
        "alice@noble.example", "bob@noble.example",
    }


def test_every_new_account_gets_a_firm(conn):
    import accounts

    user_id = accounts.create_user(conn, "solo@example.com", "a-long-enough-password")

    assert db.org_id_for_user(conn, user_id) is not None


def test_the_backfill_gives_each_existing_account_its_own_firm(conn):
    """One org per pre-existing user, never one shared org. Nothing in
    the old data says two accounts are the same firm, and merging on a
    guess would put one lobbyist's clients in front of another's."""
    first = insert_user(conn, email="one@example.com")
    second = insert_user(conn, email="two@example.com")

    db._backfill_organizations(conn)
    conn.commit()

    assert db.org_id_for_user(conn, first) != db.org_id_for_user(conn, second)


def test_the_roster_is_the_firms_and_feeds_form_601(conn):
    import datetime
    import pdf_forms

    alice, bob = _firm(conn, "Noble Law PC", "alice@noble.example", "bob@noble.example")
    db.add_org_lobbyist(conn, alice, "Alice Noble", cert_id="L-1234")
    db.add_org_lobbyist(conn, bob, "Bob Noble")
    conn.commit()

    roster = db.list_org_lobbyists(conn, bob)
    assert [l["name"] for l in roster] == ["Alice Noble", "Bob Noble"]

    values = pdf_forms.values_for_form_601(
        {"legal_name": "Noble Law PC"}, [], "alice@noble.example",
        today=datetime.date(2026, 9, 3), lobbyists=roster,
    )
    assert values["INDIVIDUAL LOBBYISTS 1"] == "Alice Noble"
    assert values["INDIVIDUAL LOBBYISTS 2"] == "Bob Noble"


def test_an_empty_roster_still_names_the_registrant(conn):
    """A firm of one is the common case and was every filing before this.
    An empty roster must not produce a 601 with no lobbyist on it."""
    import datetime
    import pdf_forms

    values = pdf_forms.values_for_form_601(
        {"legal_name": "Noble Law PC"}, [], "solo@example.com",
        today=datetime.date(2026, 9, 3), lobbyists=[],
    )

    assert values["INDIVIDUAL LOBBYISTS 1"] == "Noble Law PC"


def test_another_firms_roster_is_untouchable(conn):
    (alice,) = _firm(conn, "Noble Law PC", "alice@noble.example")
    (rival,) = _firm(conn, "Other Firm LLP", "rival@other.example")
    lobbyist_id = db.add_org_lobbyist(conn, alice, "Alice Noble")
    conn.commit()

    assert db.list_org_lobbyists(conn, rival) == []
    assert db.delete_org_lobbyist(conn, rival, lobbyist_id) is False
    assert db.delete_org_lobbyist(conn, alice, lobbyist_id) is True
