"""
Exporting the Capitol directory to a phone (US-I5).

The tests that matter here are the format ones. A vCard with a fold in
the wrong place, or an unescaped comma, imports as garbage or not at
all — and it fails on the user's phone, in the Capitol hallway, where
nobody can debug it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vcard  # noqa: E402


LEGISLATOR = {
    "full_name": "Buffy Wicks", "chamber": "Assembly", "district": "AD-14",
    "party": "D", "office_room": "6026", "office_phone": "916-319-2014",
    "staff": [],
}


def staffer(**overrides):
    person = {
        "full_name": "J. Ramirez", "title": "Legislative Aide",
        "email": "jr@asm.ca.gov", "phone": "916-555-0100",
        "is_stale": False, "assignments": [],
    }
    person.update(overrides)
    return person


def office(*staff):
    return [dict(LEGISLATOR, staff=list(staff))]


def fields(text):
    """Unfold, then split into (property, value) — how an importer reads
    it, which is the only reading that counts."""
    unfolded = text.replace("\r\n ", "")
    out = []
    for line in unfolded.split("\r\n"):
        if not line:
            continue
        name, _, value = line.partition(":")
        out.append((name, value))
    return out


# ── Escaping ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,escaped", [
    ("Health, Education", "Health\\, Education"),
    ("Ways; Means", "Ways\\; Means"),
    ("a\\b", "a\\\\b"),
    ("one\ntwo", "one\\ntwo"),
])
def test_escape(raw, escaped):
    assert vcard.escape(raw) == escaped


def test_backslash_is_escaped_before_everything_else():
    """Otherwise the escapes just added get escaped again."""
    assert vcard.escape("a\\,b") == "a\\\\\\,b"


def test_a_comma_in_a_committee_name_does_not_split_the_value(conn=None):
    text = vcard.render(office(staffer(assignments=[
        {"kind": "committee", "name": "Revenue, Taxation and Fees"}])))
    note = dict(fields(text))["NOTE"]
    assert "Revenue\\, Taxation and Fees" in note


# ── Folding ─────────────────────────────────────────────────────────

def test_short_lines_are_not_folded():
    assert "\r\n" not in vcard.fold("FN:J. Ramirez")


def test_long_lines_fold_within_the_octet_limit():
    line = "NOTE:" + "Appropriations, Health, Judiciary, Water Parks and Wildlife, " * 3
    folded = vcard.fold(line)
    assert all(len(part.encode("utf-8")) <= 75 for part in folded.split("\r\n"))


def test_folding_is_reversible():
    """An importer unfolds by removing CRLF+space. Whatever comes out
    has to be exactly what went in."""
    line = "NOTE:" + "x" * 400
    assert vcard.fold(line).replace("\r\n ", "") == line


def test_folding_never_splits_a_multibyte_character():
    """A cut mid-sequence imports as mojibake, and these names have
    accents in them."""
    line = "NOTE:" + "José Muñoz — Comité de Salud " * 6
    folded = vcard.fold(line)
    for part in folded.split("\r\n"):
        part.encode("utf-8").decode("utf-8")   # raises if a character was cut
    assert folded.replace("\r\n ", "") == line


def test_continuation_lines_carry_one_octet_less():
    """They start with a space, which counts against the same 75 — so
    the physical lines AS SPLIT (space included) all fit, and the
    payload on a continuation is 74."""
    parts = vcard.fold("NOTE:" + "x" * 300).split("\r\n")
    assert len(parts[0].encode()) == 75
    assert all(p.startswith(" ") for p in parts[1:])
    assert all(len(p.encode()) <= 75 for p in parts)


# ── The card ────────────────────────────────────────────────────────

def test_a_card_is_well_formed():
    text = vcard.render(office(staffer()))
    properties = [name for name, _ in fields(text)]
    assert properties[0] == "BEGIN"
    assert properties[1] == "VERSION"
    assert properties[-1] == "END"
    assert dict(fields(text))["VERSION"] == "3.0"
    assert text.startswith("BEGIN:VCARD\r\n")
    assert text.endswith("END:VCARD\r\n")


def test_n_has_all_five_components_even_when_empty():
    """"N:" alone is rejected by some importers; "N:;;;;" is the empty
    structured value."""
    text = vcard.render(office(staffer(full_name="Cher")))
    assert dict(fields(text))["N"] == "Cher;;;;"


def test_fn_keeps_the_name_exactly_as_the_sheet_wrote_it():
    """N's split into family/given is a guess. The displayed name never
    is."""
    text = vcard.render(office(staffer(full_name="Mary-Jo van der Berg")))
    assert dict(fields(text))["FN"] == "Mary-Jo van der Berg"
    assert dict(fields(text))["N"] == "Berg;Mary-Jo van der;;;"


def test_org_is_the_office_so_a_phone_groups_the_whole_staff():
    text = vcard.render(office(staffer()))
    assert dict(fields(text))["ORG"] == "Office of Buffy Wicks (Assembly AD-14)"


def test_the_staffers_own_number_is_preferred():
    text = vcard.render(office(staffer(phone="916-555-0100")))
    tel = [v for k, v in fields(text) if k.startswith("TEL")]
    assert tel == ["916-555-0100"]


def test_the_office_number_is_used_when_the_staffer_has_none():
    """Better than no number at all — it is the office's, and labelled
    as a work line."""
    text = vcard.render(office(staffer(phone="")))
    tel = [v for k, v in fields(text) if k.startswith("TEL")]
    assert tel == ["916-319-2014"]


def test_assignments_go_in_the_note_grouped_by_kind():
    text = vcard.render(office(staffer(assignments=[
        {"kind": "committee", "name": "Health"},
        {"kind": "committee", "name": "Appropriations"},
        {"kind": "caucus", "name": "Latino Caucus"},
    ])))
    note = dict(fields(text))["NOTE"]
    assert "Committees: Health\\, Appropriations" in note
    assert "Caucuses: Latino Caucus" in note


def test_the_note_carries_the_as_of_date():
    """This is an export, not a sync — a card sitting in someone's phone
    two years from now should still be able to say how old it is."""
    text = vcard.render(office(staffer()), as_of="2026-06-01")
    assert "current as of 2026-06-01" in dict(fields(text))["NOTE"]


def test_a_stale_contact_says_so_on_the_card():
    text = vcard.render(office(staffer(is_stale=True)))
    assert "FLAGGED AS OUT OF DATE" in dict(fields(text))["NOTE"]


def test_a_staffer_with_no_email_omits_the_property_rather_than_sending_a_blank():
    text = vcard.render(office(staffer(email="")))
    assert not any(k.startswith("EMAIL") for k, _ in fields(text))


def test_every_staffer_gets_a_card():
    text = vcard.render(office(staffer(full_name="A"), staffer(full_name="B")))
    assert text.count("BEGIN:VCARD") == 2
    assert vcard.count(office(staffer(), staffer())) == 2


def test_an_empty_directory_exports_nothing_rather_than_an_empty_card():
    assert vcard.render([]) == ""
    assert vcard.render([dict(LEGISLATOR, staff=[])]) == ""


# ── The spreadsheet ─────────────────────────────────────────────────

def test_csv_is_one_row_per_staffer():
    rows = vcard.csv_rows(office(staffer(full_name="A"), staffer(full_name="B")))
    assert rows[0] == vcard.CSV_COLUMNS
    assert [r[6] for r in rows[1:]] == ["A", "B"]


def test_csv_repeats_the_office_columns_on_every_row():
    """Flat is the point — a spreadsheet sorts and filters, and it can
    only do that if every row is complete."""
    rows = vcard.csv_rows(office(staffer(full_name="A"), staffer(full_name="B")))
    assert all(r[0] == "Buffy Wicks" and r[4] == "6026" for r in rows[1:])


def test_csv_joins_assignments_with_semicolons_not_commas():
    """A comma would need quoting on every populated row; the semicolon
    reads the same and survives a naive re-split."""
    rows = vcard.csv_rows(office(staffer(assignments=[
        {"kind": "committee", "name": "Health"},
        {"kind": "committee", "name": "Appropriations"},
    ])))
    assert rows[1][10] == "Health; Appropriations"


def test_csv_is_crlf_terminated_for_excel():
    text = vcard.render_csv(office(staffer()))
    assert text.endswith("\r\n")
    assert "\r\n" in text
