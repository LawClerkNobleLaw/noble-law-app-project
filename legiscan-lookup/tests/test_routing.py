"""
Suggesting who a position letter should go to (US-I2).

The committee names quoted here are verbatim from LegiScan calendar rows
already in this app's database ("Senate Elections and Constitutional
Amendments Hearing" and friends), and the sheet-side labels are the
shorthand these directories are actually written in.

The rule under test is strict on purpose. A suggestion that sends a
letter to the wrong staffer is worse than no suggestion, because the
user has no way to tell that it is wrong — so the false-positive tests
here matter more than the false-negative ones.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accounts  # noqa: E402
import db  # noqa: E402
import directory  # noqa: E402
import routing  # noqa: E402


# ── Reading the committee off a hearing ─────────────────────────────

@pytest.mark.parametrize("description,chamber,committee", [
    ("Senate Education Hearing", "Senate", "Education"),
    ("Assembly Appropriations Hearing", "Assembly", "Appropriations"),
    ("Assembly Privacy And Consumer Protection Hearing",
     "Assembly", "Privacy And Consumer Protection"),
    ("Senate Elections and Constitutional Amendments Hearing",
     "Senate", "Elections and Constitutional Amendments"),
])
def test_committee_of_a_hearing(description, chamber, committee):
    assert routing.committee_of({"description": description}) == (chamber, committee)


def test_committee_falls_back_to_location():
    """LegiScan puts the committee in one field or the other depending
    on the row — same order letter_drafts._hearing_line reads them in."""
    assert routing.committee_of(
        {"description": "", "location": "Senate Judiciary"}) == ("Senate", "Judiciary")


def test_no_hearing_yields_no_committee():
    assert routing.committee_of(None) == ("", "")
    assert routing.committee_of({"description": "", "location": ""}) == ("", "")


# ── Matching a staffer's column to a committee ──────────────────────

@pytest.mark.parametrize("sheet,bill", [
    ("Appropriations", "Appropriations"),
    ("Privacy & Consumer Protection", "Privacy And Consumer Protection"),
    # A staffer whose column says only "Privacy" does handle that
    # committee.
    ("Privacy", "Privacy And Consumer Protection"),
    ("Elections", "Elections and Constitutional Amendments"),
    ("Water Parks & Wildlife", "Water, Parks and Wildlife"),
    # Prefixes, which need no alias.
    ("Nat Res", "Natural Resources"),
    ("Transpo", "Transportation"),
    ("Jud", "Judiciary"),
    # Contractions, which do.
    ("Approps", "Appropriations"),
    ("Higher Ed", "Higher Education"),
])
def test_matches(sheet, bill):
    assert routing.matches(sheet, bill)


@pytest.mark.parametrize("sheet,bill", [
    # The one an any-token rule would get wrong: "public" is shared and
    # "safety" is not, and these are different committees.
    ("Public Safety", "Public Employment and Retirement"),
    ("Health", "Education"),
    ("Judiciary", "Public Safety"),
    ("Insurance", "Banking and Finance"),
    # Two characters match far too much to be a signal.
    ("Ag", "Appropriations"),
    ("", "Appropriations"),
    ("Appropriations", ""),
])
def test_does_not_match(sheet, bill):
    assert not routing.matches(sheet, bill)


def test_the_chamber_alone_never_satisfies_a_match():
    """"Senate" is stripped as noise — matching on it would put every
    Senate staffer on every Senate bill."""
    assert not routing.matches("Senate", "Senate Education")


def test_coverage_ranks_a_fuller_label_higher():
    """Both match; the one that accounts for more of the committee is
    the better suggestion."""
    exact = routing.coverage("Privacy And Consumer Protection",
                             "Privacy And Consumer Protection")
    partial = routing.coverage("Privacy", "Privacy And Consumer Protection")
    assert exact == 1.0
    assert 0 < partial < exact


# ── Putting it together ─────────────────────────────────────────────

def office(name, chamber, *staff, room=None):
    return {"full_name": name, "chamber": chamber, "office_room": room,
            "district": "", "party": "", "office_phone": "", "staff": list(staff)}


def person(name, *committees, title="", stale=False):
    return {"full_name": name, "title": title, "email": "", "phone": "",
            "is_stale": stale,
            "assignments": [{"kind": "committee", "name": c} for c in committees]}


HEARING = {"description": "Assembly Appropriations Hearing"}


def test_suggests_the_staffer_who_covers_the_committee():
    found = routing.suggest(
        [office("Buffy Wicks", "Assembly",
                person("J. Ramirez", "Appropriations"),
                person("K. Alvarez", "Health"))],
        hearing=HEARING,
    )
    assert found["committee"] == "Appropriations"
    assert [s["staff"]["full_name"] for s in found["suggestions"]] == ["J. Ramirez"]
    assert found["suggestions"][0]["reason"] == "Handles Appropriations"


def test_same_chamber_outranks_the_other_house():
    """The letter is going to a committee, and the committee sits in
    one house."""
    found = routing.suggest(
        [office("Scott Wiener", "Senate", person("L. Osei", "Appropriations")),
         office("Buffy Wicks", "Assembly", person("J. Ramirez", "Appropriations"))],
        hearing=HEARING,
    )
    assert [s["staff"]["full_name"] for s in found["suggestions"]] == \
        ["J. Ramirez", "L. Osei"]


def test_a_committee_match_outranks_the_authors_office():
    """A position letter is read by the committee's staff; the author's
    office is who you call about amendments."""
    found = routing.suggest(
        [office("Robert Rivas", "Assembly", person("D. Okafor", "Health")),
         office("Buffy Wicks", "Assembly", person("J. Ramirez", "Appropriations"))],
        hearing=HEARING,
        sponsors=[{"name": "Robert Rivas"}],
    )
    assert [(s["kind"], s["staff"]["full_name"]) for s in found["suggestions"]] == [
        ("committee", "J. Ramirez"), ("author", "D. Okafor"),
    ]


def test_the_authors_office_is_suggested_with_no_hearing_at_all():
    """A bill referred but not yet set for hearing has no committee to
    match — the author's office is still worth offering."""
    found = routing.suggest(
        [office("Buffy Wicks", "Assembly", person("K. Alvarez", title="Chief of Staff"))],
        sponsors=[{"name": "Buffy Wicks"}],
    )
    assert [s["kind"] for s in found["suggestions"]] == ["author"]


def test_a_committee_match_in_the_authors_own_office_reports_the_committee():
    """The more specific reason wins — "handles Approps" says more than
    "works for the author"."""
    found = routing.suggest(
        [office("Buffy Wicks", "Assembly", person("J. Ramirez", "Appropriations"))],
        hearing=HEARING, sponsors=[{"name": "Buffy Wicks"}],
    )
    assert found["suggestions"][0]["kind"] == "committee"


def test_no_directory_yields_nothing_rather_than_raising():
    assert routing.suggest([], hearing=HEARING)["suggestions"] == []
    assert routing.suggest(None, hearing=None)["suggestions"] == []


def test_a_committee_nobody_covers_yields_nothing():
    found = routing.suggest(
        [office("Buffy Wicks", "Assembly", person("J. Ramirez", "Health"))],
        hearing={"description": "Senate Judiciary Hearing"},
    )
    assert found["committee"] == "Judiciary"
    assert found["suggestions"] == []


def test_a_staffer_covering_several_committees_is_suggested_once():
    found = routing.suggest(
        [office("Buffy Wicks", "Assembly",
                person("J. Ramirez", "Appropriations", "Approps", "Budget"))],
        hearing=HEARING,
    )
    assert len(found["suggestions"]) == 1


def test_suggestions_are_capped():
    offices = [office(f"Member {i}", "Assembly", person(f"S{i}", "Appropriations"))
               for i in range(30)]
    assert len(routing.suggest(offices, hearing=HEARING, limit=5)["suggestions"]) == 5


# ── Through the database ────────────────────────────────────────────

def _set_up(conn, hearing_description=None, sponsor=None):
    user_id = accounts.create_user(conn, "a@firm.com", "a-long-enough-passphrase-1")
    conn.execute("INSERT INTO bills (id, state, bill_number) VALUES (99, 'CA', 'SB 1')")
    if hearing_description:
        conn.execute(
            "INSERT INTO bill_hearings (bill_id, date, description) VALUES (99, ?, ?)",
            ("2026-06-01", hearing_description),
        )
    if sponsor:
        conn.execute(
            "INSERT INTO bill_sponsors (bill_id, name, role) VALUES (99, ?, 'Rep')",
            (sponsor,),
        )
    sheet = ("Legislator,Chamber,Chief of Staff,Appropriations\n"
             "Buffy Wicks,Assembly,K. Alvarez,J. Ramirez\n")
    records = directory.build_records(sheet, {
        0: "legislator", 1: "chamber", 2: "staff_role", 3: "committee"})
    db.save_directory_import(conn, user_id, "s.csv", "2026-01-01", records["legislators"])
    return user_id


def test_routing_for_bill_reads_the_latest_hearing(conn):
    """A bill that moved houses is in front of its NEWEST committee, not
    the one it started in."""
    user_id = _set_up(conn, "Senate Judiciary Hearing")
    conn.execute(
        "INSERT INTO bill_hearings (bill_id, date, description)"
        " VALUES (99, '2026-08-13', 'Assembly Appropriations Hearing')")
    result = db.routing_for_bill(conn, user_id, 99)
    assert (result["chamber"], result["committee"]) == ("Assembly", "Appropriations")
    assert [s["staff"]["full_name"] for s in result["suggestions"]] == ["J. Ramirez"]


def test_routing_says_when_there_is_no_directory_at_all(conn):
    """Distinct from "no suggestions": a firm that hasn't imported a
    sheet needs telling that, not telling there's nobody to write to."""
    user_id = accounts.create_user(conn, "a@firm.com", "a-long-enough-passphrase-1")
    conn.execute("INSERT INTO bills (id, state, bill_number) VALUES (99, 'CA', 'SB 1')")
    result = db.routing_for_bill(conn, user_id, 99)
    assert result["have_directory"] is False
    assert result["suggestions"] == []


def test_routing_has_a_directory_but_no_hearing(conn):
    user_id = _set_up(conn)
    result = db.routing_for_bill(conn, user_id, 99)
    assert result["have_directory"] is True
    assert result["committee"] == ""
    assert result["suggestions"] == []


def test_routing_for_a_letter_with_no_bill(conn):
    user_id = _set_up(conn, "Assembly Appropriations Hearing")
    assert db.routing_for_bill(conn, user_id, None)["suggestions"] == []


def test_routing_only_sees_this_firms_directory(conn):
    """Same boundary as the directory itself — one firm's imported sheet
    must not route another firm's letters."""
    _set_up(conn, "Assembly Appropriations Hearing")
    other = accounts.create_user(conn, "b@other.com", "a-long-enough-passphrase-2")
    result = db.routing_for_bill(conn, other, 99)
    assert result["have_directory"] is False
    assert result["suggestions"] == []
