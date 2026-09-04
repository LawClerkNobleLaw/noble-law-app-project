"""
disclosure_fields.py — the editable-field schema for the disclosure
form's in-place HTML editor (see docs/disclosure-html-editor-plan.md).

Deliberately its own module, not folded into pdf_forms.py: pdf_forms.py's
own docstring says its only job is "given a dict of values, produce
filled PDF bytes" / "given our own data model, build that dict." This
module's job is different — "what does a human editing that dict see,
and which of its values are actually required before a PDF/sign-off is
allowed." Keyed by form_type throughout so a future Form 603/615 config
slots in as a new dict entry, without changing the editor UI or the
validation call sites in app.py.

Field `key` values are exactly the AcroForm field names used as keys in
a prepared_filing's field_data (see pdf_forms.values_for_form_601) — the
editor edits that same dict directly. There's no second, parallel data
model here.

Required-ness is scoped to the fields this app actually has a real data
source for today (the registrant's own business info) — it deliberately
does NOT make per-client effective date / contract period / agencies
lobbied mandatory, even though they're editable here. Those three stay
exactly as tolerant as the rest of the app already treats them (see the
"Known gaps" card in app.py's DISCLOSURE_REVIEW_BODY): pulled in
automatically when set on the client, but never required to be set. The
one per-row thing that IS effectively required is a row having a client
in it at all — enforced structurally by client_row_field_keys /
pdf_forms.client_row_values rather than by a "required" flag here, since
an included row's employer name always comes from clients.name.
"""

import re
from datetime import datetime, timedelta

import pdf_forms

_PHONE_AREA_RE = re.compile(r"^\d{3}$")
_PHONE_REST_RE = re.compile(r"^\d{3}-\d{4}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

_ROW_KEYS_BY_TYPE = {"601": ("employer", "description", "effective", "period", "agencies")}

FORM_601_SECTIONS = [
    {
        "key": "business",
        "label": "Business information",
        "fields": [
            {"key": "BUSINESS ADDRESS  Number and street", "label": "Street address", "kind": "text", "required": True, "source": "profile"},
            {"key": "BUSINESS ADDRESS  City", "label": "City", "kind": "text", "required": True, "source": "profile"},
            {"key": "BUSINESS ADDRESS  State", "label": "State", "kind": "text", "required": True, "source": "profile"},
            {"key": "BUSINESS ADDRESS  Zip", "label": "ZIP", "kind": "zip", "required": True, "source": "profile"},
            {"key": "TELEPHONE Area Code", "label": "Phone — area code", "kind": "phone_area", "required": True, "source": "profile"},
            {"key": "TELEPHONE", "label": "Phone — number (555-1234)", "kind": "phone_rest", "required": True, "source": "profile"},
            {"key": "MAILING ADDRESS  If different than above", "label": "Mailing address (if different)", "kind": "text", "required": False, "source": "profile"},
            {"key": "EMAIL", "label": "Email", "kind": "email", "required": True, "source": "account"},
            # Filled from the firm's roster on Profile (see
            # db.list_org_lobbyists), falling back to the registrant's
            # own legal name for a firm of one. Editable here, and only
            # the first slot is required: a 601 with no lobbyist named on
            # it isn't a registration.
            {"key": "INDIVIDUAL LOBBYISTS 1", "label": "Individual lobbyist 1", "kind": "text", "required": True, "source": "roster"},
            {"key": "INDIVIDUAL LOBBYISTS 2", "label": "Individual lobbyist 2", "kind": "text", "required": False, "source": "roster"},
            {"key": "INDIVIDUAL LOBBYISTS 3", "label": "Individual lobbyist 3", "kind": "text", "required": False, "source": "roster"},
            {"key": "INDIVIDUAL LOBBYISTS 4", "label": "Individual lobbyist 4", "kind": "text", "required": False, "source": "roster"},
            {"key": "INDIVIDUAL LOBBYISTS 5", "label": "Individual lobbyist 5", "kind": "text", "required": False, "source": "roster"},
            {"key": "INDIVIDUAL LOBBYISTS 6", "label": "Individual lobbyist 6", "kind": "text", "required": False, "source": "roster"},
        ],
    },
    {
        "key": "clients",
        "label": "Clients",
        # Rendered specially by the editor — one editable block per
        # client row, not a flat field list. See the module docstring
        # for why none of these are "required".
        "row_field_labels": {
            "employer": "Client name & address",
            "description": "Nature of lobbying interests",
            "effective": "Effective date",
            "period": "Period of contract",
            "agencies": "Agencies lobbied",
        },
    },
]

FORM_SECTIONS_BY_TYPE = {"601": FORM_601_SECTIONS}


# ── filing deadlines ────────────────────────────────────────────────
#
# What a form is due, relative to the event that starts its clock. Keyed
# by form_type like everything else here, so 603/615 (whose clocks are
# quarterly rather than event-driven) slot in as new entries.
#
# The date this produces is a CONVENIENCE, not an authority. The app
# can't know when a firm qualified — that's a fact about the world, so
# `trigger_date` is entered by the lobbyist and the due date is derived
# from it and left editable. Nothing here is computed from a draft's
# created_at: when someone opened a draft has no bearing on when the
# state needs the filing, and a deadline derived from it would be wrong
# in a way that looks authoritative. Same instinct as the "Known gaps in
# this draft" card — say what we don't know rather than fill it in.
FORM_DEADLINES = {
    "601": {
        "days_after_trigger": 10,
        "trigger_label": "Date the firm qualified",
        "trigger_help": "Registration is due within 10 days of qualifying as a lobbying firm. "
                        "Enter the qualifying date and the due date below is filled in for you — "
                        "check it against your own reading of the deadline.",
        "rule_label": "10 days after qualifying",
    },
}


# ── Where a pre-filled value came from (P1-21) ──────────────────────────
#
# Every field on this screen looks hand-typed, which is exactly the
# problem: a reviewer can't verify a business address without knowing
# whether it was pulled from the firm's Profile, typed here, or left over
# from an earlier edit. Sign-off on a form nobody can check is ceremony.
#
# Provenance is a property of the SCHEMA, not of the stored row — the
# initial field_data is built once by pdf_forms.values_for_form_601 from
# the profile, the account email and the firm's roster, and which of
# those fed which field never changes. So the source is declared on the
# field above, and the interesting question at render time is only
# whether the value still MATCHES its source.
#
# That comparison is what makes this reviewable rather than decorative.
# "From your Profile" under a value that no longer matches Profile would
# be a false statement on a compliance document.

FIELD_SOURCES = {
    "profile": {
        "label": "your registration profile",
        "href": "/signup/profile",
        "empty": "Nothing in your registration profile to fill this from.",
    },
    "account": {
        "label": "your account email",
        "href": "/profile",
        "empty": "No account email to fill this from.",
    },
    "roster": {
        "label": "your firm's roster",
        "href": "/profile",
        "empty": "Nobody on your firm's roster fills this slot.",
    },
}

# What the field is, once we know whether it still agrees with its
# source. Three states, because "typed here because the source was
# empty" and "typed here over what the source says" are different facts
# to a reviewer — the second one is the one worth looking at twice.
INHERITED = "inherited"
EDITED = "edited"
TYPED = "typed"
BLANK = "blank"


def provenance_for(form_type, field_data, source_values):
    """{field_key: {source, label, href, state, source_value}} for every
    field that HAS a source.

    source_values is what pdf_forms.values_for_form_601 produces from the
    firm's data as it stands right now — the same builder that filled
    this draft when it was created, run again. Comparing against it
    answers "is this still what Profile says", which is the question a
    reviewer actually has.

    Fields with no declared source (there are none on the 601 today, but
    a future form will have them) are simply absent from the result, and
    the editor renders no line for them rather than an empty one."""
    out = {}
    for field in _flat_fields(form_type):
        source_key = field.get("source")
        if not source_key:
            continue
        source = FIELD_SOURCES.get(source_key)
        if not source:
            continue
        current = (field_data.get(field["key"]) or "").strip()
        original = (source_values.get(field["key"]) or "").strip()
        if current and original and current == original:
            state = INHERITED
        elif current and original:
            state = EDITED
        elif current:
            state = TYPED
        else:
            state = BLANK
        out[field["key"]] = {
            "source": source_key,
            "label": source["label"],
            "href": source["href"],
            "empty_note": source["empty"],
            "state": state,
            "source_value": original,
        }
    return out


def field_issues(form_type, field_data):
    """The same problems validate_field_data() reports, as structured
    rows: {field_key, label, message, required}.

    Prose was enough while these were only ever shown after a rejected
    submit. The review screen shows them up front, with a jump link per
    row, and a link needs the key — so this is the primary form and
    validate_field_data() is now the sentence-shaped view of it."""
    issues = []
    for field in _flat_fields(form_type):
        value = (field_data.get(field["key"]) or "").strip()
        if field["required"] and not value:
            issues.append({
                "field_key": field["key"], "label": field["label"],
                "message": f"{field['label']} is required.", "required": True,
            })
            continue
        problem = validate_field(field["kind"], value)
        if problem:
            issues.append({
                "field_key": field["key"], "label": field["label"],
                "message": f"{field['label']} {problem}.", "required": False,
            })
    return issues


def valid_iso_date(value):
    """True if `value` is a real calendar date in ISO 'YYYY-MM-DD' form —
    what <input type="date"> submits. A regex isn't enough for a date a
    filing deadline is counted from: '2026-02-31' matches any sane
    pattern and isn't a day."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return True


def deadline_rule(form_type):
    """The deadline config for this form, or None if it has no rule yet."""
    return FORM_DEADLINES.get(form_type)


def due_date_for(form_type, trigger_date):
    """Derive a due date from the event that starts the clock. Returns
    None when the form has no rule or the trigger isn't a usable ISO
    date — a missing due date is a fine outcome here, an invented one
    isn't."""
    rule = FORM_DEADLINES.get(form_type)
    if not rule or not trigger_date:
        return None
    try:
        start = datetime.strptime(trigger_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (start + timedelta(days=rule["days_after_trigger"])).strftime("%Y-%m-%d")

_KIND_VALIDATORS = {
    "phone_area": (_PHONE_AREA_RE, "must be a 3-digit area code"),
    "phone_rest": (_PHONE_REST_RE, "must look like 555-1234"),
    "email": (_EMAIL_RE, "must be a valid email address"),
    "zip": (_ZIP_RE, "must be a 5-digit ZIP (or ZIP+4)"),
}


def sections_for_form_type(form_type):
    sections = FORM_SECTIONS_BY_TYPE.get(form_type)
    if sections is None:
        raise ValueError(f"Unknown form type: {form_type}")
    return sections


def _flat_fields(form_type):
    """Every non-client-row field across every section, flattened.
    Client-row fields aren't included — they repeat per row rather than
    appearing once, and are validated separately (see the module
    docstring on why they're never required)."""
    fields = []
    for section in sections_for_form_type(form_type):
        fields.extend(section.get("fields", []))
    return fields


def client_row_field_keys(form_type):
    """The real AcroForm field names (field_data keys) for every client
    row of this form type — the same names pdf_forms.client_row_values()
    writes. Used to recognize a per-field edit as a legitimate client-row
    field rather than an arbitrary/unknown key."""
    if form_type != "601":
        return set()
    row_keys = _ROW_KEYS_BY_TYPE[form_type]
    return {row[k] for row in pdf_forms.CLIENT_ROW_FIELDS for k in row_keys}


def is_editable_field_key(form_type, field_key):
    if field_key in client_row_field_keys(form_type):
        return True
    return any(f["key"] == field_key for f in _flat_fields(form_type))


def validate_field(kind, value):
    """Format-checks one non-blank value against its kind. Returns an
    error string, or None if it's fine. A blank value is never a format
    error here — required-ness is validate_field_data's job, so a field
    can be optional-and-blank or required-and-missing without this
    function being involved either way."""
    if not value:
        return None
    rule = _KIND_VALIDATORS.get(kind)
    if not rule:
        return None
    pattern, message = rule
    if not pattern.match(value.strip()):
        return message
    return None


def validate_field_data(form_type, field_data):
    """Returns a list of human-readable error strings — empty means this
    filing is complete enough to generate a PDF / be signed off on.
    Checks required-ness and format together so the caller gets the
    whole list at once instead of stopping at the first problem.

    The sentence-shaped view of field_issues() above; both report the
    same problems in the same order, so the banner on the review screen
    and the error a rejected submit returns can never disagree about
    what is wrong with a filing."""
    return [issue["message"] for issue in field_issues(form_type, field_data)]
