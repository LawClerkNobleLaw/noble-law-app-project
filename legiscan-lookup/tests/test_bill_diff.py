"""
Redlining two versions of a bill (US-A4).

The sentences here are the shape real amendments take: a clause added to
a subdivision, a cross-reference renumbered, a section struck and a new
one inserted somewhere else. The cases that matter most are the ones
where a naive diff reads badly rather than wrongly — an unrelated pair
rendered as a mutual rewrite is technically a valid diff and useless to
the person reading it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bill_diff  # noqa: E402
import bill_text  # noqa: E402


def marked(runs):
    """Runs -> "kept [-struck-] {+added+}", for readable assertions."""
    return "".join(
        run["text"] if run["kind"] == "equal"
        else f"[-{run['text']}-]" if run["kind"] == "delete"
        else f"{{+{run['text']}+}}"
        for run in runs
    )


# ── Word-level ──────────────────────────────────────────────────────

def test_a_clause_added_to_a_sentence_shows_as_an_insertion():
    runs = bill_diff.word_diff(
        "The commission shall not issue a determination for any project.",
        "The commission shall not issue a determination for any project located on the base.",
    )
    assert marked(runs) == (
        "The commission shall not issue a determination for any project"
        "{+ located on the base+}."
    )


def test_a_replacement_shows_the_struck_text_before_its_replacement():
    """Which is the order a redline is read in on paper."""
    runs = bill_diff.word_diff("on or before January 1, 2028",
                               "on or before January 1, 2030")
    assert marked(runs) == "on or before January 1, [-2028-]{+2030+}"


def test_a_section_number_is_one_token():
    """1798.99.80 must not diff as three tokens against 1798.99.81 —
    the change is the section, not its punctuation."""
    assert bill_diff.tokenize("Section 1798.99.80 of") == \
        ["Section", " ", "1798.99.80", " ", "of"]


def test_identical_blocks_produce_one_equal_run():
    runs = bill_diff.word_diff("No change at all.", "No change at all.")
    assert [r["kind"] for r in runs] == ["equal"]


def test_changes_separated_only_by_whitespace_become_one_change():
    """Without this, the single matching space between two edited words
    splits one change into two: delete(b) insert(x) EQUAL(" ")
    delete(c) insert(y z). Right, and unreadable."""
    runs = bill_diff.word_diff("a b c d", "a x y z d")
    assert marked(runs) == "a [-b c-]{+x y z+} d"
    assert len(runs) == 4  # equal, delete, insert, equal


def test_a_real_equal_word_between_two_edits_is_not_swallowed():
    """Only WHITESPACE gaps merge — an untouched word between two
    insertions is untouched, and saying otherwise would overstate the
    amendment."""
    runs = bill_diff.word_diff("solicit comment.", "solicit public comment annually.")
    assert marked(runs) == "solicit {+public +}comment{+ annually+}."


# ── Pairing blocks ──────────────────────────────────────────────────

def test_an_edited_paragraph_is_paired_not_shown_as_two_walls():
    old = ["The agency shall convene working groups to solicit comment."]
    new = ["The agency shall convene working groups to solicit public comment annually."]
    entries = bill_diff.diff_blocks(old, new)
    assert [e["kind"] for e in entries] == ["changed"]
    # Two genuinely separate insertions, with the untouched word
    # "comment" between them — not one change split by a space.
    assert marked(entries[0]["runs"]) == (
        "The agency shall convene working groups to solicit "
        "{+public +}comment{+ annually+}."
    )


def test_two_unrelated_paragraphs_are_not_paired():
    """The case that character-level similarity gets wrong: these share
    only "paragraph", a comma and a full stop, and rendering them as a
    mutual rewrite is worse than saying one went and another came."""
    old = ["Old trailing paragraph that goes away entirely."]
    new = ["A brand new paragraph appears here."]
    assert sorted(e["kind"] for e in bill_diff.diff_blocks(old, new)) == \
        ["added", "removed"]


def test_a_pure_deletion_is_a_removal():
    entries = bill_diff.diff_blocks(["stays", "goes"], ["stays"])
    assert [e["kind"] for e in entries] == ["equal", "removed"]


def test_a_pure_insertion_is_an_addition():
    entries = bill_diff.diff_blocks(["stays"], ["stays", "arrives"])
    assert [e["kind"] for e in entries] == ["equal", "added"]


def test_an_edited_block_stays_where_the_reader_expects_it():
    """Emitted in the OLD side's order, so an edit doesn't jump."""
    old = ["one", "The agency shall convene working groups to solicit comment.", "three"]
    new = ["one", "The agency shall convene working groups to solicit public comment.", "three"]
    entries = bill_diff.diff_blocks(old, new)
    assert [e["kind"] for e in entries] == ["equal", "changed", "equal"]


# ── Collapsing ──────────────────────────────────────────────────────

def test_long_unchanged_runs_collapse_to_a_count():
    old = ["a", "b", "c", "d", "e", "f", "CHANGED old"]
    new = ["a", "b", "c", "d", "e", "f", "CHANGED new entirely different words here"]
    collapsed = bill_diff.collapse(bill_diff.diff_blocks(old, new))
    skipped = [e for e in collapsed if e["kind"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["count"] >= 4


def test_context_is_kept_either_side_of_a_change():
    old = ["a", "b", "c", "d", "e", "target old", "f", "g", "h", "i"]
    new = ["a", "b", "c", "d", "e", "target new", "f", "g", "h", "i"]
    collapsed = bill_diff.collapse(bill_diff.diff_blocks(old, new), context=1)
    kinds = [e["kind"] for e in collapsed]
    # skipped, the block before, the change, the block after, skipped
    assert kinds == ["skipped", "equal", "changed", "equal", "skipped"]


def test_an_all_equal_document_collapses_entirely():
    collapsed = bill_diff.collapse(bill_diff.diff_blocks(["a", "b", "c"], ["a", "b", "c"]))
    assert [e["kind"] for e in collapsed] == ["skipped"]
    assert collapsed[0]["count"] == 3


# ── The whole thing ─────────────────────────────────────────────────

def test_redline_reports_identical_versions_as_identical():
    result = bill_diff.redline(["a", "b"], ["a", "b"])
    assert result["identical"] is True
    assert result["summary"] == {"changed": 0, "added": 0, "removed": 0}


def test_redline_counts_blocks_not_words():
    """"Nine paragraphs changed" is a scale a person can judge."""
    old = ["intro", "The agency shall convene groups to solicit comment.", "gone"]
    new = ["intro", "The agency shall convene groups to solicit public comment.", "fresh arrival"]
    result = bill_diff.redline(old, new)
    assert result["summary"]["changed"] == 1
    assert result["summary"]["removed"] == 1
    assert result["summary"]["added"] == 1


def test_redline_of_an_empty_version_does_not_raise():
    assert bill_diff.redline([], [])["identical"] is True
    assert bill_diff.redline([], ["new"])["summary"]["added"] == 1
    assert bill_diff.redline(["old"], [])["summary"]["removed"] == 1


# ── Block extraction ────────────────────────────────────────────────

def test_to_blocks_keeps_paragraphs_apart():
    """The whole reason this exists beside to_plain_text: a bill
    collapsed to one line diffs as one enormous paragraph."""
    html = "<p>SECTION 1. Section 30345 is added.</p><div>30345. The commission shall not.</div>"
    assert bill_text.to_blocks(html) == [
        "SECTION 1. Section 30345 is added.",
        "30345. The commission shall not.",
    ]


def test_to_plain_text_still_flattens():
    """Unchanged — its output feeds an FTS snippet, where structure is
    worth nothing."""
    html = "<p>One.</p><p>Two.</p>"
    assert bill_text.to_plain_text(html) == "One. Two."


def test_to_blocks_drops_empty_blocks_and_collapses_whitespace():
    html = "<p>  Spaced   out  </p><p></p><p>\n\nNext\n</p>"
    assert bill_text.to_blocks(html) == ["Spaced out", "Next"]


def test_to_blocks_unescapes_entities():
    assert bill_text.to_blocks("<p>Health &amp; Safety</p>") == ["Health & Safety"]


def test_to_blocks_of_nothing():
    assert bill_text.to_blocks("") == []
    assert bill_text.to_blocks(None) == []


def test_to_blocks_accepts_bytes():
    """fetch_document hands it base64-decoded bytes, not a str."""
    assert bill_text.to_blocks(b"<p>Bytes in.</p>") == ["Bytes in."]


# ── Caching a version ───────────────────────────────────────────────

def test_a_cached_version_round_trips(conn):
    import db

    blocks = ["SECTION 1. Something.", "30345. Something else, with, commas."]
    db.upsert_bill_text_version(
        conn, 99, {"doc_id": 5, "date": "2026-01-01", "type": "Amended"}, blocks, 1234)
    conn.commit()
    cached = db.get_bill_text_version(conn, 5)
    assert cached["blocks"] == blocks
    assert cached["version_type"] == "Amended"
    assert cached["byte_size"] == 1234


def test_re_caching_a_version_replaces_it(conn):
    import db

    document = {"doc_id": 5, "date": "2026-01-01", "type": "Amended"}
    db.upsert_bill_text_version(conn, 99, document, ["first"], 1)
    db.upsert_bill_text_version(conn, 99, document, ["second"], 2)
    conn.commit()
    assert db.get_bill_text_version(conn, 5)["blocks"] == ["second"]
    assert db.cached_version_doc_ids(conn, 99) == {5}


def test_an_uncached_version_is_none(conn):
    import db

    assert db.get_bill_text_version(conn, 404) is None
