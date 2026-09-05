"""
bill_text.py — getting a bill's actual words out of LegiScan and into
something searchable.

Why this exists at all: /api/search has always been a passthrough to
LegiScan's getSearch, which searches LegiScan's index of titles and
summaries. That answers "which bill is SB 122" well and "which bills
touch my client's concern" badly, because the concern is usually a
phrase that appears in the operative text and nowhere in the title. The
product audit's US-A1 wants the second question answered, and no
parameter to getSearch will do it — the text has to be held locally and
indexed locally.

Two jobs here, both deliberately kept out of legiscan_client.py:

  * fetch_bill_text() is the one call that module doesn't make. It kept
    to the two search modes and getBill on purpose (see its docstring);
    getBillText is a per-DOCUMENT call, and a bill has several documents,
    so it has a different cost shape than anything there and belongs
    with the thing that budgets for it.

  * to_plain_text() is HTML handling, which nothing else in this app
    does. LegiScan returns California bill text as HTML — the
    Legislature's own markup, tables and all — and an FTS index over
    "<span class=" is an index of the markup.

Measured against the live API on 2026-09-04, over a random sample of 20
CA AB/SB bills from the 2025-26 session, because every sizing decision
below rests on it:

    bills in the session       5,060  (4,243 of them AB/SB)
    versions per bill          4.8 mean, 4 median, 11 max
    bytes per document HTML    42KB mean, 23KB median, 442KB max
    plain text after stripping 42% of the HTML

Which is what settles the scope question. Indexing EVERY version of
every bill is ~20,400 documents: ~24,600 API calls to build once, and
~857MB of HTML. That does not fit a 30,000-query monthly free tier and
does not fit comfortably beside a 180MB database on a small disk. The
CURRENT version of each substantive bill is ~8,500 calls and ~75MB of
plain text, which fits both with room left.

So this module indexes the current version only, and US-A1's "search
prior versions too" is a deliberate deferral rather than an oversight —
it costs roughly 5x on both axes and should be bought knowingly, on a
paid LegiScan tier, not discovered as a quota overrun in March.
"""

import base64
import html
import re

import legiscan_client


# Bill text arrives as HTML and is indexed as words. Scripts and styles
# go first (their contents are not markup and would survive the tag
# strip as text), then tags, then entities, then whitespace — in that
# order, because unescaping before stripping would turn a literal
# "&lt;br&gt;" in the bill's own text into a tag and delete it.
_DROP_ELEMENTS = re.compile(r"(?is)<(script|style|head)\b.*?</\1\s*>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

# Elements that end a block of text. Used only by to_blocks() below —
# to_plain_text() deliberately flattens these to spaces, because an FTS
# snippet reads as prose either way.
_BLOCK_BOUNDARY = re.compile(
    r"(?is)</?(p|div|br|tr|li|h[1-6]|table|blockquote|section)\b[^>]*>")
# Runs of blank space that separate blocks once the tags are gone.
_BLOCK_SPLIT = re.compile(r"\n\s*\n+")


def to_plain_text(document):
    """HTML (or bytes of it) -> the words, whitespace-collapsed.

    Not a general-purpose HTML parser and not trying to be one: the
    output is only ever fed to an FTS index and to snippet(), so
    structure is worth nothing here and only the text is. Block
    elements become a space rather than a newline for the same reason —
    a snippet reads as a run of prose either way.
    """
    if isinstance(document, bytes):
        document = document.decode("utf-8", "replace")
    if not document:
        return ""
    text = _DROP_ELEMENTS.sub(" ", document)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    return _WHITESPACE.sub(" ", text).strip()


def to_blocks(document):
    """HTML -> its paragraphs, in order.

    The structure-preserving sibling of to_plain_text(), and separate
    from it on purpose. That one flattens everything to a single line
    because its output feeds an FTS index and snippet(), where structure
    is worth nothing. A redline is the opposite case: "what changed" is
    unreadable without knowing WHERE, and a bill collapsed to one line
    diffs as one enormous paragraph.

    Not a general HTML parser either — block tags become newlines,
    everything else becomes a space, and runs of blank lines separate
    the blocks. Good enough for the Legislature's own markup, which is
    the only markup this ever sees.
    """
    if isinstance(document, bytes):
        document = document.decode("utf-8", "replace")
    if not document:
        return []
    text = _DROP_ELEMENTS.sub(" ", document)
    text = _BLOCK_BOUNDARY.sub("\n\n", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    blocks = []
    for chunk in _BLOCK_SPLIT.split(text):
        cleaned = _WHITESPACE.sub(" ", chunk).strip()
        if cleaned:
            blocks.append(cleaned)
    return blocks


def current_document(texts):
    """LegiScan's `texts` array -> the one version to index.

    Latest by date, and by doc_id within a date. LegiScan returns them
    in roughly chronological order already, but "roughly" is not a
    thing to index on, and two documents genuinely can share a date
    (an introduced and an amended version filed the same day), where
    the higher doc_id is the later one.
    """
    if not texts:
        return None
    return max(
        texts,
        key=lambda t: ((t.get("date") or ""), int(t.get("doc_id") or 0)),
    )


def fetch_document(doc_id):
    """One getBillText call -> (plain text, mime, byte size).

    LegiScan base64-encodes the document in `doc`. The size it reports
    in text_size is the size of the ENCODED document's decoded bytes —
    i.e. of the HTML, not of what comes back from to_plain_text() — so
    the size returned here is measured rather than taken from the
    response, since it is the one that predicts disk use.
    """
    payload = legiscan_client.legiscan_call("getBillText", id=doc_id)
    if payload.get("status") != "OK":
        raise RuntimeError(f"LegiScan getBillText failed: {payload}")
    record = payload.get("text") or {}
    raw = base64.b64decode(record.get("doc") or "")
    return to_plain_text(raw), record.get("mime"), len(raw)


def searchable_row(bill, document, body, byte_size):
    """Everything the corpus needs to hold about one bill, from a
    getBill payload plus the fetched document.

    Carries bill_number/title/url/last_action even though `bills` has
    columns of the same name, because the two tables have different
    lifecycles: `bills` is what this app has pulled full detail for and
    is tracking (7 rows today), and the corpus is every bill in the
    session whether anyone has ever opened it (5,060). Joining the
    search results against a table that mostly doesn't have the row
    would mean a corpus that can only find bills someone already found.
    """
    history = bill.get("history") or []
    last = history[-1] if history else {}
    return {
        "bill_id": bill.get("bill_id"),
        "bill_number": bill.get("bill_number"),
        "title": bill.get("title"),
        "description": bill.get("description"),
        "url": bill.get("url"),
        "last_action": last.get("action"),
        "last_action_date": last.get("date"),
        "doc_id": document.get("doc_id"),
        "version_date": document.get("date"),
        "version_type": document.get("type"),
        "body": body,
        "byte_size": byte_size,
        "change_hash": bill.get("change_hash"),
    }
