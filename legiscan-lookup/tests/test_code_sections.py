"""
Code-section extraction and search.

The preambles quoted here are verbatim from real CA 2025-26 bills — the
grammar in code_sections.py was written against them, so a change that
breaks one of these breaks a bill that actually exists. AB22's is the
hardest form observed: four clauses, two codes, a trailing code shared
by three of them, and a "repeal and add" pair.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import code_sections as cs  # noqa: E402
import db  # noqa: E402


def preamble(text):
    """Wrap a title the way a real bill's text does, so the parser has
    the digest marker it cuts on."""
    return f"{text} LEGISLATIVE COUNSEL'S DIGEST AB 1, as introduced, Someone."


# ── Extraction, against real preambles ──────────────────────────────

def test_the_simple_case():
    got = cs.extract(preamble(
        "An act to add Section 30345 to the Public Resources Code, "
        "relating to coastal resources."
    ))
    assert got == [{
        "code": "Public Resources Code", "section": "30345", "action": "add",
        "citation": "30345", "is_range": False,
    }]


def test_a_list_keeps_every_section_including_after_the_oxford_comma():
    """"301, 321.6, and 910.1" puts a comma AND an "and" between the
    last two — the bug this pins was truncating every list there."""
    got = cs.extract(preamble(
        "An act to amend Sections 301, 321.6, and 910.1 of, and to add "
        "Section 910.10 to, the Public Utilities Code, relating to the "
        "Public Utilities Commission."
    ))
    amended = sorted(g["section"] for g in got if g["action"] == "amend")
    added = [g["section"] for g in got if g["action"] == "add"]
    assert amended == ["301", "321.6", "910.1"]
    assert added == ["910.10"]
    assert {g["code"] for g in got} == {"Public Utilities Code"}


def test_the_hardest_real_preamble():
    """AB 22, verbatim. Two codes; the first section belongs to the
    Penal Code and the remaining four clauses share a trailing Welfare
    and Institutions Code; and "repeal and add" applies both verbs."""
    got = cs.extract(preamble(
        "An act to amend Section 290 of the Penal Code, and to amend "
        "Sections 653.5, 707.2, 727, 828.1, 1753.3, 1767.1, and 6608.5 of, "
        "to add Section 6609.4 to, and to repeal and add Sections 602 and "
        "707 of, the Welfare and Institutions Code, relating to crimes."
    ))
    by = {(g["code"], g["section"], g["action"]) for g in got}

    assert ("Penal Code", "290", "amend") in by
    for section in ("653.5", "707.2", "727", "828.1", "1753.3", "1767.1", "6608.5"):
        assert ("Welfare and Institutions Code", section, "amend") in by
    assert ("Welfare and Institutions Code", "6609.4", "add") in by
    # "repeal and add" is two rows per section, not one merged action.
    for section in ("602", "707"):
        assert ("Welfare and Institutions Code", section, "repeal") in by
        assert ("Welfare and Institutions Code", section, "add") in by
    # Nothing leaked across the code boundary.
    assert ("Penal Code", "602", "repeal") not in by


def test_a_section_takes_the_nearest_code_named_after_it():
    got = cs.extract(preamble(
        "An act to add Article 19.2 (commencing with Section 69995) to "
        "Chapter 2 of Part 42 of Division 5 of Title 3 of the Education "
        "Code, and to add Sections 17132.2 and 17210 to the Revenue and "
        "Taxation Code, relating to education expenses."
    ))
    by_section = {g["section"]: g["code"] for g in got}
    assert by_section["69995"] == "Education Code"
    assert by_section["17132.2"] == "Revenue and Taxation Code"
    assert by_section["17210"] == "Revenue and Taxation Code"


def test_commencing_with_records_the_section_it_commences_at():
    """The operative heading for these says "Chapter 22.5 is added", so
    the section number only ever appears in the title. It is still the
    section a lobbyist would search for."""
    got = cs.extract(preamble(
        "An act to add Chapter 22.5 (commencing with Section 26280) to "
        "Division 20 of the Health and Safety Code, relating to housing."
    ))
    assert [(g["section"], g["action"]) for g in got] == [("26280", "add")]


def test_a_range_records_its_endpoints_and_says_it_is_a_range():
    """Not expanded — ordering CA section numbers is ambiguous. See the
    module docstring."""
    got = cs.extract(preamble(
        "An act to amend Sections 290 to 290.024, inclusive, of the Penal "
        "Code, relating to crimes."
    ))
    assert sorted(g["section"] for g in got) == ["290", "290.024"]
    assert all(g["is_range"] for g in got)
    assert all("inclusive" in g["citation"] for g in got)


def test_a_bill_that_cites_no_code_yields_nothing():
    """A spot bill is a real category, not a parse failure."""
    assert cs.extract(preamble("An act relating to immigration.")) == []


def test_a_citation_with_no_code_after_it_is_dropped_not_guessed():
    """"Sections 5.25 and 39.10 of that act" is an uncodified act.
    Filing it under a guessed code would be worse than losing it."""
    assert cs.extract(preamble(
        "An act to amend the Budget Act of 2024, by amending Sections "
        "5.25 and 39.10 of that act, relating to the state budget."
    )) == []


def test_text_with_no_preamble_yields_nothing():
    assert cs.extract("") == []
    assert cs.extract(None) == []
    assert cs.extract("Some text that never says An act to anything.") == []


def test_the_body_after_the_digest_marker_is_not_parsed():
    """The body is full of cross-references that look like citations and
    aren't — that is the whole reason only the preamble is read."""
    got = cs.extract(
        "An act to amend Section 290 of the Penal Code, relating to crimes. "
        "LEGISLATIVE COUNSEL'S DIGEST Existing law, Section 11350 of the "
        "Health and Safety Code, provides that Section 99999 of the Vehicle "
        "Code is not applicable."
    )
    assert [g["section"] for g in got] == ["290"]


@pytest.mark.parametrize("longer,shorter", [
    ("Unemployment Insurance Code", "Insurance Code"),
    ("Code of Civil Procedure", "Civil Code"),
])
def test_a_longer_code_name_wins_over_one_contained_in_it(longer, shorter):
    got = cs.extract(preamble(f"An act to amend Section 5 of the {longer}, relating to x."))
    assert [g["code"] for g in got] == [longer]


# ── Reading what the user typed ─────────────────────────────────────

@pytest.mark.parametrize("query,code,section", [
    ("17053.5", None, "17053.5"),
    ("Revenue and Taxation Code 17053.5", "Revenue and Taxation Code", "17053.5"),
    ("17053.5 Revenue and Taxation Code", "Revenue and Taxation Code", "17053.5"),
    # Abbreviations, as they actually get typed.
    ("rev and tax 17053.5", "Revenue and Taxation Code", "17053.5"),
    ("pen 290", "Penal Code", "290"),
    ("veh 23152", "Vehicle Code", "23152"),
    ("welf and inst 602", "Welfare and Institutions Code", "602"),
    # A code alone is "everything moving against it", a real question.
    ("Health and Safety Code", "Health and Safety Code", None),
    ("health and safety", "Health and Safety Code", None),
    # Disambiguation.
    ("unemployment insurance 1234", "Unemployment Insurance Code", "1234"),
    ("insurance 1234", "Insurance Code", "1234"),
    ("code of civil procedure 425.16", "Code of Civil Procedure", "425.16"),
    ("", None, None),
])
def test_parse_query(query, code, section):
    assert cs.parse_query(query) == (code, section)


@pytest.mark.parametrize("query", [
    "housing element",     # "element" must not select the Elections Code
    "cannabis licensing",
    "local control",
    "artificial intelligence",
])
def test_prose_is_not_mistaken_for_a_code(query):
    """A citation search that quietly answers a different question than
    the one asked is the worst thing this mode can do — "housing
    element" read as the Elections Code was a real bug."""
    assert cs.parse_query(query) == (None, None)


def test_a_one_or_two_letter_word_cannot_select_a_code():
    assert cs.parse_query("a 5") == (None, "5")


# ── Storage and search ──────────────────────────────────────────────

def _corpus(conn, bill_id, number, body, last_action_date="2025-03-01"):
    db.upsert_bill_text(conn, {
        "bill_id": bill_id, "bill_number": number, "title": "A bill",
        "description": "", "url": "u", "last_action": "Referred",
        "last_action_date": last_action_date, "doc_id": bill_id, "version_date": "2025-03-01",
        "version_type": "Amended", "body": body, "byte_size": len(body), "change_hash": "h",
    })
    db.replace_bill_code_sections(conn, bill_id, cs.extract(body))


def test_search_by_code_and_section(conn):
    _corpus(conn, 1, "AB 1", preamble(
        "An act to amend Section 290 of the Penal Code, relating to crimes."))
    _corpus(conn, 2, "AB 2", preamble(
        "An act to amend Section 290 of the Vehicle Code, relating to cars."))

    hits = db.search_code_sections(conn, code="Penal Code", section="290")
    assert [h["bill_number"] for h in hits] == ["AB 1"]
    assert hits[0]["sections"] == [{
        "code": "Penal Code", "section": "290", "action": "amend",
        "citation": "290", "is_range": False,
    }]


def test_a_bare_section_number_searches_every_code(conn):
    _corpus(conn, 1, "AB 1", preamble("An act to amend Section 290 of the Penal Code, relating to x."))
    _corpus(conn, 2, "AB 2", preamble("An act to amend Section 290 of the Vehicle Code, relating to x."))
    assert len(db.search_code_sections(conn, section="290")) == 2


def test_a_code_alone_returns_everything_moving_against_it(conn):
    _corpus(conn, 1, "AB 1", preamble(
        "An act to amend Sections 100 and 200 of the Water Code, relating to x."))
    _corpus(conn, 2, "AB 2", preamble(
        "An act to add Section 5 to the Penal Code, relating to x."))
    hits = db.search_code_sections(conn, code="Water Code")
    assert [h["bill_number"] for h in hits] == ["AB 1"]
    assert {s["section"] for s in hits[0]["sections"]} == {"100", "200"}


def test_search_with_neither_half_of_a_citation_is_empty(conn):
    _corpus(conn, 1, "AB 1", preamble("An act to amend Section 290 of the Penal Code, relating to x."))
    assert db.search_code_sections(conn) == []


def test_a_section_number_is_matched_exactly_not_as_a_prefix(conn):
    """The reason this table exists rather than leaning on full-text
    search: "1798.100" as words also matches "1798.1005"."""
    _corpus(conn, 1, "AB 1", preamble(
        "An act to amend Section 1798.1005 of the Civil Code, relating to x."))
    assert db.search_code_sections(conn, section="1798.100") == []
    assert len(db.search_code_sections(conn, section="1798.1005")) == 1


def test_only_the_matched_citations_are_returned_on_a_row(conn):
    """A bill touching twelve sections should not answer a search for
    one of them with all twelve — the point of the chips is to say why
    THIS bill is in THESE results."""
    _corpus(conn, 1, "AB 1", preamble(
        "An act to amend Sections 100, 200, and 300 of the Water Code, relating to x."))
    hits = db.search_code_sections(conn, code="Water Code", section="200")
    assert [s["section"] for s in hits[0]["sections"]] == ["200"]


def test_reparsing_replaces_the_old_citations(conn):
    """An amended bill that drops a section from its title has to stop
    answering for it."""
    _corpus(conn, 1, "AB 1", preamble(
        "An act to amend Sections 100 and 200 of the Water Code, relating to x."))
    assert len(db.search_code_sections(conn, section="100")) == 1
    db.replace_bill_code_sections(conn, 1, cs.extract(preamble(
        "An act to amend Section 200 of the Water Code, relating to x.")))
    assert db.search_code_sections(conn, section="100") == []
    assert len(db.search_code_sections(conn, section="200")) == 1


def test_parsing_stamps_the_corpus_row_so_it_is_not_reparsed(conn):
    _corpus(conn, 1, "AB 1", preamble("An act to amend Section 5 of the Water Code, relating to x."))
    assert db.bills_needing_section_parse(conn) == []


def test_freshly_fetched_text_is_queued_for_parsing(conn):
    """upsert_bill_text alone leaves sections_parsed_at NULL, which is
    the free queue the corpus builder sweeps before spending budget."""
    db.upsert_bill_text(conn, {
        "bill_id": 1, "bill_number": "AB 1", "title": "t", "description": "",
        "url": "u", "last_action": "", "last_action_date": "", "doc_id": 1,
        "version_date": "", "version_type": "", "body": "An act to amend Section 5 of the Water Code.",
        "byte_size": 10, "change_hash": "h",
    })
    assert [bill_id for bill_id, _body in db.bills_needing_section_parse(conn)] == [1]


def test_code_section_stats(conn):
    _corpus(conn, 1, "AB 1", preamble(
        "An act to amend Sections 100 and 200 of the Water Code, relating to x."))
    assert db.code_section_stats(conn) == {"parsed": 1, "citations": 2}


def test_results_are_ordered_by_most_recent_action(conn):
    """Every hit is an exact citation match, so there is no relevance to
    rank by — recency is what distinguishes them."""
    _corpus(conn, 1, "AB 1", preamble(
        "An act to amend Section 5 of the Water Code, relating to x."), last_action_date="2025-01-01")
    _corpus(conn, 2, "AB 2", preamble(
        "An act to amend Section 5 of the Water Code, relating to x."), last_action_date="2026-08-01")
    assert [h["bill_number"] for h in db.search_code_sections(conn, section="5")] == ["AB 2", "AB 1"]
