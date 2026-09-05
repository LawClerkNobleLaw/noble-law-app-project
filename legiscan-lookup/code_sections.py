"""
code_sections.py — which sections of which California code a bill
actually touches.

The question this answers is the one a lobbyist asks that no bill
search can: "we care about Revenue and Taxation Code 17053.5 — what is
moving against it this session?" Full-text search (bill_text.py) gets
close and then fails on exactly the wrong cases, because a section
number is a terrible search term: "17053.5" appears in bills that merely
cross-reference it, and a search for "1798.100" also matches "1798.1005".
What is needed is not the words but the citation, extracted and stored
as a citation.

── Where the answer comes from ──

Every California bill opens with the Legislative Counsel's own title —
"An act to amend Section 290 of the Penal Code, and to amend Sections
653.5, 707.2 ... of, to add Section 6609.4 to, and to repeal and add
Sections 602 and 707 of, the Welfare and Institutions Code, relating to
crimes." That sentence is drafted to be the complete and authoritative
statement of what the bill does to the codes, which makes it a far
better source than the body: the body's "Section 11350 of the Health and
Safety Code" is as likely to be a cross-reference inside the statute as
an operative heading, and telling those apart is guesswork the preamble
makes unnecessary.

So this parses the preamble, and only the preamble.

── The grammar, as observed in real bills ──

Reading left to right, the sentence is a run of clauses, each carrying a
verb, one or more section numbers, and eventually a code name. Two rules
cover every form seen in the corpus:

  * a section group takes the verb(s) most recently stated before it —
    "to repeal and add Sections 602 and 707" gives both sections both
    actions;
  * a section group takes the NEAREST CODE NAMED AFTER IT, because the
    citation always precedes its code ("Sections 17132.2 and 17210 to
    the Revenue and Taxation Code"), including when several clauses
    share one trailing code ("... of, to add Section 6609.4 to, and to
    repeal and add Sections 602 and 707 of, the Welfare and Institutions
    Code").

Code names are matched against the fixed list of California codes below
rather than a shape like "[A-Z][a-z]+ Code". A vocabulary is both
stricter (it will not invent a "Budget Act Code") and looser in the way
that matters (it gets "Health and Safety" and "Revenue and Taxation",
where a capital-letter pattern stops at the lowercase "and").

── What this deliberately does not do ──

  * **Ranges are stored, not expanded.** "Sections 290 to 290.024,
    inclusive" records its two endpoints and the literal citation, and a
    search for 290.011 will not find it. Expanding would mean ordering
    California section numbers, and their numbering is genuinely
    ambiguous — read as decimals 290.024 sorts before 290.1, read as
    sequence numbers it sorts after. Guessing wrong would silently
    return bills that do not touch the section, which for this purpose
    is worse than returning fewer bills and saying so.

  * **Indirect amendment is out of scope.** The concept doc's US-A1 asks
    for bills that amend a section "indirectly." That relationship is
    not stated in the bill at all — it is a conclusion drawn by reading
    what the amended section cross-references — so no amount of parsing
    the text produces it. Named citations are the honest 80%.

  * **Bills that cite no code are a real category, not a parse failure.**
    "An act relating to immigration", with no code section named, is a
    spot bill. It correctly yields nothing.
"""

import re


# The 29 California codes, as Legislative Counsel names them. A fixed
# vocabulary rather than a pattern — see the module docstring.
CALIFORNIA_CODES = [
    "Business and Professions Code",
    "Civil Code",
    "Code of Civil Procedure",
    "Commercial Code",
    "Corporations Code",
    "Education Code",
    "Elections Code",
    "Evidence Code",
    "Family Code",
    "Financial Code",
    "Fish and Game Code",
    "Food and Agricultural Code",
    "Government Code",
    "Harbors and Navigation Code",
    "Health and Safety Code",
    "Insurance Code",
    "Labor Code",
    "Military and Veterans Code",
    "Penal Code",
    "Probate Code",
    "Public Contract Code",
    "Public Resources Code",
    "Public Utilities Code",
    "Revenue and Taxation Code",
    "Streets and Highways Code",
    "Unemployment Insurance Code",
    "Vehicle Code",
    "Water Code",
    "Welfare and Institutions Code",
]

# Longest first, so "Civil Code" can't win inside "Code of Civil
# Procedure" and "Insurance Code" can't win inside "Unemployment
# Insurance Code".
_CODE_RE = re.compile(
    "|".join(re.escape(name) for name in sorted(CALIFORNIA_CODES, key=len, reverse=True))
)

# The preamble: Legislative Counsel's title, which ends where the digest
# begins. "[ Approved by Governor" appears instead on chaptered bills,
# which carry a filing stamp between the two.
_PREAMBLE_RE = re.compile(r"An act .*?(?=LEGISLATIVE COUNSEL|\[ Approved|$)", re.S)

# Verbs, in the preamble's own vocabulary. "repeal and add" is two of
# them applying to one section group, which is why they are collected as
# a run rather than singly.
_VERB_RE = re.compile(r"\b(add|amend|repeal)(?:ed|s|ing)?\b", re.I)

# "Section 290", "Sections 653.5, 707.2, 727, and 6608.5", "Sections 290
# to 290.024, inclusive". Captures the whole run of numbers and the
# connectives between them; individual numbers come out of it below.
# The connectives repeat — "301, 321.6, and 910.1" puts a comma AND an
# "and" between the last two numbers, and "290 to 290.024, inclusive"
# puts a comma after the word. So a RUN of them is allowed between
# numbers, not a single one; requiring one silently truncated every
# list at its Oxford comma.
# A trailing ", inclusive" is absorbed too, so the citation kept on the
# row reads the way the bill wrote it ("290 to 290.024, inclusive")
# rather than trailing off after the last number.
_SECTION_GROUP_RE = re.compile(
    r"\bSections?\s+(\d[\d.]*[a-z]?(?:(?:\s*(?:,|and|or|to|through|inclusive)\s*)+\d[\d.]*[a-z]?)*"
    r"(?:\s*,?\s*inclusive)?)",
    re.I,
)
_NUMBER_RE = re.compile(r"\d[\d.]*[a-z]?")

# A range reads "X to Y" (usually followed by ", inclusive") — recorded
# as such rather than expanded, for the reason in the module docstring.
# Detected on the connective alone, since inside a captured citation
# "to" and "through" only ever join the ends of a range; the "inclusive"
# is conventional and not always present.
_RANGE_RE = re.compile(r"\d[\d.]*[a-z]?\s*(?:to|through)\s*\d[\d.]*[a-z]?", re.I)

ACTIONS = ("add", "amend", "repeal")


def preamble(body):
    """The Legislative Counsel's title, or "" if the text doesn't have
    one in a recognisable form."""
    if not body:
        return ""
    match = _PREAMBLE_RE.search(body)
    if not match:
        return ""
    # A preamble runs to a few hundred characters at most; anything much
    # longer means the digest marker was missing and the match ran on
    # into the body, where the grammar above does not hold.
    text = match.group(0)
    return text if len(text) <= 2000 else text[:2000]


def normalize_section(number):
    """"290." -> "290". Trailing punctuation rides along with the number
    when a citation ends a clause."""
    return (number or "").strip().rstrip(".").strip()


def _verbs_before(text, position):
    """The run of verbs immediately preceding `position` — "repeal and
    add" gives both, "amend" alone gives one.

    Immediately is the operative word: the search stops at the last verb
    before this section group, then walks back only over verbs and the
    connectives between them, so a verb from two clauses ago cannot leak
    forward onto a group that has its own.
    """
    verbs = [(m.start(), m.end(), m.group(1).lower()) for m in _VERB_RE.finditer(text, 0, position)]
    if not verbs:
        return []
    run = [verbs[-1]]
    for start, end, verb in reversed(verbs[:-1]):
        gap = text[end:run[0][0]]
        # Only "and"/"or"/"to"/punctuation may sit between two verbs of
        # one run. Anything with a section number or a code name in it
        # means the earlier verb belongs to an earlier clause.
        if re.fullmatch(r"[\s,]*(?:and|or|to)?[\s,]*", gap, re.I):
            run.insert(0, (start, end, verb))
        else:
            break
    return [verb for _s, _e, verb in run]


def _code_after(text, position):
    """The nearest code named after `position` — the citation always
    precedes its code."""
    match = _CODE_RE.search(text, position)
    return match.group(0) if match else None


def extract(body):
    """Bill text -> the code sections its preamble says it touches.

    Each entry is {code, section, action, citation, is_range}. One entry
    per (section, action) pair, so "repeal and add Section 602" yields
    two — a search for repeals and a search for additions should each
    find it, and collapsing them would make the bill's actual effect
    unreportable.

    Returns [] for a bill that names no code section, which is a real
    category (a spot bill) and not a failure.
    """
    text = preamble(body)
    if not text:
        return []

    found = []
    seen = set()
    for group in _SECTION_GROUP_RE.finditer(text):
        code = _code_after(text, group.end())
        if not code:
            # A section number with no code after it anywhere is a
            # citation this grammar can't place — most often a reference
            # to an uncodified act ("Sections 5.25 and 39.10 of that
            # act"). Dropping it beats filing it under a guess.
            continue
        citation = group.group(1).strip()
        is_range = bool(_RANGE_RE.search(citation))
        actions = _verbs_before(text, group.start()) or ["amend"]
        for number in _NUMBER_RE.findall(citation):
            section = normalize_section(number)
            for action in actions:
                key = (code, section, action)
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "code": code,
                    "section": section,
                    "action": action,
                    "citation": citation,
                    "is_range": is_range,
                })
    return found


def parse_query(query):
    """A user's search box -> (code or None, section or None).

    Accepts what a lobbyist actually types: a bare number ("17053.5"),
    a number with a code in any of the forms the code is known by
    ("Revenue and Taxation 17053.5", "17053.5 Revenue and Taxation
    Code", "rev & tax 17053.5"), or a code alone to see everything
    moving against it.
    """
    text = (query or "").strip()
    if not text:
        return None, None

    code = None
    match = _CODE_RE.search(text)
    if match:
        code = match.group(0)
        text = text[:match.start()] + " " + text[match.end():]
    else:
        code = _match_code_loosely(text)
        if code:
            text = _strip_loose_code(text, code)

    number = _NUMBER_RE.search(text)
    return code, (normalize_section(number.group(0)) if number else None)


# "rev & tax", "health and safety", "welfare & institutions", "pen" —
# the code names as they get typed rather than as they get drafted.
# Built from the canonical list so it can't drift out of step with it.
def _code_words(name):
    return [w for w in re.findall(r"[A-Za-z]+", name.lower())
            if w not in ("code", "and", "of")]


_LOOSE_CODES = {name: _code_words(name) for name in CALIFORNIA_CODES}


def _match_code_loosely(text):
    """The best code every one of whose significant words is prefixed by
    some word the user typed — so "rev & tax" finds the Revenue and
    Taxation Code.

    A true prefix, in one direction only. The looser "first three
    characters agree either way" rule that this replaces read "housing
    element" as the Elections Code, because "element" and "elections"
    share three letters — a search silently answering a different
    question than the one asked, which is the worst thing this mode can
    do. Requiring the typed word to actually begin the code's word keeps
    every real abbreviation (rev, tax, pen, welf) and drops that.

    Three characters minimum, so a stray "a" or "in" can't select a code.
    Longest code name first, so "unemployment insurance" cannot come
    back as the Insurance Code.
    """
    typed = [w for w in re.findall(r"[A-Za-z]+", text.lower()) if len(w) >= 3]
    if not typed:
        return None
    best = None
    for name, words in sorted(_LOOSE_CODES.items(), key=lambda kv: -len(kv[1])):
        if all(any(word.startswith(t) for t in typed) for word in words):
            if best is None or len(words) > len(_LOOSE_CODES[best]):
                best = name
    return best


def _strip_loose_code(text, code):
    """Remove the words that selected `code`, so the section number is
    what's left. Only the tokens that actually matched are removed —
    anything else in the query stays put."""
    for word in _LOOSE_CODES[code]:
        text = re.sub(
            r"\b[A-Za-z]+\b",
            lambda m: " " if word.startswith(m.group(0).lower()) and len(m.group(0)) >= 3 else m.group(0),
            text,
        )
    return re.sub(r"\b(?:code|and|of)\b", " ", text, flags=re.I)
