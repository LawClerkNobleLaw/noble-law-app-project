"""
Everything in Rotunda that isn't a bill, for the same query.

Search here has only ever searched the Legislature, which left a real
gap: a firm's clients, its letters, the Capitol directory and the
CAL-ACCESS register all live in this same database file, and typing a
lobbyist's or a staffer's name into the one search box on the product
returned nothing at all.

Two properties matter more than the matching itself, and are what these
tests mostly pin down: it costs no LegiScan call (four LIKE scans over
local tables), and it is org-scoped with nothing at all for a signed-out
visitor — the directory holds real contact details for identifiable
people and the client list is the firm's book of business. Bill search
stays open to anyone; this half does not.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import db  # noqa: E402
import directory  # noqa: E402
from conftest import insert_entity, insert_user  # noqa: E402


WIDE = (
    "Legislator,District,Party,Capitol Room,Chief of Staff,Health\n"
    "Buffy Wicks,AD-14,D,6026,K. Alvarez,J. Ramirez\n"
)


def _firm(conn, *emails):
    org_id = db.create_organization(conn, "Noble Law")
    ids = []
    for email in emails:
        user_id = insert_user(conn, email=email)
        conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (org_id, user_id))
        ids.append(user_id)
    conn.commit()
    return ids


def _labels(found, group):
    return [row["label"] for row in found.get(group, [])]


# ── Clients ─────────────────────────────────────────────────────────

def test_a_client_is_found_by_name(conn):
    user_id = insert_user(conn)
    db.create_client(conn, user_id, {"name": "Golden State Cannabis"})
    conn.commit()
    found = app.search_elsewhere(conn, user_id, "cannabis")
    assert _labels(found, "clients") == ["Golden State Cannabis"]


def test_a_client_is_found_by_what_it_cares_about(conn):
    """interests is what the lobbyist wrote for Form 602, and "which of
    our clients cares about this" is the same question as "do we have a
    client called this" — one box over both."""
    user_id = insert_user(conn)
    db.create_client(conn, user_id, {"name": "Acme Corp",
                                     "interests": "Artificial intelligence, privacy"})
    conn.commit()
    assert _labels(app.search_elsewhere(conn, user_id, "privacy"), "clients") == ["Acme Corp"]


def test_a_client_row_links_to_that_client(conn):
    user_id = insert_user(conn)
    client_id = db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()
    row = app.search_elsewhere(conn, user_id, "acme")["clients"][0]
    assert row["href"] == f"/clients/detail?id={client_id}"


# ── Letters ─────────────────────────────────────────────────────────

def test_a_letter_is_found_by_subject_bill_or_client(conn):
    user_id = insert_user(conn)
    db.create_letter(conn, user_id, {
        "subject": "Support for the housing package", "bill_label": "AB 2011",
        "client_name": "Acme Corp", "position": "support",
    })
    conn.commit()
    for term in ("housing package", "AB 2011", "Acme"):
        assert _labels(app.search_elsewhere(conn, user_id, term), "letters") == \
            ["Support for the housing package"], term


# ── The Capitol directory ───────────────────────────────────────────

def test_a_member_and_their_staffer_are_both_reachable_by_their_own_name(conn):
    """"Who is Wicks's health person" and "who is Ramirez" are the same
    lookup from opposite ends, so the staffer is listed under their own
    name rather than folded under their member's."""
    user_id = insert_user(conn)
    people = directory.build_records(WIDE, {0: "legislator", 5: "ignore"})["legislators"]
    db.save_directory_import(conn, user_id, "sheet.csv", "2026-01-15", people)
    conn.commit()
    assert "Buffy Wicks" in _labels(app.search_elsewhere(conn, user_id, "Wicks"), "directory")


def test_only_the_staff_who_matched_come_back(conn):
    """A search for a member would otherwise return their whole office
    as separate rows and bury every other group."""
    user_id = insert_user(conn)
    people = directory.build_records(WIDE, {0: "legislator"})["legislators"]
    db.save_directory_import(conn, user_id, "sheet.csv", "2026-01-15", people)
    conn.commit()
    rows = app.search_elsewhere(conn, user_id, "Wicks").get("directory", [])
    assert [r["label"] for r in rows] == ["Buffy Wicks"]


# ── The CAL-ACCESS register ─────────────────────────────────────────

def test_a_registered_lobbying_entity_is_found(conn):
    user_id = insert_user(conn)
    insert_entity(conn, "Capitol Advocacy LLC")
    conn.commit()
    assert _labels(app.search_elsewhere(conn, user_id, "Capitol Advocacy"), "lobbying") == \
        ["Capitol Advocacy LLC"]


# ── The boundaries ──────────────────────────────────────────────────

def test_a_signed_out_visitor_gets_nothing(conn):
    """Bill search doesn't need an account. This half is the firm's own
    records and the real contact details of identifiable people."""
    user_id = insert_user(conn)
    db.create_client(conn, user_id, {"name": "Acme Corp"})
    insert_entity(conn, "Acme Advocacy LLC")
    conn.commit()
    assert app.search_elsewhere(conn, None, "acme") == {}


def test_another_firms_client_is_not_reachable(conn):
    ours, = _firm(conn, "us@noble.example")
    theirs = insert_user(conn, email="them@other.example")
    db.create_client(conn, theirs, {"name": "Their Secret Client"})
    conn.commit()
    assert app.search_elsewhere(conn, ours, "secret") == {}


def test_a_colleague_at_the_same_firm_sees_the_same_clients(conn):
    """Sharing is firm-wide here, same as everywhere else in this app."""
    one, two = _firm(conn, "a@noble.example", "b@noble.example")
    db.create_client(conn, one, {"name": "Acme Corp"})
    conn.commit()
    assert _labels(app.search_elsewhere(conn, two, "acme"), "clients") == ["Acme Corp"]


def test_a_group_with_no_hits_is_absent_rather_than_empty(conn):
    """The page renders a heading per group it is given; an empty list
    would draw "Letters" over nothing."""
    user_id = insert_user(conn)
    db.create_client(conn, user_id, {"name": "Acme Corp"})
    conn.commit()
    assert set(app.search_elsewhere(conn, user_id, "acme")) == {"clients"}


def test_an_empty_query_asks_nothing_of_the_database(conn):
    user_id = insert_user(conn)
    assert app.search_elsewhere(conn, user_id, "   ") == {}
