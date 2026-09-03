"""
Tests for position letters (P1-7): letter_drafts.build_seed, which
decides what a new letter says, and the db.letters storage under it.

What's worth pinning about the seed is its handling of absence. Every
input past the bill can genuinely be missing — a bill with no client, a
client with no position set, a bill with nothing scheduled — and the rule
is that a missing fact drops its line rather than leaving a placeholder
behind. A draft that ships "[COMMITTEE]" to a member's office is worse
than one that never mentioned the committee.
"""

import db
import letter_drafts
from conftest import insert_bill, insert_user

BILL = {
    "state": "CA",
    "bill_number": "SB1159",
    "title": "Artificial intelligence: transparency and governance.",
}


def test_the_header_block_carries_every_known_fact(conn):
    seed = letter_drafts.build_seed(
        BILL,
        client={"name": "Anthropic PBC", "position": "oppose"},
        position="oppose",
        hearing={"location": "Assembly Judiciary", "date": "2026-06-30"},
        profile={"legal_name": "Noble Law PC"},
    )

    assert seed["subject"] == "CA SB1159 — OPPOSE — on behalf of Anthropic PBC"
    assert "Re: CA SB1159" in seed["body"]
    assert "Position: OPPOSE on behalf of Anthropic PBC" in seed["body"]
    assert "Set for hearing: Assembly Judiciary, June 30, 2026" in seed["body"]
    assert seed["body"].rstrip().endswith("Noble Law PC")


def test_a_missing_hearing_drops_its_line_rather_than_leaving_a_blank(conn):
    seed = letter_drafts.build_seed(BILL, client={"name": "Anthropic PBC"}, position="support")

    assert "Set for hearing" not in seed["body"]
    assert "[" not in seed["body"].split("Dear Member:")[0]


def test_support_and_oppose_ask_for_opposite_votes(conn):
    support = letter_drafts.build_seed(BILL, {"name": "UCSA"}, "support")
    oppose = letter_drafts.build_seed(BILL, {"name": "UCSA"}, "oppose")

    assert "vote AYE" in support["body"] and "supports" in support["body"]
    assert "vote NO" in oppose["body"] and "opposes" in oppose["body"]


def test_watch_asks_for_nothing(conn):
    """A watch position has no ask in it, and inventing one would put a
    request in the client's mouth they never made."""
    seed = letter_drafts.build_seed(BILL, {"name": "UCSA"}, "watch")

    assert "vote" not in seed["body"]
    assert "monitoring" in seed["body"]
    assert "following CA SB1159 closely" in seed["body"]


def test_no_client_still_produces_a_usable_letter(conn):
    seed = letter_drafts.build_seed(BILL)

    assert seed["subject"] == "CA SB1159"
    assert "on behalf of" not in seed["body"]
    assert "I write regarding CA SB1159" in seed["body"]


def test_a_malformed_hearing_date_is_printed_as_given(conn):
    """LegiScan's data is what it is, and a drafting module is not where
    a bad date should surface as an exception."""
    seed = letter_drafts.build_seed(BILL, hearing={"location": "Approps", "date": "not-a-date"})

    assert "Approps, not-a-date" in seed["body"]


def test_letters_round_trip_and_stay_scoped_to_their_user(conn):
    user_id = insert_user(conn)
    other = insert_user(conn, email="other@example.com")
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    letter_id = db.create_letter(conn, user_id, {
        "bill_id": 1, "bill_label": "CA SB1159", "client_name": "Anthropic PBC",
        "position": "oppose", "subject": "Draft", "body": "Body text",
    })
    conn.commit()

    assert db.get_letter(conn, user_id, letter_id)["body"] == "Body text"
    assert db.get_letter(conn, other, letter_id) is None
    assert db.list_letters(conn, other) == []


def test_editing_touches_only_what_the_user_typed(conn):
    """The bill and client are what the letter was written about. They
    don't change because somebody edited a paragraph."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    letter_id = db.create_letter(conn, user_id, {
        "bill_id": 1, "bill_label": "CA SB1159", "client_name": "Anthropic PBC",
        "position": "oppose", "subject": "Draft", "body": "First pass",
    })
    conn.commit()

    assert db.update_letter(conn, user_id, letter_id, "Final", "Second pass") is True
    letter = db.get_letter(conn, user_id, letter_id)
    assert (letter["subject"], letter["body"]) == ("Final", "Second pass")
    assert letter["bill_label"] == "CA SB1159"
    assert letter["client_name"] == "Anthropic PBC"


def test_letters_can_be_listed_by_bill_and_by_client(conn):
    """The bill report shows one bill's letters; the client record shows
    one client's."""
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    insert_bill(conn, bill_id=2, bill_number="SB813")
    client_id = db.create_client(conn, user_id, {"name": "Anthropic PBC"})
    db.create_letter(conn, user_id, {"bill_id": 1, "client_id": client_id,
                                     "subject": "One", "body": ""})
    db.create_letter(conn, user_id, {"bill_id": 2, "subject": "Two", "body": ""})
    conn.commit()

    assert [l["subject"] for l in db.list_letters(conn, user_id, bill_id=1)] == ["One"]
    assert [l["subject"] for l in db.list_letters(conn, user_id, client_id=client_id)] == ["One"]
    assert len(db.list_letters(conn, user_id)) == 2


def test_another_user_cannot_delete_your_letter(conn):
    user_id = insert_user(conn)
    other = insert_user(conn, email="other@example.com")
    letter_id = db.create_letter(conn, user_id, {"subject": "Mine", "body": ""})
    conn.commit()

    assert db.delete_letter(conn, other, letter_id) is False
    assert db.delete_letter(conn, user_id, letter_id) is True
