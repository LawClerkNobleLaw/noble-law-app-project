"""
routing.py — who to address a letter to.

US-I2: drafting a position letter on a bill, the question is which
staffer in which office actually handles this. The directory knows who
covers which committee (directory.py); the bill knows which committee it
is in front of. This is the join.

── Where the bill's committee comes from ──

LegiScan's calendar rows, which this app already stores as
bill_hearings: `description` reads "Senate Education Hearing",
"Assembly Privacy And Consumer Protection Hearing". That is the
committee of reference, stated by the source, and it needs no new
column and no new API call.

The alternative was adding LegiScan's own `committee` field to the
bills table — a schema change, a shape_bill change and a refresh-path
change, to learn something the hearing row already says. Worth doing if
this outgrows the hearing (a bill referred but not yet set for hearing
has no calendar row and so no suggestion here), and deliberately not
done yet.

── The matching rule ──

Committee names never agree between the two sides. LegiScan writes
"Assembly Privacy And Consumer Protection"; the sheet's column header
says "Privacy & Consumer Protection", or "Approps", or just "Privacy".
So:

    EVERY significant token of the SHEET's label must be accounted for
    by some token of the BILL's committee — by prefix, or by one of the
    few named aliases below.

"Nat Res" matches "Natural Resources" on prefixes alone. "Approps"
matches "Appropriations" only because it is listed as an alias, since
"appropriations" does not in fact begin with "approps". "Privacy"
matches "Privacy And Consumer Protection" because a staffer whose
column says Privacy does handle that committee.

The "every token" part is what earns its keep: "Public Safety" does NOT
match "Public Employment and Retirement", which a looser any-token rule
would have accepted. A suggestion that sends a letter to the wrong
staffer is worse than no suggestion, because the user has no way to
tell that it is wrong.

── What it does not do ──

It does not address, send, or fill anything in. It offers names next to
the letter and stops, same boundary the rest of the drafting flow keeps
(see letter_drafts.py): the app never sends, so who a letter is
addressed to remains a decision the user types.
"""

import re


# Words that carry no distinguishing signal in a committee name. The
# chamber words are here because the chamber is matched separately and
# on purpose; leaving them in would let "Senate" alone satisfy a match.
_NOISE = {
    "committee", "committees", "hearing", "hearings", "subcommittee",
    "select", "standing", "joint", "special", "on", "of", "the", "and",
    "assembly", "senate", "floor", "session",
}

# Shortest token that may stand in for a longer one. Two characters
# ("pu", "co") match far too much to be a signal, so the prefix rule
# below ignores them — which is why the genuinely useful two-letter
# shortenings are named explicitly in _ALIASES instead.
_MIN_PREFIX = 3

# Capitol shorthand that the prefix rule cannot reach, because these are
# contractions rather than prefixes: "appropriations" does not start
# with "approps". Deliberately short and explicit — every entry here is
# a claim that two words mean the same committee, and a wrong one
# routes a letter to the wrong staffer silently. Add to it when a real
# sheet turns up shorthand this misses; do not guess.
#
# Shortenings that ARE prefixes need no entry and have none: "transpo",
# "jud", "nat res", "pub safety" and "elec" all match on their own.
_ALIASES = {
    "approps": "appropriations",
    "approp": "appropriations",
    "approps.": "appropriations",
    "ed": "education",
    "govt": "governmental",
    "gov": "governmental",
    "env": "environmental",
    "ins": "insurance",
    "util": "utilities",
    "transp": "transportation",
}

_CHAMBERS = ("Assembly", "Senate")


def _tokens(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [w for w in words if w not in _NOISE]


def chamber_of(text):
    """'Senate Education Hearing' -> 'Senate'."""
    lowered = (text or "").lower()
    for chamber in _CHAMBERS:
        if chamber.lower() in lowered:
            return chamber
    return ""


def committee_of(hearing):
    """One bill_hearings row -> (chamber, committee label as written).

    Reads `description` first and falls back to `location`, same order
    and same reason as letter_drafts._hearing_line: LegiScan puts the
    committee in one or the other depending on the row.
    """
    if not hearing:
        return "", ""
    text = (hearing.get("description") or hearing.get("location") or "").strip()
    if not text:
        return "", ""
    chamber = chamber_of(text)
    label = re.sub(r"\b(hearing|committee)s?\b", " ", text, flags=re.I)
    if chamber:
        label = re.sub(rf"\b{chamber}\b", " ", label, flags=re.I)
    label = re.sub(r"\s+", " ", label).strip(" ,-–—")
    return chamber, label


def matches(assignment_name, bill_committee):
    """Does a staffer's committee column cover this bill's committee?

    True only when every significant token of the assignment is
    accounted for in the bill's committee — see the module docstring on
    why the rule runs in that direction.
    """
    theirs = _tokens(assignment_name)
    bills = _tokens(bill_committee)
    if not theirs or not bills:
        return False
    return all(_token_matches(token, bills) for token in theirs)


def _token_matches(token, candidates):
    token = _ALIASES.get(token, token)
    for other in candidates:
        other = _ALIASES.get(other, other)
        if token == other:
            return True
        shorter, longer = sorted((token, other), key=len)
        if len(shorter) >= _MIN_PREFIX and longer.startswith(shorter):
            return True
    return False


def coverage(assignment_name, bill_committee):
    """How much of the bill's committee this assignment accounts for,
    0..1. Ranks an exact "Privacy And Consumer Protection" above a bare
    "Privacy" when both match."""
    bills = _tokens(bill_committee)
    if not bills:
        return 0.0
    theirs = _tokens(assignment_name)
    covered = sum(1 for token in bills if _token_matches(token, theirs))
    return covered / len(bills)


def suggest(legislators, hearing=None, sponsors=None, limit=12):
    """The directory plus a bill -> who to write to.

    `legislators` is db.search_directory's output; `hearing` is the
    bill's next calendar row; `sponsors` is bill_sponsors. Returns a
    flat, ranked list of {staff, legislator, reason, kind, score}.

    Two kinds of suggestion, kept distinct because they answer different
    questions. A COMMITTEE match is "this person handles this subject
    for their member" — the person a position letter is actually read
    by. An AUTHOR match is "this is the bill's own office", which is who
    you call about amendments. Merging them into one ranked list without
    saying which is which would leave the user unable to tell why any
    given name is on it.
    """
    chamber, committee = committee_of(hearing)
    author_names = {
        (s.get("name") or "").strip().lower()
        for s in (sponsors or [])
        if (s.get("name") or "").strip()
    }

    found = []
    for legislator in legislators or []:
        is_author = (legislator.get("full_name") or "").strip().lower() in author_names
        same_chamber = bool(chamber) and legislator.get("chamber") == chamber

        for staff in legislator.get("staff") or []:
            best = None
            for assignment in staff.get("assignments") or []:
                if assignment.get("kind") not in ("committee", "issue"):
                    continue
                if committee and matches(assignment["name"], committee):
                    score = coverage(assignment["name"], committee)
                    if best is None or score > best[0]:
                        best = (score, assignment["name"])

            if best:
                found.append({
                    "staff": staff, "legislator": legislator,
                    "kind": "committee",
                    "reason": f"Handles {best[1]}",
                    # Same-chamber first: the letter is going to the
                    # committee, and the committee sits in one house.
                    "score": best[0] + (1.0 if same_chamber else 0.0),
                })
            elif is_author:
                found.append({
                    "staff": staff, "legislator": legislator,
                    "kind": "author",
                    "reason": "In the author's office",
                    "score": 0.5,
                })

    # A committee match always outranks an author-office one: the letter
    # is addressed to the committee, and the author's staff are a second
    # call rather than the recipient.
    found.sort(key=lambda s: (s["kind"] != "committee", -s["score"],
                              s["staff"].get("full_name") or ""))
    return {
        "committee": committee,
        "chamber": chamber,
        "suggestions": found[:limit],
    }
