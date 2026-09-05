"""
vcard.py — the Capitol directory as something a phone will accept.

US-I5: a lobbyist walking into the building wants to call a staffer from
the hallway, not open a web app first. The directory already holds the
number; getting it onto the phone is a file format problem.

vCard 3.0 rather than 4.0, deliberately. 4.0 is the newer standard and
the one a purist would pick; 3.0 is the one iOS Contacts, Android and
Outlook all import without argument. This file exists to be opened by
whatever phone the user happens to have, so compatibility beats
currency.

Served from a URL rather than built in the browser (which is how the
flagged-bills CSV export works, see flagged_body.html). Two reasons: on
iOS, opening a .vcf URL hands straight off to Contacts — which IS the
feature — where a blob: download does not; and the escaping and folding
rules below are fiddly enough to be worth unit tests, which they can
only have on this side.

── Not a sync ──

The roadmap is explicit that ongoing sync is a different feature (a
CardDAV server, or an app) and a separate decision. This is a one-time
export: correct on the day it is taken, and stale afterwards in exactly
the way the source spreadsheet is. The file says so — every card's note
carries the "as of" date it was exported against, so a contact sitting
in someone's phone two years from now still says how old it is.
"""

import re


# vCard's own escape set (RFC 6350 §3.4, unchanged from 3.0 in
# practice): backslash first, or it would double-escape the escapes it
# is about to add.
_ESCAPES = (("\\", "\\\\"), (";", "\\;"), (",", "\\,"), ("\n", "\\n"))

# Lines are folded at 75 OCTETS, not characters — a name with an
# em-dash or an accent is more bytes than it looks, and a fold in the
# middle of a UTF-8 sequence produces a card that imports as mojibake.
_FOLD_OCTETS = 75


def escape(value):
    text = str(value or "")
    for char, replacement in _ESCAPES:
        text = text.replace(char, replacement)
    # Bare CR would end a line mid-value; it carries no meaning here.
    return text.replace("\r", "")


def fold(line):
    """One logical line -> the folded physical lines, CRLF-joined.

    Continuation lines begin with a single space, which the parser
    strips back off — so a continuation carries one octet less of
    payload than the first line does. Folds never split a UTF-8
    character: a cut mid-sequence imports as mojibake, and these names
    have accents in them.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= _FOLD_OCTETS:
        return line

    pieces = []
    start = 0
    while start < len(encoded):
        budget = _FOLD_OCTETS if not pieces else _FOLD_OCTETS - 1
        end = min(start + budget, len(encoded))
        # Walk back off any continuation byte so the cut lands on a
        # character boundary.
        while end > start + 1 and end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        pieces.append(encoded[start:end].decode("utf-8"))
        start = end
    return "\r\n ".join(pieces)


def split_name(full_name):
    """"J. Ramirez" -> ("Ramirez", "J."), for vCard's structured N.

    Last token is the family name and everything before it is given,
    which is right for the way these sheets are written and wrong for
    some real names. FN carries the name exactly as the sheet had it, so
    the display name is never the guess — only the sort key is.
    """
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], " ".join(parts[:-1])


def _line(name, value, params=""):
    return fold(f"{name}{params}:{escape(value)}")


def card(staff, legislator, as_of=None):
    """One staffer -> one VCARD block, as a list of physical lines."""
    family, given = split_name(staff.get("full_name"))
    office = (legislator or {}).get("full_name") or ""
    chamber = (legislator or {}).get("chamber") or ""
    district = (legislator or {}).get("district") or ""

    lines = ["BEGIN:VCARD", "VERSION:3.0"]
    # N is structured and has five components; an empty one is
    # "N:;;;;" and not "N:", which some importers reject outright.
    lines.append(fold(f"N:{escape(family)};{escape(given)};;;"))
    lines.append(_line("FN", staff.get("full_name") or ""))

    if office:
        # ORG is what groups these in a phone's contact list, so it
        # carries the office rather than "California Legislature" —
        # searching "Wicks" in Contacts should find her whole staff.
        org = f"Office of {office}"
        if chamber:
            org += f" ({chamber}{' ' + district if district else ''})"
        lines.append(_line("ORG", org))
    if staff.get("title"):
        lines.append(_line("TITLE", staff["title"]))
    if staff.get("email"):
        lines.append(_line("EMAIL", staff["email"], ";TYPE=INTERNET,WORK"))
    if staff.get("phone"):
        lines.append(_line("TEL", staff["phone"], ";TYPE=WORK,VOICE"))
    elif (legislator or {}).get("office_phone"):
        # The office line is better than no number at all, and is
        # labelled as the office's so nobody thinks it is a direct line.
        lines.append(_line("TEL", legislator["office_phone"], ";TYPE=WORK,VOICE"))

    note = note_for(staff, legislator, as_of)
    if note:
        lines.append(_line("NOTE", note))

    # CATEGORIES is what lets a phone or Outlook group the whole import
    # and, more usefully, delete it again when a newer sheet arrives.
    categories = ["Capitol"] + ([chamber] if chamber else [])
    lines.append(fold("CATEGORIES:" + ",".join(escape(c) for c in categories)))

    lines.append("END:VCARD")
    return lines


def note_for(staff, legislator, as_of):
    """What the card says beyond the name and number.

    The assignments, because "handles Approps and Health" is the reason
    this contact is worth having; the office's room, because that is
    where you go; and the "as of" date, because this file is a snapshot
    and will be wrong eventually. A card in someone's phone two years
    from now should be able to say how old it is.
    """
    parts = []
    assignments = staff.get("assignments") or []
    for kind, label in (("committee", "Committees"), ("caucus", "Caucuses"), ("issue", "Issues")):
        named = [a["name"] for a in assignments if a.get("kind") == kind]
        if named:
            parts.append(f"{label}: {', '.join(named)}")
    room = (legislator or {}).get("office_room")
    if room:
        parts.append(f"Capitol room {room}")
    if staff.get("is_stale"):
        parts.append("FLAGGED AS OUT OF DATE in Rotunda")
    if as_of:
        parts.append(f"From a directory current as of {as_of}")
    return "\n".join(parts)


def render(legislators, as_of=None):
    """The directory (as db.search_directory returns it) -> vCard text.

    CRLF throughout and a trailing one, which the spec requires and
    which some importers are strict about.
    """
    lines = []
    for legislator in legislators or []:
        for staff in legislator.get("staff") or []:
            lines.extend(card(staff, legislator, as_of=as_of))
    if not lines:
        return ""
    return "\r\n".join(lines) + "\r\n"


def count(legislators):
    return sum(len(l.get("staff") or []) for l in (legislators or []))


# ── The other export ────────────────────────────────────────────────
#
# A spreadsheet, for the half of this that isn't about phones: handing a
# client their coalition's contact list, or getting the directory into
# Outlook, or just working on it somewhere it can be sorted. Flat and
# one row per staffer, because that is the shape a spreadsheet is good
# at — the wide format this was imported FROM is the shape a person
# maintains, not the shape a person reads.
CSV_COLUMNS = [
    "Legislator", "Chamber", "District", "Party", "Capitol room", "Office phone",
    "Staffer", "Title", "Email", "Direct phone",
    "Committees", "Caucuses", "Issues", "Flagged stale", "Directory as of",
]


def csv_rows(legislators, as_of=None):
    """The directory as a header row plus one row per staffer."""
    rows = [CSV_COLUMNS]
    for legislator in legislators or []:
        for staff in legislator.get("staff") or []:
            assignments = staff.get("assignments") or []

            def named(kind):
                return "; ".join(a["name"] for a in assignments if a.get("kind") == kind)

            rows.append([
                legislator.get("full_name") or "",
                legislator.get("chamber") or "",
                legislator.get("district") or "",
                legislator.get("party") or "",
                legislator.get("office_room") or "",
                legislator.get("office_phone") or "",
                staff.get("full_name") or "",
                staff.get("title") or "",
                staff.get("email") or "",
                staff.get("phone") or "",
                named("committee"), named("caucus"), named("issue"),
                "yes" if staff.get("is_stale") else "",
                as_of or "",
            ])
    return rows


def render_csv(legislators, as_of=None):
    """CSV text, CRLF-terminated so Excel on Windows doesn't run the
    whole file onto one line."""
    import csv as _csv
    import io as _io

    buffer = _io.StringIO()
    writer = _csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(csv_rows(legislators, as_of=as_of))
    return buffer.getvalue()
