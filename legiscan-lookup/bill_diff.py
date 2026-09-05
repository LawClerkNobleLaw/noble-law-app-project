"""
bill_diff.py — what changed between two versions of a bill.

US-A4. A bill is amended four or five times on its way through, and the
question after every one of them is the same: what moved, and does it
still do what my client needs. Today that is answered by opening two
PDFs side by side and reading.

── Why this is on-demand and not indexed ──

bill_text.py's header records the measurement: 4.8 versions per bill
across 5,060 bills, ~24,600 API calls and ~857MB to hold every version
of everything. That is why the corpus indexes the current version only,
and the same arithmetic decides the shape here — a redline is asked for
one bill at a time, by a person looking at that bill, so its two
documents are fetched when asked for and cached afterwards. A firm
redlining all forty bills it tracks, every version, spends ~190 calls.
Backfilling the session to answer the same question in advance spends
twenty-four thousand.

── Two passes, because one reads badly ──

A word-level diff over a whole bill produces thousands of scattered
single-word changes with no way to tell a renumbered cross-reference
from a rewritten subdivision. A block-level diff says "this paragraph
changed" and makes you find the words yourself.

So: blocks first (bill_text.to_blocks), to locate the change and to
skip the untouched 90% of the bill; then words WITHIN a changed block,
to show it. Unchanged runs collapse to a count, because the point of
opening a redline is the parts that are not the same.

── Matching blocks that moved ──

difflib pairs a deleted block with an inserted one only when they sit
opposite each other in the same replace op. Legislative amendments
insert and renumber constantly, so a paragraph that merely gained a
clause often appears as an unrelated delete and insert. `_pair_blocks`
re-pairs those by similarity before rendering, which is the difference
between "one sentence gained six words" and two walls of solid colour.
"""

import difflib
import re


# What counts as a token for the word-level pass. Words and numbers hold
# together (including section numbers like 1798.99.80, which must not
# diff as three tokens); punctuation stands alone so that a changed
# comma is a changed comma and not a changed sentence.
_TOKEN = re.compile(r"\w+(?:[.\-']\w+)*|\s+|[^\w\s]")

# Below this, two blocks are different paragraphs rather than one
# paragraph edited. Tuned to be forgiving — an amendment can rewrite
# half a sentence — but not so forgiving that two unrelated findings
# pair up and render as a total rewrite of each other.
_PAIR_THRESHOLD = 0.5

# How many unchanged blocks to keep either side of a change. Enough to
# see which section you are looking at; not so much that the page is
# mostly things that did not change.
_CONTEXT_BLOCKS = 1


def tokenize(text):
    return _TOKEN.findall(text or "")


def _significant(text):
    """Tokens with the whitespace dropped — what two blocks are compared
    on."""
    return [token for token in tokenize(text) if token.strip()]


def _similarity(a, b):
    """How alike two blocks are, 0..1, compared on WORDS rather than
    characters.

    Characters over-score unrelated English prose: two paragraphs
    sharing nothing but "paragraph", a comma and a full stop score 0.54
    character-wise and 0.27 word-wise, and at a 0.5 threshold the
    character measure pairs them and renders two unrelated sentences as
    a mutual rewrite. The same two versions of one real sentence score
    0.81 and 0.82 — so words separate the cases and characters do not.
    """
    return difflib.SequenceMatcher(None, _significant(a), _significant(b)).ratio()


def word_diff(old, new):
    """Two blocks -> runs of ("equal" | "delete" | "insert", text).

    Whitespace rides along with the tokens rather than being diffed on
    its own, so a re-wrapped paragraph doesn't light up as changed.
    """
    old_tokens, new_tokens = tokenize(old), tokenize(new)
    opcodes = _merge_across_gaps(
        difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).get_opcodes(),
        old_tokens,
    )

    runs = []
    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            _append(runs, "equal", "".join(old_tokens[i1:i2]))
        else:
            # Struck text then its replacement, in that order, because
            # that is how a redline is read. Either side may be empty,
            # which is what makes a pure insertion or deletion fall out
            # of the same branch.
            _append(runs, "delete", "".join(old_tokens[i1:i2]))
            _append(runs, "insert", "".join(new_tokens[j1:j2]))
    return [run for run in runs if run["text"]]


def _merge_across_gaps(opcodes, old_tokens):
    """Fold a whitespace-only "equal" opcode into the change on either
    side of it.

    "a b c d" -> "a x y z d" otherwise diffs as delete(b), insert(x),
    EQUAL(" "), delete(c), insert(y z) — technically right and unreadable,
    because the single matching space between two edited words splits one
    change into two. Real bill text is full of these: any two-word edit
    with a shared space in the middle fragments the same way.

    So a run of changes separated only by whitespace becomes one change:
    "a [-b c-]{+x y z+} d".
    """
    changed = [op != "equal" for op, *_ in opcodes]
    for index in range(1, len(opcodes) - 1):
        op, i1, i2, _j1, _j2 = opcodes[index]
        if op != "equal" or changed[index]:
            continue
        if not "".join(old_tokens[i1:i2]).strip() and changed[index - 1] and changed[index + 1]:
            changed[index] = True

    merged = []
    for flag, (op, i1, i2, j1, j2) in zip(changed, opcodes):
        if merged and flag and merged[-1][0] == "replace":
            _prev_op, pi1, _pi2, pj1, _pj2 = merged[-1]
            merged[-1] = ("replace", pi1, i2, pj1, j2)
        elif flag:
            merged.append(("replace", i1, i2, j1, j2))
        else:
            merged.append((op, i1, i2, j1, j2))
    return merged


def _append(runs, kind, text):
    """Merge into the previous run when it's the same kind — otherwise
    every token becomes its own span and the markup outweighs the
    text."""
    if not text:
        return
    if runs and runs[-1]["kind"] == kind:
        runs[-1]["text"] += text
    else:
        runs.append({"kind": kind, "text": text})


def _pair_blocks(deleted, inserted):
    """Match up blocks within one replace op — see the module docstring.

    Greedy best-first: the most similar surviving pair is taken, then
    the next, until nothing left clears the threshold. Greedy rather
    than optimal because these lists are a handful of blocks long and
    the difference never shows.
    """
    scored = sorted(
        (
            (_similarity(old, new), oi, ni)
            for oi, old in enumerate(deleted)
            for ni, new in enumerate(inserted)
        ),
        key=lambda s: -s[0],
    )
    pairs, used_old, used_new = {}, set(), set()
    for score, oi, ni in scored:
        if score < _PAIR_THRESHOLD:
            break
        if oi in used_old or ni in used_new:
            continue
        pairs[oi] = ni
        used_old.add(oi)
        used_new.add(ni)
    return pairs, used_old, used_new


def diff_blocks(old_blocks, new_blocks):
    """Two versions' blocks -> the entries a redline renders.

    Each entry is one of:
      {"kind": "equal",   "text": ...}
      {"kind": "added",   "text": ...}
      {"kind": "removed", "text": ...}
      {"kind": "changed", "runs": [...], "old": ..., "new": ...}
      {"kind": "skipped", "count": n}
    """
    entries = []
    matcher = difflib.SequenceMatcher(None, old_blocks, new_blocks, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for block in old_blocks[i1:i2]:
                entries.append({"kind": "equal", "text": block})
        elif op == "delete":
            for block in old_blocks[i1:i2]:
                entries.append({"kind": "removed", "text": block})
        elif op == "insert":
            for block in new_blocks[j1:j2]:
                entries.append({"kind": "added", "text": block})
        else:
            deleted, inserted = old_blocks[i1:i2], new_blocks[j1:j2]
            pairs, used_old, used_new = _pair_blocks(deleted, inserted)
            # Walk the OLD side in order and emit each paired block where
            # it was, so an edit stays where the reader expects it.
            for oi, old in enumerate(deleted):
                if oi in pairs:
                    new = inserted[pairs[oi]]
                    entries.append({
                        "kind": "changed", "old": old, "new": new,
                        "runs": word_diff(old, new),
                    })
                elif oi not in used_old:
                    entries.append({"kind": "removed", "text": old})
            for ni, new in enumerate(inserted):
                if ni not in used_new:
                    entries.append({"kind": "added", "text": new})
    return entries


def collapse(entries, context=_CONTEXT_BLOCKS):
    """Replace long runs of unchanged blocks with a count of them.

    A bill is mostly unchanged between versions, and a redline that
    makes you scroll through the unchanged part to find the changed part
    has reproduced the problem it exists to solve.
    """
    keep = [False] * len(entries)
    for index, entry in enumerate(entries):
        if entry["kind"] == "equal":
            continue
        for near in range(max(0, index - context), min(len(entries), index + context + 1)):
            keep[near] = True

    out, run = [], 0
    for index, entry in enumerate(entries):
        if entry["kind"] == "equal" and not keep[index]:
            run += 1
            continue
        if run:
            out.append({"kind": "skipped", "count": run})
            run = 0
        out.append(entry)
    if run:
        out.append({"kind": "skipped", "count": run})
    return out


def summarize(entries):
    """The one-line answer, for a reader deciding whether to read the
    rest. Counts blocks, not words: "nine paragraphs changed" is a
    scale a person can judge, where "412 words" is not."""
    counts = {"changed": 0, "added": 0, "removed": 0}
    for entry in entries:
        if entry["kind"] in counts:
            counts[entry["kind"]] += 1
    return counts


def redline(old_text, new_text):
    """Two versions' block lists -> a rendered, collapsed redline plus
    its summary."""
    entries = diff_blocks(old_text or [], new_text or [])
    return {
        "entries": collapse(entries),
        "summary": summarize(entries),
        "identical": all(entry["kind"] == "equal" for entry in entries),
    }
