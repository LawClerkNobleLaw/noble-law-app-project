"""
directory.py — reading a Capitol staff spreadsheet into structured
records.

The industry keeps this directory in a crowdsourced sheet: one row per
legislative office, and a wide run of columns naming, for each committee
and caucus and issue area, the staffer in that office who handles it.
Nobody agrees on the column names, several are blank, and the useful
half of the sheet is columns this app has never heard of. US-I1 asks to
import that "rather than requiring a rigid template", which rules out
the usual approach of naming the columns in advance.

So the shape here is: **guess, show the guess, let the user correct it.**
`inspect()` reads the header row and proposes a role for every column;
the page renders those proposals as dropdowns; `build_records()` applies
whatever mapping comes back. The parser never insists — an unrecognised
column becomes an assignment column, which is the right default because
in this sheet most columns are.

── Why an unknown column defaults to "committee assignment" ──

A cell under "Appropriations" holding "J. Ramirez" means Ramirez handles
Appropriations for that office. That is the sheet's whole point and its
most common column by a wide margin, so treating unknown columns as
noise would throw away the data the import exists to capture. Columns
the user really doesn't want come back mapped to "ignore".

── What this does not do ──

It does not fetch, scrape, or ship a directory. Every row comes from a
file the firm already had (see the schema.sql note): the sheet is
somebody else's crowdsourced work and holds personal contact details for
identifiable people, so importing a firm's own copy into its own account
is the whole of what's on offer here.
"""

import csv
import io
import re


# What a column can be. Everything except ASSIGNMENT_KINDS maps to one
# field on one record; the assignment kinds map to a row per non-empty
# cell in that column.
COLUMN_ROLES = (
    "ignore",
    "legislator",
    "chamber",
    "district",
    "party",
    "office_room",
    "office_phone",
    "staff_name",
    "staff_role",
    "staff_title",
    "staff_email",
    "staff_phone",
    "committee",
    "caucus",
    "issue",
)

ASSIGNMENT_KINDS = ("committee", "caucus", "issue")

# The wide sheet's one real rule: the HEADER names a thing and the CELL
# names the person who has it. Which thing depends on the column —
# a committee, a caucus, an issue area, or a job. "staff_role" is that
# last case: a column headed "Chief of Staff" whose cells are names
# means those people are that office's chief of staff, so the header
# becomes their title rather than an assignment. Without it the title
# sitting in plain sight at the top of the column gets thrown away.
HEADER_NAMES_THE_THING = ASSIGNMENT_KINDS + ("staff_role",)

# Header text -> role, matched loosely (case, punctuation and filler
# words are stripped first). Ordered longest-key-first at match time so
# "staff email" can't be decided by the "email" rule.
_HEADER_HINTS = {
    "legislator": "legislator",
    "member": "legislator",
    "office": "legislator",
    "name": "legislator",
    "senator": "legislator",
    "assemblymember": "legislator",
    "chamber": "chamber",
    "house": "chamber",
    "body": "chamber",
    "district": "district",
    "ad": "district",
    "sd": "district",
    "party": "party",
    "room": "office_room",
    "capitol room": "office_room",
    "office room": "office_room",
    "phone": "office_phone",
    "office phone": "office_phone",
    "capitol phone": "office_phone",
    "staff": "staff_name",
    "staffer": "staff_name",
    "staff name": "staff_name",
    "contact": "staff_name",
    # Columns headed with a job rather than with the word "staff" — the
    # crowdsourced sheet's usual first few columns.
    "chief of staff": "staff_role",
    "cos": "staff_role",
    "scheduler": "staff_role",
    "legislative director": "staff_role",
    "district director": "staff_role",
    "communications": "staff_role",
    "press secretary": "staff_role",
    "legislative aide": "staff_role",
    "legislative assistant": "staff_role",
    "field representative": "staff_role",
    "consultant": "staff_role",
    "title": "staff_title",
    "position": "staff_title",
    "role": "staff_title",
    "email": "staff_email",
    "e mail": "staff_email",
    "staff email": "staff_email",
    "staff phone": "staff_phone",
    "direct phone": "staff_phone",
    "cell": "staff_phone",
    "mobile": "staff_phone",
    "caucus": "caucus",
    "issue": "issue",
    "issue area": "issue",
    "subject": "issue",
    "committee": "committee",
}


def _normalize_header(text):
    """"Capitol Room #" -> "capitol room". Punctuation out, case down,
    runs of space collapsed — so a sheet's cosmetic differences don't
    each need their own hint."""
    text = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def guess_role(header):
    """One header -> the role it probably is.

    Exact normalized match first, then longest containing hint, then the
    assignment default. Longest-first matters: "staff email" must not be
    decided by "email" (which would make it the staffer's email anyway,
    but "office phone" decided by "phone" would be wrong the other way).
    """
    normalized = _normalize_header(header)
    if not normalized:
        return "ignore"
    if normalized in _HEADER_HINTS:
        return _HEADER_HINTS[normalized]
    for hint in sorted(_HEADER_HINTS, key=len, reverse=True):
        if hint in normalized:
            return _HEADER_HINTS[hint]
    # See the module docstring: in this sheet, a column nobody
    # recognises is nearly always a committee whose name is the header.
    return "committee"


def read_csv(text):
    """CSV text -> (headers, rows-as-lists). Tolerates the tab-separated
    export people get from pasting a sheet, since "download as CSV" and
    "copy the cells" produce different things and both arrive here."""
    if not text or not text.strip():
        return [], []
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any((cell or "").strip() for cell in row)]
    if not rows:
        return [], []
    return [(h or "").strip() for h in rows[0]], rows[1:]


def inspect(text, max_preview=5):
    """What the mapping screen needs: the headers, the guessed role for
    each, and a few real rows so the user can see what they're mapping
    rather than reasoning about column names alone."""
    headers, rows = read_csv(text)
    if not headers:
        return {"columns": [], "preview": [], "row_count": 0}
    columns = [
        {"index": i, "header": header, "role": guess_role(header)}
        for i, header in enumerate(headers)
    ]
    preview = [
        [(cell or "").strip() for cell in row[:len(headers)]]
        for row in rows[:max_preview]
    ]
    return {"columns": columns, "preview": preview, "row_count": len(rows)}


def _cell(row, index):
    if index is None or index >= len(row):
        return ""
    return (row[index] or "").strip()


def _split_names(value):
    """"J. Ramirez / A. Chen" -> two names.

    A cell in an assignment column often names more than one staffer,
    because two people share a portfolio. Splitting on the separators
    people actually use beats storing "J. Ramirez / A. Chen" as one
    person nobody can call.
    """
    parts = re.split(r"\s*(?:/|;|\||,| and | & )\s*", value)
    return [p.strip() for p in parts if p.strip()]


# A cell that means "nothing here" in the sheets people keep.
_EMPTY_MARKERS = {"", "-", "--", "n/a", "na", "none", "tbd", "vacant", "?"}


def _is_empty(value):
    return (value or "").strip().lower() in _EMPTY_MARKERS


def build_records(text, mapping):
    """CSV text plus {column index: role} -> the records to store.

    Returns {"legislators": [...], "warnings": [...]}, each legislator
    carrying its own "staff" list, each staffer its "assignments". One
    nested structure rather than three flat lists because the caller
    writes them in that order anyway (a staffer needs its legislator's
    id), and flattening here would only mean rebuilding the nesting
    there.

    Two shapes of sheet come out of this correctly, which is why the
    staff record is assembled per row rather than per column:

      * one row per OFFICE, with assignment columns naming staff — the
        crowdsourced format, where each named staffer becomes a record;
      * one row per STAFFER, with a legislator column repeating — the
        format a firm's own address book export usually has, where the
        row's own staff_name column is the record.
    """
    headers, rows = read_csv(text)
    if not headers:
        return {"legislators": [], "warnings": ["That file had no rows in it."]}

    mapping = {int(k): v for k, v in (mapping or {}).items() if v in COLUMN_ROLES}
    first = {}
    for index, role in mapping.items():
        first.setdefault(role, index)

    if "legislator" not in first:
        return {"legislators": [],
                "warnings": ["No column is mapped to the legislator's name, so there is "
                             "nothing to file these contacts under."]}

    by_legislator = {}
    order = []
    warnings = []
    skipped = 0

    for row in rows:
        name = _cell(row, first.get("legislator"))
        if _is_empty(name):
            skipped += 1
            continue
        key = name.lower()
        if key not in by_legislator:
            by_legislator[key] = {
                "full_name": name,
                "chamber": _chamber(_cell(row, first.get("chamber")),
                                    _cell(row, first.get("district"))),
                "district": _cell(row, first.get("district")),
                "party": _cell(row, first.get("party")),
                "office_room": _cell(row, first.get("office_room")),
                "office_phone": _cell(row, first.get("office_phone")),
                "staff": [],
            }
            order.append(key)
        legislator = by_legislator[key]

        # The row's own staffer, if the sheet has one. Its email and
        # phone belong to this person and to nobody named in an
        # assignment column, so they are only ever attached here.
        row_staff_name = _cell(row, first.get("staff_name"))
        row_staff = None
        if not _is_empty(row_staff_name):
            row_staff = _staff_for(legislator, row_staff_name)
            row_staff["title"] = row_staff["title"] or _cell(row, first.get("staff_title"))
            row_staff["email"] = row_staff["email"] or _cell(row, first.get("staff_email"))
            row_staff["phone"] = row_staff["phone"] or _cell(row, first.get("staff_phone"))

        for index, role in mapping.items():
            if role not in HEADER_NAMES_THE_THING:
                continue
            value = _cell(row, index)
            if _is_empty(value):
                continue
            label = headers[index].strip() if index < len(headers) else ""
            if role == "staff_role":
                # Header is the job, cell is who holds it. Two people in
                # one cell both hold it (an office with co-chiefs, or a
                # sheet listing an outgoing and incoming staffer).
                for staff_name in _split_names(value):
                    staff = _staff_for(legislator, staff_name)
                    staff["title"] = staff["title"] or label
                continue
            # The column header names the committee; the cell names who
            # covers it. A sheet with a generic "Committee" header
            # instead puts the committee's name in the cell, so that is
            # what gets recorded when the header carries no name of its
            # own.
            generic = _normalize_header(label) in ("committee", "caucus", "issue", "issue area")
            if generic:
                if row_staff is not None:
                    _assign(row_staff, role, value)
                continue
            for staff_name in _split_names(value):
                _assign(_staff_for(legislator, staff_name), role, label)

    if skipped:
        warnings.append(f"{skipped} row(s) had no legislator name and were skipped.")
    legislators = [by_legislator[key] for key in order]
    if not legislators:
        warnings.append("No rows had a legislator name in the mapped column.")
    return {"legislators": legislators, "warnings": warnings}


def _staff_for(legislator, name):
    """This office's record for `name`, created on first sight.

    Matched on the name as written, case-insensitively. A staffer named
    the same way in six assignment columns is one person with six
    assignments, which is the entire reason the wide sheet is worth
    importing rather than reading.
    """
    key = name.strip().lower()
    for staff in legislator["staff"]:
        if staff["full_name"].strip().lower() == key:
            return staff
    staff = {"full_name": name.strip(), "title": "", "email": "", "phone": "",
             "assignments": []}
    legislator["staff"].append(staff)
    return staff


def _assign(staff, kind, name):
    entry = {"kind": kind, "name": name.strip()}
    if entry["name"] and entry not in staff["assignments"]:
        staff["assignments"].append(entry)


def _chamber(stated, district):
    """'Assembly' / 'Senate', from whatever the sheet said.

    Falls back to the district's own prefix, since "AD-12" and "SD-4"
    carry the chamber and a sheet with a district column often has no
    separate one for it.
    """
    text = (stated or "").strip().lower()
    if text.startswith("a") or "assembly" in text:
        return "Assembly"
    if text.startswith("s") or "senate" in text:
        return "Senate"
    district = (district or "").strip().lower()
    if district.startswith("ad"):
        return "Assembly"
    if district.startswith("sd"):
        return "Senate"
    return ""
