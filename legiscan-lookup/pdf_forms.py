"""
pdf_forms.py — fills in real FPPC disclosure form PDFs with data already
stored in this app.

This is the one file in the whole app that isn't pure standard library —
it uses pypdf (see requirements.txt) to write values into the real
form's AcroForm fields. Everything else about "prepare my disclosure
form" (the review page, the sign-off step, what's stored where) lives in
app.py/db.py; this module's only job is "given a dict of values, produce
filled PDF bytes" (fill_form) and "given our own data model, build that
dict" (values_for_form_601).

Templates live in forms/ — real, official PDFs downloaded from FPPC
(fppc.ca.gov), not something this app generates from scratch. Form 601
(fppc.ca.gov/.../lobbying/601.pdf) is a genuine fillable AcroForm with
133 named fields; get_fields() names below were copied verbatim from
inspecting that real file, not guessed — several are inconsistently
named in the original PDF itself (e.g. "DESCRIPTION 1" vs
"DESCRIPTION_3"), which is why the ROW field-name lists below spell each
one out individually rather than building them from a pattern.

Known, real gaps between what this form asks for and what this app
actually collects — left blank rather than guessed, and surfaced in the
UI rather than hidden:
  - Part II Section B (subcontracted clients) — our data model has no
    concept of "lobbying through another firm's contract."
  - Individual lobbyists beyond the 22 slots this fills (the form's own
    six on Part I plus the first continuation block of sixteen). Past
    that the real form needs hand-written continuation sheets, which
    this app doesn't generate — same rule as the nine client rows.

Never touched, on purpose: the real `/Sig` field ("Signature_5"). This
app's sign-off is a typed name + a checkbox, recorded in
`prepared_filings` — that's an honest "I reviewed this" record, not a
digital signature, and filling a PDF's actual signature field would
misrepresent it as one.
"""

import os

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import FreeText

FORMS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forms")
FORM_601_TEMPLATE = os.path.join(FORMS_DIR, "form_601.pdf")

# A permanent, honest label on every filled form this app produces —
# present whether or not sign-off is complete, since this app never
# files anything itself either way (see app.py's /disclosures docs).
_DISCLAIMER = (
    "Prepared via Rotunda — draft for your own review. "
    "Not filed with the FPPC or Secretary of State. You must file this yourself."
)


def current_session_label(today):
    """CA's legislature runs on 2-year sessions starting in odd years
    (2025-2026, 2027-2028, ...). `today` is a datetime.date — passed in
    rather than computed here so this stays testable/deterministic."""
    year = today.year
    start = year if year % 2 == 1 else year - 1
    return f"{start}-{start + 1}"


def _split_phone(raw):
    """Best-effort split of a free-text phone number into (area_code,
    rest) for Form 601's two separate TELEPHONE fields. Falls back to
    putting the whole thing in `rest` if it doesn't look like a normal
    10-digit US number — better than guessing wrong digits."""
    if not raw:
        return "", ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return digits[:3], f"{digits[3:6]}-{digits[6:]}"
    return "", raw


def _mailing_address_line(profile):
    if profile.get("mail_same_as_bus"):
        return ""  # blank = "same as business address," same convention the real form uses
    parts = [profile.get("mail_addr1"), profile.get("mail_city"), profile.get("mail_st"), profile.get("mail_zip4")]
    return ", ".join(p for p in parts if p)


def _client_block(client):
    """"Employer's Name, Address and Telephone Number" is one multiline
    field on the real form — name on its own line, address below it."""
    addr = ", ".join(p for p in [client.get("bus_addr1"), client.get("bus_city"), client.get("bus_st"), client.get("bus_zip4")] if p)
    lines = [client.get("name") or ""]
    if addr:
        lines.append(addr)
    return "\n".join(lines)


# The real form spreads Part II Section A's 9 client rows across three
# pages with genuinely inconsistent field-name punctuation (some use a
# space before the row number, some an underscore) — copied verbatim
# from inspecting the template, not reconstructed from a pattern, so a
# typo here can't silently mismatch and leave a row unfilled.
#
# No leading underscore: disclosure_fields.py (the editable-field
# schema for the in-place HTML editor — see
# docs/disclosure-html-editor-plan.md) reads this to know which
# field_data keys are the editable client-row inputs, so this needs to
# be a real cross-module name, not a "private to this file" one.
CLIENT_ROW_FIELDS = [
    {"employer": "Employers Name Address and Telephone Number", "effective": "Effective Date",
     "period": "Period of Contract", "description": "DESCRIPTION 1", "agencies": "AGENCIES 1"},
    {"employer": "Employers Name Address and Telephone Number_2", "effective": "Effective Date_2",
     "period": "Period of Contract_2", "description": "DESCRIPTION 2", "agencies": "AGENCIES 2"},
    {"employer": "Employers Name Address and Telephone Number_3", "effective": "Effective Date_3",
     "period": "Period of Contract_3", "description": "DESCRIPTION_3", "agencies": "AGENCIES 3"},
    {"employer": "Employers Name Address and Telephone Number_4", "effective": "Effective Date_4",
     "period": "Period of Contract_4", "description": "DESCRIPTION 4", "agencies": "AGENCIES 4"},
    {"employer": "Employers Name Address and Telephone Number_5", "effective": "Effective Date_5",
     "period": "Period of Contract_5", "description": "DESCRIPTION_5", "agencies": "AGENCIES 5"},
    {"employer": "Employers Name Address and Telephone Number_6", "effective": "Effective Date_6",
     "period": "Period of Contract_6", "description": "DESCRIPTION 6", "agencies": "AGENCIES 6"},
    {"employer": "Employers Name Address and Telephone Number_7", "effective": "Effective Date_7",
     "period": "Period of Contract_7", "description": "DESCRIPTION 7", "agencies": "AGENCIES 7"},
    {"employer": "Employers Name Address and Telephone Number_8", "effective": "Effective Date_8",
     "period": "Period of Contract_8", "description": "DESCRIPTION 8", "agencies": "AGENCIES 8"},
    {"employer": "Employers Name Address and Telephone Number_9", "effective": "Effective Date_9",
     "period": "Period of Contract_9", "description": "DESCRIPTION 9", "agencies": "AGENCIES 9"},
]

# Every page repeats the firm's name in its own header field, and has
# its own "Page __ of __" pair — 5 real worksheet pages (the template's
# 2 instruction/example pages aren't counted in its own numbering).
HEADER_FIELDS = [
    {"name": "NAME OF LOBBYING FIRM_2", "page": "Page", "of": "of_2", "n": "1"},
    {"name": "NAME OF LOBBYING FIRM_4", "page": "Page2", "of": "of_4", "n": "2"},
    {"name": "NAME OF LOBBYING FIRM_5", "page": "Page3", "of": "of_5", "n": "3"},
    {"name": "NAME OF LOBBYING FIRM_6", "page": "Page4", "of": "of_6", "n": "4"},
    {"name": "NAME OF LOBBYING FIRM_7", "page": "Page5", "of": "of", "n": "5"},
]


def client_row_values(clients):
    """Builds the flattened {field_name: value} dict for all 9 client
    rows, given `clients` in the exact order they should fill rows
    1..9 — the caller decides that order (values_for_form_601 uses
    `list_clients`'s alphabetical order; the disclosure-editor's
    client-row picker lets the lobbyist choose/reorder it instead, see
    docs/disclosure-html-editor-plan.md).

    Always writes all 9 rows, blanking any row past len(clients) —
    important when this is called again after a *smaller* selection
    than before (e.g. the picker going from 9 clients down to 5): rows
    6-9 must be cleared, not left holding the previous selection's
    stale data."""
    values = {}
    for i, row in enumerate(CLIENT_ROW_FIELDS):
        client = clients[i] if i < len(clients) else None
        values[row["employer"]] = _client_block(client) if client else ""
        values[row["description"]] = (client.get("interests") or "") if client else ""
        values[row["effective"]] = (client.get("effective_date") or "") if client else ""
        values[row["period"]] = (client.get("contract_period") or "") if client else ""
        values[row["agencies"]] = (client.get("agencies_lobbied") or "") if client else ""
    return values


def max_client_rows():
    return len(CLIENT_ROW_FIELDS)


# Where a firm's lobbyists go on the real form: six slots on Part I,
# then a continuation block of sixteen. Named out individually rather
# than built from a pattern, same as HEADER_FIELDS — "INDIVIDUAL
# LOBBYISTS 1" and "PART I  Individual Lobbyists ContinuedRow1" are not
# the same naming scheme, and the second one has two spaces in it.
LOBBYIST_FIELDS = (
    ["INDIVIDUAL LOBBYISTS %d" % n for n in range(1, 7)]
    + ["PART I  Individual Lobbyists ContinuedRow%d" % n for n in range(1, 17)]
)


def values_for_form_601(profile, clients, account_email, sign_off=None, today=None,
                        lobbyists=None):
    """Builds the {field_name: value} dict for fill_form(FORM_601_TEMPLATE, ...).

    `profile` is a dict from db.get_profile(). `clients` is a list of
    dicts from db.list_clients() — only the first 9 are placed (the
    number of client rows the real form provides before it would need
    hand-written continuation sheets, which this app doesn't generate).
    `lobbyists` is the firm's roster (db.list_org_lobbyists) — this form
    exists to register a firm's lobbyists, and until an organization sat
    above the account there was only ever one name to put here. An empty
    roster still falls back to the registrant's own name, which is right
    for a firm of one and is what every existing filing was built from.
    `sign_off` is None (draft, nothing signed yet) or
    {"signed_name": ..., "signed_at": "YYYY-MM-DD"} — Part III's fields
    stay blank until this is provided, which is what makes an unsigned
    preview visibly incomplete on the form itself, not just in the app's
    UI around it."""
    area_code, phone_rest = _split_phone(profile.get("bus_phone"))
    values = {
        "Insert Years": current_session_label(today),
        "BUSINESS ADDRESS  Number and street": profile.get("bus_addr1") or "",
        "BUSINESS ADDRESS  City": profile.get("bus_city") or "",
        "BUSINESS ADDRESS  State": profile.get("bus_st") or "",
        "BUSINESS ADDRESS  Zip": profile.get("bus_zip4") or "",
        "TELEPHONE Area Code": area_code,
        "TELEPHONE": phone_rest,
        "MAILING ADDRESS  If different than above": _mailing_address_line(profile),
        "EMAIL": account_email or "",
    }

    names = [
        (l.get("name") or "").strip()
        for l in (lobbyists or [])
        if (l.get("name") or "").strip()
    ]
    if not names and profile.get("legal_name"):
        names = [profile["legal_name"]]
    for field, name in zip(LOBBYIST_FIELDS, names):
        values[field] = name
    for h in HEADER_FIELDS:
        values[h["name"]] = profile.get("legal_name") or ""
        values[h["page"]] = h["n"]
        values[h["of"]] = "5"

    values.update(client_row_values(clients[:9]))

    if sign_off:
        values["Name of responsible officer_1"] = sign_off["signed_name"]
        values["Name of Responsible Officer_5"] = sign_off["signed_name"]
        values["Executed On_5"] = sign_off["signed_at"]
        # Title_5 (the responsible officer's title) isn't collected
        # anywhere in this app either — left blank rather than guessed.

    return values


def fill_form(template_path, values):
    """Fills `template_path`'s AcroForm fields with `values` and returns
    the resulting PDF as bytes. Fields not present in `values` are left
    exactly as they were in the template (blank, for every field on a
    fresh form). Sets NeedAppearances so PDF viewers actually render the
    filled-in text — pypdf doesn't regenerate field appearance streams
    itself, and without this flag some viewers show the fields as blank
    even though the values are really there."""
    reader = PdfReader(template_path)
    writer = PdfWriter()
    writer.append(reader)

    for page in writer.pages:
        writer.update_page_form_field_values(page, values)
    writer.set_need_appearances_writer(True)

    writer.add_annotation(
        page_number=0,
        annotation=FreeText(
            text=_DISCLAIMER,
            rect=(36, 760, 400, 784),
            font="Helvetica",
            font_size="8pt",
            font_color="a3372c",
            border_color=None,
            background_color=None,
        ),
    )

    from io import BytesIO
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def fill_form_601(profile, clients, account_email, sign_off=None, today=None):
    values = values_for_form_601(profile, clients, account_email, sign_off=sign_off, today=today)
    return fill_form(FORM_601_TEMPLATE, values)


TEMPLATES_BY_FORM_TYPE = {"601": FORM_601_TEMPLATE}

# Which PDF fields to add at render time once a filing's been signed
# off — not stored in the filing's own field_data snapshot, since that
# snapshot is taken at draft-generation time, before sign-off exists.
_SIGN_OFF_FIELDS_BY_FORM_TYPE = {
    "601": lambda signed_name, signed_at: {
        "Name of responsible officer_1": signed_name,
        "Name of Responsible Officer_5": signed_name,
        "Executed On_5": (signed_at or "")[:10],
    },
}


def render_prepared_filing(filing):
    """Regenerates the actual PDF for a stored prepared_filings row
    (see db.py) — draft or signed, doesn't matter; Part III's fields
    just aren't in the value dict at all until sign-off adds them."""
    template = TEMPLATES_BY_FORM_TYPE.get(filing["form_type"])
    if not template:
        raise ValueError(f"Unknown form type: {filing['form_type']}")

    values = dict(filing["field_data"])
    if filing["status"] == "ready_to_file":
        extra = _SIGN_OFF_FIELDS_BY_FORM_TYPE.get(filing["form_type"])
        if extra:
            values.update(extra(filing["signed_name"], filing["signed_at"]))

    return fill_form(template, values)
