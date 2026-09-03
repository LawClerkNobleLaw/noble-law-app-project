"""
letter_drafts.py — what a new position letter starts out saying.

Its own module for the same reason disclosure_fields.py is: db.py stores
a subject and a body and knows nothing about how either is worded, and
app.py routes requests. This is the domain bit in between — given a bill,
a client and a position, what does the page hand the user before they
start typing.

The premise, from the product audit (P1-7): the Draft section contained
no drafting. Its only child was Disclosures, so the one thing a lobbyist
actually hands to a member's office — a page saying who we are, who we
represent, and what we want done with this bill — had to be written
somewhere else entirely, from data this app was already holding.

What's generated here is a first draft and nothing more. The header
block is filled because it is pure fact (the bill number, the client, the
position on record, the next hearing and its committee); the argument is
left to the person who has one. Two deliberate non-goals:

  * Nothing is regenerated. Once a letter exists, its body is whatever
    the user made it — a "refresh from the bill" action would overwrite
    what they wrote to restate what they already knew.

  * Nothing is sent. Same boundary the disclosure flow draws in so many
    words ("this app never files anything on your behalf"): a letter is
    printed or copied out by the person whose signature goes on it.

The tone is deliberately flat. A generated draft that arrives already
arguing is one the user has to disagree with before they can start, and
an over-written opener is the first thing a reader spots as machine
output.
"""

from datetime import datetime


POSITION_VERBS = {
    "support": "supports",
    "oppose": "opposes",
    "watch": "is monitoring",
}

POSITION_ASKS = {
    "support": "vote AYE",
    "oppose": "vote NO",
    "watch": None,
}


def _format_date(iso_date):
    """'2026-09-08' -> 'September 8, 2026'. Returns the input unchanged if
    it isn't a date — a hearing row's date comes from LegiScan and this is
    not the place to discover it was malformed."""
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%B %-d, %Y")
    except (TypeError, ValueError):
        return iso_date


def _hearing_line(hearing):
    """"Assembly Judiciary, June 30" — the committee and date a letter is
    written ahead of. LegiScan's calendar rows carry the committee in
    `location` for a hearing and sometimes only in `description`, so both
    are tried before giving up on naming one."""
    if not hearing:
        return ""
    where = (hearing.get("location") or hearing.get("description") or "").strip()
    when = _format_date(hearing.get("date"))
    return ", ".join(part for part in (where, when) if part)


def build_seed(bill, client=None, position=None, hearing=None, profile=None):
    """Returns {"subject", "body"} for a brand-new letter.

    Every argument is optional past the bill, because every one of them
    can genuinely be missing: a bill with no client assigned, a client
    with no position set yet, a bill with nothing scheduled. Each absence
    drops its line rather than leaving a blank to be filled in — a draft
    with "[COMMITTEE]" in it is a draft that ships with a placeholder in
    it eventually."""
    bill_label = f"{bill.get('state') or ''} {bill.get('bill_number') or ''}".strip() or "this bill"
    bill_title = (bill.get("title") or "").strip()
    client_name = (client or {}).get("name") or ""
    position = position or (client or {}).get("position") or ""
    verb = POSITION_VERBS.get(position, "is monitoring")
    ask = POSITION_ASKS.get(position)

    stance_word = {"support": "SUPPORT", "oppose": "OPPOSE", "watch": "WATCH"}.get(position, "")
    subject_parts = [bill_label]
    if stance_word:
        subject_parts.append(stance_word)
    if client_name:
        subject_parts.append(f"on behalf of {client_name}")
    subject = " — ".join(subject_parts)

    hearing_line = _hearing_line(hearing)

    # The header block: the facts, laid out the way a letter to a
    # member's office lays them out.
    header = [f"Re: {bill_label}"]
    if bill_title:
        header.append(f"    {bill_title}")
    if client_name:
        header.append(f"    Position: {stance_word or position.upper()} on behalf of {client_name}")
    if hearing_line:
        header.append(f"    Set for hearing: {hearing_line}")

    # lobbyist_profiles carries one name, legal_name, which is the firm's
    # for a firm registrant and the person's for an individual one (see
    # registrant_type in schema.sql). Printed once either way — a
    # signature block that repeats the same name twice reads as a bug.
    signer = (profile or {}).get("legal_name") or ""

    opening = "Dear Member:"
    if client_name:
        body_first = (
            f"I write on behalf of {client_name}, which {verb} {bill_label}"
            f"{', ' + bill_title.rstrip('.') if bill_title else ''}."
        )
    else:
        body_first = f"I write regarding {bill_label}{', ' + bill_title.rstrip('.') if bill_title else ''}."

    paragraphs = [
        body_first,
        "[Why this bill matters to the client — the specific provision, "
        "the practical effect, and what it would change.]",
    ]
    if ask:
        who = client_name or "we"
        paragraphs.append(
            f"For these reasons, {who} respectfully {'requests' if client_name else 'request'} "
            f"that you {ask} on {bill_label}."
        )
    else:
        paragraphs.append(
            f"We are following {bill_label} closely and will follow up as it moves."
        )
    paragraphs.append("Thank you for your consideration.")

    closing = ["Sincerely,"]
    if signer:
        closing.append(signer)

    body = "\n".join([
        *header,
        "",
        opening,
        "",
        *[p + "\n" for p in paragraphs],
        *closing,
    ]).rstrip() + "\n"

    return {"subject": subject, "body": body}
