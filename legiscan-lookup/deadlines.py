"""
deadlines.py — the Legislature's own calendar, as dates this app can
count down to.

US-B3. The hearing calendar answers "when is my bill being heard",
which is the question with an answer already in LegiScan. It does not
answer the one that actually loses bills: "which of my bills has to
clear a committee in the next nine days or die." Those deadlines are
statutory and procedural, they apply to every bill at once, and nothing
in any API publishes them.

── Why the dates are entered rather than shipped ──

The deadline KINDS below are stable — they come from the Constitution
and Joint Rule 61, and the same dozen categories recur every session.
The DATES are not: they are set each session by a house resolution, they
move with weekends and recesses, and a wrong one here is a bill the firm
thought it had another week to amend.

So no dates are hard-coded in this repo. A firm pastes the Legislature's
published tentative calendar in, `parse_calendar` reads it, and the page
shows what it read before anything is stored — the same guess-then-
confirm shape the directory import uses (see directory.py), and for the
same reason: the source is a human document that changes format, and
being wrong quietly is worse than asking.

── What a "kind" is for ──

Two things. It drives which of a firm's bills a deadline actually
threatens — a policy-committee deadline for fiscal bills is not the same
warning as a house-of-origin passage deadline — and it survives
rephrasing, so a firm that pastes next session's calendar in different
words still gets the same categories in the same order.

Classification is by keyword against the pasted text, and an unmatched
line is stored as "other" rather than dropped. A date the firm can see
on its calendar, uncategorised, beats a date this module decided not to
believe in.
"""

import datetime
import re


# The recurring categories, in the order they occur in a session. Order
# matters only for display — a session's deadlines read as a sequence.
DEADLINE_KINDS = (
    "introduction",       # last day to introduce bills
    "two_year",           # last day for 2-year bills to clear their house
    "policy_fiscal",      # policy committees, fiscal bills, own house
    "policy_nonfiscal",   # policy committees, non-fiscal bills, own house
    "fiscal_committee",   # fiscal committees, own house
    "house_of_origin",    # last day to pass bills out of the first house
    "budget",             # budget bill passage
    "policy_second",      # policy committees, second house
    "fiscal_second",      # fiscal committees, second house
    "floor_second",       # last day for each house to pass bills
    "recess",             # interim/spring/summer recess boundaries
    "governor",           # signing/veto deadline
    "effective",          # statutes take effect
    "other",
)

# Keyword tests, tried in order — first match wins, so the more specific
# patterns come first. Written against the phrasing the Legislature's own
# tentative calendar uses ("Last day for policy committees to hear and
# report to fiscal committees fiscal bills introduced in their house").
_CLASSIFIERS = (
    ("effective", r"statutes take effect"),
    ("recess", r"\brecess\b"),
    ("governor", r"governor to sign or veto|last day for the governor"),
    ("budget", r"budget bill"),
    ("introduction", r"last day for bills to be introduced|last day to be introduced"),
    ("two_year", r"odd-numbered year|two-year bill"),
    # ORDER IS THE WHOLE DESIGN HERE. Each of these phrases is a
    # substring of the one below it, so a general pattern tested first
    # swallows the specific case and mislabels it:
    #
    #   "…pass bills introduced in that house" is house-of-origin, but
    #   the floor_second pattern "last day for each house to pass bills"
    #   matches it too — so house_of_origin goes first.
    #
    #   "non-fiscal bills" contains "fiscal bills", so policy_nonfiscal
    #   goes before policy_fiscal.
    #
    #   Second-house deadlines are worded like own-house ones plus
    #   "other house"/"second house", so they go before both.
    ("house_of_origin", r"house of origin|pass bills introduced in that house"),
    ("fiscal_second", r"fiscal committees.*(?:second house|other house)"),
    ("policy_second", r"policy committees.*(?:second house|other house)"),
    ("floor_second", r"last day for each house to pass bills"),
    ("policy_nonfiscal", r"policy committees.*(?:non-?fiscal)"),
    ("policy_fiscal", r"policy committees.*fiscal bills"),
    ("fiscal_committee", r"fiscal committees"),
    ("policy_nonfiscal", r"policy committees"),
)

# Which bills a deadline threatens. "any" means every tracked bill in
# the session; the chamber-specific ones narrow it. Kept deliberately
# coarse — this app knows a bill's chamber from its number and its
# committee from its hearings, and does not know whether a bill is
# fiscal, so claiming to filter on that would be a guess dressed as a
# fact.
APPLIES_TO_ANY = "any"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "Jan. 31", "January 31", "Sept. 30", optionally behind a weekday.
#
# The month is matched against the KNOWN NAMES rather than as
# "[a-z]{3,9}", which is the same lesson code_sections.py records about
# code names — a shape is looser in exactly the wrong way. With a shape
# and a greedy optional weekday in front of it, "July 2" parsed as month
# "uly": the weekday wildcard ate the "J", "uly" satisfied the shape,
# and the row was silently dropped for having an unknown month. A
# vocabulary cannot do that.
_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|"
    "september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun"

_DATE_AT_START = re.compile(
    rf"^\s*(?:(?:{_WEEKDAYS})\.?,?\s+)?"
    rf"({_MONTH_NAMES})\.?\s+(\d{{1,2}})\b[.,]?\s*(.*)$",
    re.I,
)

def classify(text):
    """One calendar line's text -> its deadline kind."""
    lowered = (text or "").lower()
    for kind, pattern in _CLASSIFIERS:
        if re.search(pattern, lowered):
            return kind
    return "other"


def clean_label(text):
    """Strip the citations the published calendar carries — "(J.R. 61(a)
    (1))", "(Art. IV, Sec. 10(c))". They are authority, not information,
    and they crowd out the sentence on a narrow row."""
    # One level of nesting, because these citations have it:
    # "(Art. IV, Sec. 8(c))" and "(J.R. 61(a)(1))". A plain [^)]* stops
    # at the inner bracket and leaves the outer one stranded on the end
    # of the sentence.
    text = re.sub(
        r"\((?:J\.?\s?R\.?|Art\.?|Joint Rule|Gov\.? Code|Sec\.?)"
        r"[^()]*(?:\([^()]*\)[^()]*)*\)?",
        " ", text, flags=re.I)
    text = re.sub(r"\(\s*\)|\(\s*$", " ", text)
    text = re.sub(r"\s+([.,;)])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip(" .;,()")


def parse_calendar(text, year=None):
    """The Legislature's published tentative calendar -> deadline rows.

    Returns {"deadlines": [...], "warnings": [...]}. Each deadline is
    {date, label, kind}. The year is taken from the argument rather than
    from the text, because the calendar prints the month and day only —
    and a session spans two years, so a January date on a calendar for
    the 2025-26 session belongs to whichever year the caller says.

    Lines with no leading date are treated as continuations of the
    previous one, since the published calendar wraps long entries.
    """
    year = year or datetime.date.today().year
    found, warnings, unparsed = [], [], 0

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _DATE_AT_START.match(line)
        if not match:
            # A wrapped continuation of the previous entry, if there is
            # one — otherwise a line this parser can't place.
            if found:
                found[-1]["label"] = clean_label(f"{found[-1]['label']} {line}")
                found[-1]["kind"] = classify(found[-1]["label"])
            else:
                unparsed += 1
            continue

        month_word, day, rest = match.group(1), match.group(2), match.group(3)
        month = _MONTHS.get(month_word[:3].lower())
        if not month:
            unparsed += 1
            continue
        try:
            date = datetime.date(int(year), month, int(day))
        except ValueError:
            warnings.append(f"{month_word} {day} isn't a real date in {year}.")
            continue

        label = clean_label(rest)
        if not label:
            unparsed += 1
            continue
        found.append({
            "date": date.isoformat(),
            "label": label,
            "kind": classify(label),
        })

    if unparsed:
        warnings.append(
            f"{unparsed} line(s) had no date this could read and were skipped.")
    if not found:
        warnings.append(
            "No dated lines found. Each row should start with a month and day, "
            "e.g. “Jan. 31 Last day for each house to pass bills…”")
    return {"deadlines": found, "warnings": warnings}


def days_until(deadline_date, today=None):
    """Signed days from today to an ISO date. Negative once it's past,
    which is what lets a deadline sort alongside a hearing and an
    overdue filing on one queue."""
    today = today or datetime.date.today()
    try:
        target = datetime.date.fromisoformat(deadline_date)
    except (TypeError, ValueError):
        return None
    return (target - today).days
