"""
The Capitol directory: importing a staff sheet, searching it, and the
org boundary that keeps one firm's copy its own.

The sheets quoted here are the two real shapes — the crowdsourced wide
format (one row per office, a column per committee) and the narrow one
a firm's own address-book export has (one row per staffer).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accounts  # noqa: E402
import db  # noqa: E402
import directory  # noqa: E402

from conftest import insert_user  # noqa: E402


WIDE = (
    "Legislator,District,Party,Capitol Room,Chief of Staff,Appropriations,Health,Latino Caucus\n"
    "Buffy Wicks,AD-14,D,6026,K. Alvarez,J. Ramirez,J. Ramirez,\n"
    "Scott Wiener,SD-11,D,5100,M. Tran,,L. Osei / P. Ng,M. Tran\n"
)

NARROW = (
    "Member\tChamber\tStaff Name\tTitle\tEmail\tDirect Phone\tCommittee\n"
    "Buffy Wicks\tAssembly\tJ. Ramirez\tLegislative Aide\tjr@asm.ca.gov\t916-319-2014\tHealth\n"
    "Buffy Wicks\tAssembly\tK. Alvarez\tChief of Staff\tka@asm.ca.gov\t\t\n"
)


def auto_mapping(text):
    """What the page would send after accepting every guess."""
    return {c["index"]: c["role"] for c in directory.inspect(text)["columns"]}


def imported(text):
    return directory.build_records(text, auto_mapping(text))["legislators"]


def by_name(legislators, name):
    return next(l for l in legislators if l["full_name"] == name)


def staff_named(legislator, name):
    return next(s for s in legislator["staff"] if s["full_name"] == name)


# ── Guessing what a column is ───────────────────────────────────────

@pytest.mark.parametrize("header,role", [
    ("Legislator", "legislator"),
    ("Member", "legislator"),
    ("District", "district"),
    ("AD/SD", "district"),
    ("Party", "party"),
    ("Capitol Room #", "office_room"),
    ("Office Phone", "office_phone"),
    ("Staff Name", "staff_name"),
    ("Title", "staff_title"),
    ("Email", "staff_email"),
    ("Direct Phone", "staff_phone"),
    ("Cell", "staff_phone"),
    # A job title as a header means the cells are the people who hold it.
    ("Chief of Staff", "staff_role"),
    ("Scheduler", "staff_role"),
    ("Legislative Director", "staff_role"),
    ("Latino Caucus", "caucus"),
    ("Issue Area", "issue"),
    ("", "ignore"),
])
def test_guess_role(header, role):
    assert directory.guess_role(header) == role


def test_an_unknown_header_is_assumed_to_be_a_committee():
    """In this sheet a column nobody recognises is nearly always a
    committee whose name is the header — treating it as noise would
    throw away the data the import exists to capture."""
    assert directory.guess_role("Water Parks & Wildlife") == "committee"
    assert directory.guess_role("Revenue & Taxation") == "committee"


def test_a_longer_hint_wins_over_one_contained_in_it():
    """"Chief of Staff" must not be decided by the "staff" rule."""
    assert directory.guess_role("Chief of Staff") == "staff_role"
    assert directory.guess_role("Staff Email") == "staff_email"


# ── Reading the file ────────────────────────────────────────────────

def test_tab_separated_paste_is_read_as_well_as_csv():
    """"Download as CSV" and "copy the cells" produce different things
    and both arrive here."""
    headers, rows = directory.read_csv(NARROW)
    assert headers[0] == "Member"
    assert len(rows) == 2


def test_inspect_returns_columns_guesses_and_a_preview():
    found = directory.inspect(WIDE)
    assert found["row_count"] == 2
    assert [c["header"] for c in found["columns"]][:2] == ["Legislator", "District"]
    assert found["preview"][0][0] == "Buffy Wicks"


def test_inspect_of_an_empty_file_says_so_rather_than_raising():
    assert directory.inspect("") == {"columns": [], "preview": [], "row_count": 0}


# ── The wide, crowdsourced shape ────────────────────────────────────

def test_a_column_header_that_is_a_job_becomes_the_staffer_title():
    wicks = by_name(imported(WIDE), "Buffy Wicks")
    assert staff_named(wicks, "K. Alvarez")["title"] == "Chief of Staff"


def test_a_column_header_that_is_a_committee_becomes_an_assignment():
    wicks = by_name(imported(WIDE), "Buffy Wicks")
    assert [a["name"] for a in staff_named(wicks, "J. Ramirez")["assignments"]] == [
        "Appropriations", "Health",
    ]


def test_one_staffer_named_in_several_columns_is_one_person():
    """The entire reason the wide sheet is worth importing rather than
    reading: Ramirez appears twice and is one record with two
    assignments, not two records."""
    wicks = by_name(imported(WIDE), "Buffy Wicks")
    assert [s["full_name"] for s in wicks["staff"]] == ["K. Alvarez", "J. Ramirez"]


def test_two_names_in_one_cell_become_two_people():
    wiener = by_name(imported(WIDE), "Scott Wiener")
    names = {s["full_name"] for s in wiener["staff"]}
    assert {"L. Osei", "P. Ng"} <= names
    assert staff_named(wiener, "P. Ng")["assignments"] == [
        {"kind": "committee", "name": "Health"}]


def test_a_caucus_column_records_a_caucus_not_a_committee():
    wiener = by_name(imported(WIDE), "Scott Wiener")
    assert {"kind": "caucus", "name": "Latino Caucus"} in staff_named(wiener, "M. Tran")["assignments"]


def test_chamber_is_inferred_from_the_district_prefix():
    """"AD-14" and "SD-11" carry the chamber, and a sheet with a
    district column often has no separate one for it."""
    people = imported(WIDE)
    assert by_name(people, "Buffy Wicks")["chamber"] == "Assembly"
    assert by_name(people, "Scott Wiener")["chamber"] == "Senate"


def test_office_level_fields_are_read_once_per_office():
    assert by_name(imported(WIDE), "Buffy Wicks")["office_room"] == "6026"


# ── The narrow, one-row-per-staffer shape ───────────────────────────

def test_repeated_legislator_rows_collapse_to_one_office():
    people = imported(NARROW)
    assert [l["full_name"] for l in people] == ["Buffy Wicks"]
    assert len(people[0]["staff"]) == 2


def test_contact_details_attach_to_the_rows_own_staffer():
    ramirez = staff_named(by_name(imported(NARROW), "Buffy Wicks"), "J. Ramirez")
    assert ramirez["email"] == "jr@asm.ca.gov"
    assert ramirez["phone"] == "916-319-2014"
    assert ramirez["title"] == "Legislative Aide"


def test_a_generic_committee_header_puts_the_committee_in_the_cell():
    """A column headed just "Committee" names the committee in its
    cells, the reverse of the wide sheet."""
    ramirez = staff_named(by_name(imported(NARROW), "Buffy Wicks"), "J. Ramirez")
    assert ramirez["assignments"] == [{"kind": "committee", "name": "Health"}]


# ── Refusing to guess ───────────────────────────────────────────────

def test_no_legislator_column_is_an_error_not_an_empty_import():
    result = directory.build_records(WIDE, {4: "staff_name"})
    assert result["legislators"] == []
    assert "legislator" in result["warnings"][0].lower()


def test_rows_with_no_legislator_name_are_skipped_and_counted():
    text = "Legislator,Chief of Staff\nBuffy Wicks,K. Alvarez\n,Orphan Row\n"
    result = directory.build_records(text, {0: "legislator", 1: "staff_role"})
    assert len(result["legislators"]) == 1
    assert "1 row(s)" in result["warnings"][0]


@pytest.mark.parametrize("marker", ["", "-", "N/A", "none", "TBD", "vacant"])
def test_placeholder_cells_do_not_become_staff(marker):
    text = f"Legislator,Health\nBuffy Wicks,{marker}\n"
    people = directory.build_records(text, {0: "legislator", 1: "committee"})["legislators"]
    assert people[0]["staff"] == []


def test_an_ignored_column_is_not_imported():
    people = directory.build_records(WIDE, {0: "legislator", 5: "ignore"})["legislators"]
    assert all(not l["staff"] for l in people)


# ── Storing it ──────────────────────────────────────────────────────

def test_save_and_search(conn):
    user_id = insert_user(conn)
    db.save_directory_import(conn, user_id, "codex.csv", "2026-01-15", imported(WIDE))

    assert db.directory_stats(conn, user_id) == {"legislators": 2, "staff": 5, "stale": 0}
    hits = db.search_directory(conn, user_id, query="Appropriations")
    assert [l["full_name"] for l in hits] == ["Buffy Wicks"]


def test_search_matches_a_staffer_by_what_they_handle(conn):
    """One box, not three: "water", "Wicks" and "Ramirez" are the same
    question asked three ways."""
    user_id = insert_user(conn)
    db.save_directory_import(conn, user_id, "s.csv", "2026-01-15", imported(WIDE))
    for term in ("Wicks", "AD-14", "Ramirez", "Appropriations"):
        assert [l["full_name"] for l in db.search_directory(conn, user_id, query=term)] == \
            ["Buffy Wicks"], term


def test_search_marks_which_staff_matched_and_puts_them_first(conn):
    """The office is shown whole for context, so without this a search
    for a committee lists four names and says nothing about which one
    handles it."""
    user_id = insert_user(conn)
    db.save_directory_import(conn, user_id, "s.csv", "2026-01-15", imported(WIDE))
    wicks = db.search_directory(conn, user_id, query="Appropriations")[0]
    assert wicks["staff"][0]["full_name"] == "J. Ramirez"
    assert wicks["staff"][0]["matched"] is True
    assert wicks["staff"][1]["matched"] is False


def test_chamber_filter(conn):
    user_id = insert_user(conn)
    db.save_directory_import(conn, user_id, "s.csv", "2026-01-15", imported(WIDE))
    assert [l["full_name"] for l in db.search_directory(conn, user_id, chamber="Senate")] == \
        ["Scott Wiener"]


def test_a_new_import_replaces_the_old_directory(conn):
    """A sheet is a snapshot of who works where on a date. Merging two
    snapshots produces a roster that never existed — the staffer who
    left in March survives forever because the June sheet doesn't
    mention them."""
    user_id = insert_user(conn)
    db.save_directory_import(conn, user_id, "jan.csv", "2026-01-15", imported(WIDE))
    db.save_directory_import(conn, user_id, "jun.csv", "2026-06-01", imported(NARROW))

    stats = db.directory_stats(conn, user_id)
    assert stats == {"legislators": 1, "staff": 2, "stale": 0}
    assert db.latest_directory_import(conn, user_id)["source_name"] == "jun.csv"
    assert db.search_directory(conn, user_id, query="Wiener") == []


def test_stale_flags_survive_a_reimport(conn):
    """Those are a person's own reports about contacts they found wrong,
    not the sheet's content — a re-import shouldn't discard them."""
    user_id = insert_user(conn)
    db.save_directory_import(conn, user_id, "jan.csv", "2026-01-15", imported(WIDE))
    wicks = db.search_directory(conn, user_id, query="Alvarez")[0]
    alvarez = next(s for s in wicks["staff"] if s["full_name"] == "K. Alvarez")
    db.set_staff_stale(conn, user_id, alvarez["id"], True)

    db.save_directory_import(conn, user_id, "jan-again.csv", "2026-02-01", imported(WIDE))

    wicks = db.search_directory(conn, user_id, query="Alvarez")[0]
    again = next(s for s in wicks["staff"] if s["full_name"] == "K. Alvarez")
    assert again["is_stale"] is True


def test_flagging_and_correcting_a_contact(conn):
    user_id = insert_user(conn)
    db.save_directory_import(conn, user_id, "s.csv", "2026-01-15", imported(NARROW))
    staff_id = db.search_directory(conn, user_id)[0]["staff"][0]["id"]

    db.set_staff_stale(conn, user_id, staff_id, True)
    assert db.directory_stats(conn, user_id)["stale"] == 1

    # Correcting it clears the flag — the row is no longer wrong.
    db.update_staff(conn, user_id, staff_id, {"email": "new@asm.ca.gov"})
    assert db.directory_stats(conn, user_id)["stale"] == 0
    person = next(s for s in db.search_directory(conn, user_id)[0]["staff"] if s["id"] == staff_id)
    assert person["email"] == "new@asm.ca.gov"


def test_update_staff_ignores_fields_it_does_not_own(conn):
    user_id = insert_user(conn)
    db.save_directory_import(conn, user_id, "s.csv", "2026-01-15", imported(NARROW))
    staff_id = db.search_directory(conn, user_id)[0]["staff"][0]["id"]
    db.update_staff(conn, user_id, staff_id, {"user_id": 999, "id": 1})
    assert conn.execute(
        "SELECT user_id FROM capitol_staff WHERE id = ?", (staff_id,)
    ).fetchone()["user_id"] == user_id


# ── The boundary: whose directory is it ─────────────────────────────

def _two_firms(conn):
    firm_a = accounts.create_user(conn, "a@firm-a.com", "a-long-enough-passphrase-1")
    firm_b = accounts.create_user(conn, "b@firm-b.com", "a-long-enough-passphrase-2")
    colleague = accounts.create_user(conn, "c@firm-a.com", "a-long-enough-passphrase-3")
    conn.execute(
        "UPDATE users SET org_id = (SELECT org_id FROM users WHERE id = ?) WHERE id = ?",
        (firm_a, colleague),
    )
    return firm_a, colleague, firm_b


def test_the_firm_shares_one_directory(conn):
    """US-I3 — one person's contact research benefits the whole team."""
    firm_a, colleague, _firm_b = _two_firms(conn)
    db.save_directory_import(conn, firm_a, "s.csv", "2026-01-15", imported(WIDE))
    assert db.directory_stats(conn, colleague)["staff"] == 5
    assert db.search_directory(conn, colleague, query="Ramirez")


def test_another_firm_sees_nothing(conn):
    """Not a filter — a boundary. This data is a firm's own copy of a
    directory holding direct contact details for identifiable people,
    and pooling it across firms is somebody else's crowdsourced work and
    somebody else's personal data. See the schema.sql note."""
    firm_a, _colleague, firm_b = _two_firms(conn)
    db.save_directory_import(conn, firm_a, "s.csv", "2026-01-15", imported(WIDE))
    assert db.directory_stats(conn, firm_b) == {"legislators": 0, "staff": 0, "stale": 0}
    assert db.search_directory(conn, firm_b, query="Ramirez") == []


def test_another_firm_cannot_write_to_this_ones_directory(conn):
    firm_a, _colleague, firm_b = _two_firms(conn)
    db.save_directory_import(conn, firm_a, "s.csv", "2026-01-15", imported(WIDE))
    staff_id = db.search_directory(conn, firm_a)[0]["staff"][0]["id"]

    db.set_staff_stale(conn, firm_b, staff_id, True)
    db.update_staff(conn, firm_b, staff_id, {"email": "attacker@example.com"})

    person = next(s for s in db.search_directory(conn, firm_a)[0]["staff"] if s["id"] == staff_id)
    assert person["is_stale"] is False
    assert person["email"] != "attacker@example.com"


def test_one_firms_import_does_not_clear_anothers(conn):
    """save_directory_import replaces THIS firm's directory. The delete
    it does first has to be scoped, or the first firm to import wipes
    everyone."""
    firm_a, _colleague, firm_b = _two_firms(conn)
    db.save_directory_import(conn, firm_a, "a.csv", "2026-01-15", imported(WIDE))
    db.save_directory_import(conn, firm_b, "b.csv", "2026-01-15", imported(NARROW))
    assert db.directory_stats(conn, firm_a)["staff"] == 5
    assert db.directory_stats(conn, firm_b)["staff"] == 2
