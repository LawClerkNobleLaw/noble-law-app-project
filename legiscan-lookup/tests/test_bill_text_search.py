"""
Full-text bill search — the corpus, the index, and the budget that
decides how much of the session ends up in it.

Nothing here touches LegiScan: fetch_document() is the one function
that makes a call and it is exercised through a stub, so the suite
stays offline and keyless like the rest of it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bill_text  # noqa: E402
import build_bill_corpus  # noqa: E402
import db  # noqa: E402


# ── Turning a bill document into words ──────────────────────────────

def test_to_plain_text_strips_markup_and_collapses_space():
    html = "<div class='bill'>  <p>An act to amend  <b>Section 17053.5</b>\n of the code.</p></div>"
    assert bill_text.to_plain_text(html) == "An act to amend Section 17053.5 of the code."


def test_to_plain_text_drops_script_and_style_contents():
    """Their contents are not markup, so a tag strip alone would leave
    them behind as indexable words."""
    html = "<style>.a{color:red}</style><p>Housing</p><script>var x = 1;</script>"
    assert bill_text.to_plain_text(html) == "Housing"


def test_to_plain_text_unescapes_after_stripping_not_before():
    """A literal "&lt;br&gt;" in the bill's own text is text. Unescaping
    first would turn it into a tag and delete it."""
    assert bill_text.to_plain_text("<p>a &lt;br&gt; b</p>") == "a <br> b"


def test_to_plain_text_accepts_bytes():
    assert bill_text.to_plain_text(b"<p>Water</p>") == "Water"


def test_to_plain_text_of_nothing_is_empty():
    assert bill_text.to_plain_text("") == ""
    assert bill_text.to_plain_text(None) == ""


# ── Which version gets indexed ──────────────────────────────────────

def test_current_document_is_the_latest_by_date():
    texts = [
        {"doc_id": 1, "date": "2025-01-05", "type": "Introduced"},
        {"doc_id": 2, "date": "2025-06-01", "type": "Amended"},
        {"doc_id": 3, "date": "2025-03-02", "type": "Amended"},
    ]
    assert bill_text.current_document(texts)["doc_id"] == 2


def test_current_document_breaks_a_date_tie_on_doc_id():
    """Two versions genuinely can share a date; the higher doc_id is the
    later one."""
    texts = [
        {"doc_id": 7, "date": "2025-06-01", "type": "Introduced"},
        {"doc_id": 9, "date": "2025-06-01", "type": "Amended"},
    ]
    assert bill_text.current_document(texts)["doc_id"] == 9


def test_current_document_of_a_bill_with_no_text_is_none():
    assert bill_text.current_document([]) is None
    assert bill_text.current_document(None) is None


# ── The FTS query language, which user input must never reach raw ────

@pytest.mark.parametrize("raw,expected", [
    ("cannabis", '"cannabis"'),
    ("cannabis licensing", '"cannabis" AND "licensing"'),
    # FTS5 operators are words once quoted, so none of these change the
    # shape of the query.
    ("a OR b", '"a" AND "OR" AND "b"'),
    ("title:housing", '"title" AND "housing"'),
    ("-housing", '"housing"'),
    # Unbalanced quotes and bare punctuation are a syntax error raw.
    ('cannabis "licensing', '"cannabis" AND "licensing"'),
    ("((", None),
    ("", None),
    ("   ", None),
    (None, None),
])
def test_fts_query_neutralizes_input(raw, expected):
    assert db.fts_query(raw) == expected


def test_fts_query_keeps_a_quoted_phrase_as_a_phrase():
    """The one bit of query syntax worth exposing: a term of art is
    searched for as a phrase, not as its words scattered apart."""
    assert db.fts_query('cannabis "local control"') == '"local control" AND "cannabis"'


def test_search_survives_input_that_is_raw_fts_syntax(conn):
    _index(conn, 1, body="an act about water")
    # Raw, each of these raises OperationalError out of MATCH.
    for hostile in ['a OR ) b', '"', 'NEAR(', '*', 'body:water OR 1']:
        assert db.search_bill_text(conn, hostile) == []


# ── Searching the corpus ────────────────────────────────────────────

def _index(conn, bill_id, body="", title="A bill", number=None, change_hash="h1"):
    db.upsert_bill_text(conn, {
        "bill_id": bill_id,
        "bill_number": number or f"AB {bill_id}",
        "title": title,
        "description": "",
        "url": f"https://legiscan.com/CA/bill/AB{bill_id}/2025",
        "last_action": "Referred to committee",
        "last_action_date": "2025-03-01",
        "doc_id": bill_id * 10,
        "version_date": "2025-03-01",
        "version_type": "Amended",
        "body": body,
        "byte_size": len(body),
        "change_hash": change_hash,
    })


def test_search_finds_a_bill_by_words_that_are_only_in_its_text(conn):
    """The whole point: LegiScan indexes titles and summaries, so a bill
    whose title says nothing about the client's concern is invisible to
    it."""
    _index(conn, 1, title="Elections: precinct maps.",
           body="By imposing new duties on local officials, this bill would create a "
                "state-mandated local program.")
    hits = db.search_bill_text(conn, "state-mandated local program")
    assert [h["bill_number"] for h in hits] == ["AB 1"]


def test_search_stems_so_licensing_finds_license(conn):
    _index(conn, 1, body="regulations governing cannabis licensing")
    assert len(db.search_bill_text(conn, "license")) == 1


def test_search_requires_every_term(conn):
    """A two-word search that returns everything matching either word
    got broader when the user tried to narrow it."""
    _index(conn, 1, body="cannabis cultivation")
    _index(conn, 2, body="housing element")
    assert len(db.search_bill_text(conn, "cannabis")) == 1
    assert db.search_bill_text(conn, "cannabis housing") == []


def test_search_returns_a_snippet_marked_with_sentinels_not_tags(conn):
    """The snippet is a span of the bill's own fetched text. It must
    arrive inert so the page can escape it and add the markup back."""
    _index(conn, 1, body="The department shall adopt regulations for widgets thereafter.")
    snippet = db.search_bill_text(conn, "widgets")[0]["snippet"]
    assert "\x02widgets\x03" in snippet
    assert "<mark>" not in snippet


def test_snippet_never_carries_markup_out_of_hostile_bill_text(conn):
    """Bill text is fetched HTML that this app stripped tags from and
    does not otherwise trust. Whatever survives, no tag is ADDED to it
    on the server — the page escapes the lot."""
    _index(conn, 1, body='shall adopt <script>alert(1)</script> rules for widgets')
    snippet = db.search_bill_text(conn, "widgets")[0]["snippet"]
    assert "<mark" not in snippet and "</mark>" not in snippet


def test_search_orders_best_match_first(conn):
    _index(conn, 1, title="Water", body="water " * 40)
    _index(conn, 2, title="Passing mention", body="a bill mentioning water once " + "filler " * 200)
    assert [h["bill_id"] for h in db.search_bill_text(conn, "water")] == [1, 2]


def test_search_of_an_empty_corpus_is_empty_not_an_error(conn):
    assert db.search_bill_text(conn, "cannabis") == []


def test_reindexing_a_bill_replaces_its_old_text(conn):
    """A new version supersedes the old outright — the index must not
    keep answering for text the bill no longer has."""
    _index(conn, 1, body="cannabis licensing")
    _index(conn, 1, body="water conveyance")
    assert db.search_bill_text(conn, "cannabis") == []
    assert len(db.search_bill_text(conn, "water")) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM bill_texts").fetchone()["n"] == 1


def test_removing_a_bill_removes_it_from_the_index(conn):
    _index(conn, 1, body="cannabis licensing")
    conn.execute("DELETE FROM bill_texts WHERE bill_id = 1")
    assert db.search_bill_text(conn, "cannabis") == []


def test_corpus_stats_reports_what_is_held(conn):
    _index(conn, 1, body="a" * 100)
    _index(conn, 2, body="b" * 50)
    stats = db.corpus_stats(conn)
    assert stats["bills"] == 2
    assert stats["bytes"] == 150
    assert stats["last_fetched"]


def test_indexed_change_hashes_maps_bill_to_hash(conn):
    _index(conn, 1, change_hash="aaa")
    _index(conn, 2, change_hash="bbb")
    assert db.indexed_change_hashes(conn) == {1: "aaa", 2: "bbb"}


# ── What the builder decides to spend calls on ──────────────────────

MASTER = [
    {"bill_id": 1, "number": "AB 1", "change_hash": "a"},
    {"bill_id": 2, "number": "SB 2", "change_hash": "b"},
    {"bill_id": 3, "number": "ACR 3", "change_hash": "c"},
    {"bill_id": 4, "number": "ABX1 4", "change_hash": "d"},
]


def test_only_substantive_measures_are_queued_by_default():
    """Resolutions are ~16% of the session and almost never carry a
    client position."""
    queued = build_bill_corpus.needs_fetching(MASTER, {})
    assert [row["number"] for row in queued] == ["AB 1", "SB 2"]


def test_all_types_queues_the_resolutions_too():
    queued = build_bill_corpus.needs_fetching(MASTER, {}, all_types=True)
    assert len(queued) == 4


def test_an_unchanged_bill_is_not_refetched():
    """The nightly cost is two calls per bill that MOVED, not per bill
    that exists — this comparison is what makes that true."""
    assert build_bill_corpus.needs_fetching(MASTER, {1: "a", 2: "b"}) == []


def test_a_changed_bill_is_refetched():
    queued = build_bill_corpus.needs_fetching(MASTER, {1: "OLD", 2: "b"})
    assert [row["number"] for row in queued] == ["AB 1"]


def test_stale_bills_are_queued_ahead_of_never_indexed_ones():
    """A stale row answers searches with last month's text as if it were
    current, which is worse than a bill the search simply can't find —
    so a budget-limited run fixes those first."""
    queued = build_bill_corpus.needs_fetching(MASTER, {2: "OLD"})
    assert [row["number"] for row in queued] == ["SB 2", "AB 1"]


@pytest.mark.parametrize("number,prefix", [
    ("AB 1234", "AB"), ("SB99", "SB"), ("ABX11", "ABX"), ("ACR 3", "ACR"), ("", ""),
])
def test_measure_prefix(number, prefix):
    assert build_bill_corpus.measure_prefix(number) == prefix


def test_index_one_writes_nothing_when_a_bill_has_no_text_yet(conn, monkeypatch):
    """A just-introduced bill can be in the master list before its text
    is posted. Caching that absence would mean never fetching it."""
    monkeypatch.setattr(
        build_bill_corpus.legiscan_client, "legiscan_call",
        lambda op, **kw: {"status": "OK", "bill": {"bill_id": 1, "texts": []}},
    )
    assert build_bill_corpus.index_one(conn, 1) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM bill_texts").fetchone()["n"] == 0


def test_index_one_indexes_the_current_version(conn, monkeypatch):
    bill = {
        "bill_id": 1, "bill_number": "AB 1", "title": "Housing element",
        "description": "An act", "url": "u", "change_hash": "h",
        "history": [{"date": "2025-03-01", "action": "Referred to committee"}],
        "texts": [
            {"doc_id": 10, "date": "2025-01-01", "type": "Introduced"},
            {"doc_id": 11, "date": "2025-03-01", "type": "Amended"},
        ],
    }

    def fake_call(op, **kw):
        if op == "getBill":
            return {"status": "OK", "bill": bill}
        assert kw["id"] == 11, "must fetch the CURRENT version, not the first"
        import base64
        return {"status": "OK", "text": {
            "mime": "text/html",
            "doc": base64.b64encode(b"<p>local control of housing</p>").decode(),
        }}

    monkeypatch.setattr(build_bill_corpus.legiscan_client, "legiscan_call", fake_call)
    monkeypatch.setattr(bill_text.legiscan_client, "legiscan_call", fake_call)

    assert build_bill_corpus.index_one(conn, 1) == 2
    row = conn.execute("SELECT * FROM bill_texts").fetchone()
    assert row["doc_id"] == 11
    assert row["version_type"] == "Amended"
    assert row["body"] == "local control of housing"
    assert row["last_action"] == "Referred to committee"
    assert len(db.search_bill_text(conn, "local control")) == 1
