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

What's generated here is a first draft and nothing more. It fills what
the app can state as fact and scaffolds the rest:

  * The header block is pure fact — the bill number, the client, the
    position on record, the next hearing and its committee.

  * The argument paragraph now leads with fact too: the code sections
    the bill touches (bill_code_sections, read verbatim from its own
    Legislative Counsel preamble) and a plain-summary line from the
    bill's description — then a STRUCTURED FRAME for the client's case,
    with the client's actual rationale left as a bracketed blank. The
    app scaffolds the argument; it never invents the reason a client
    holds a position, because it doesn't know it. (This softens an
    earlier non-goal that left the whole argument blank — the facts and
    the shape are now provided; the judgment still isn't.)

Two non-goals remain absolute:

  * Nothing is regenerated. Once a letter exists, its body is whatever
    the user made it — this seed runs once, at creation. A "refresh from
    the bill" action would overwrite what they wrote, so there isn't one.

  * Nothing is sent. Same boundary the disclosure flow draws in so many
    words ("this app never files anything on your behalf"): a letter is
    printed or copied out by the person whose signature goes on it.

The tone is deliberately flat. A generated draft that arrives already
arguing is one the user has to disagree with before they can start, so
the frame states what the bill does and leaves the persuasion to the
person whose name goes under it.
"""

from datetime import datetime


# California code citations a bill touches, in the order a letter names
# them: what it changes, what it adds, what it strikes.
_ACTION_VERB = {"amend": "amend", "add": "add", "repeal": "repeal"}
_ACTION_ORDER = ("amend", "add", "repeal")

# How much of the bill's own description to carry into the draft. It's a
# summary line, not the letter's substance — a wall of pasted text is the
# machine output the flat tone exists to avoid — so it's trimmed to a
# sentence or two at a word boundary.
_DIGEST_CHARS = 300


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


def _join_and(items):
    """['a'] -> 'a'; ['a','b'] -> 'a and b'; ['a','b','c'] -> 'a, b and c'."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _sections_sentence(bill_label, sections):
    """"CA SB1159 would amend Section 1798.100 of the Civil Code and add
    Section 1798.99.80 of the Civil Code." — a factual reading of the code
    sections the bill's Legislative Counsel preamble said it touches
    (bill_code_sections). Groups by what it does (amend/add/repeal) then by
    code, so one clause covers all the sections a bill amends in a given
    code. Empty string when there are no parsed sections — the line drops,
    same as any other absent fact."""
    grouped = {}  # action -> code -> [section numbers, in stored order, unique]
    for s in sections or []:
        action, code, num = s.get("action"), s.get("code"), s.get("section")
        if action not in _ACTION_VERB or not code or not num:
            continue
        nums = grouped.setdefault(action, {}).setdefault(code, [])
        if num not in nums:
            nums.append(num)
    clauses = []
    for action in _ACTION_ORDER:
        for code, nums in grouped.get(action, {}).items():
            noun = "Section" if len(nums) == 1 else "Sections"
            clauses.append(f"{_ACTION_VERB[action]} {noun} {_join_and(nums)} of the {code}")
    if not clauses:
        return ""
    return f"{bill_label} would {_join_and(clauses)}."


def _digest_line(description):
    """The bill's own description, trimmed to a sentence or two at a word
    boundary and introduced as the summary it is. Empty when there's no
    description to carry."""
    text = " ".join((description or "").split())
    if not text:
        return ""
    if len(text) > _DIGEST_CHARS:
        text = text[:_DIGEST_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return f"In summary, {text}"


def _argument_block(bill_label, client_name, position, sections, description):
    """The paragraphs that replace the old single "[Why this matters]"
    placeholder: a facts paragraph (sections touched + a summary line),
    then a structured frame for the client's case with the rationale left
    as a bracketed blank. Support and oppose get their own framing; watch
    or an unknown position keeps the fully-open prompt, since there's no
    stance to frame around."""
    block = []

    facts = [_sections_sentence(bill_label, sections), _digest_line(description)]
    facts = [f for f in facts if f]
    if facts:
        block.append(" ".join(facts))

    who = client_name or "the client"
    if position == "support":
        block.append(
            f"For {who}, this bill would [the specific provision and how it helps — "
            "what it enables, clarifies, or protects]."
        )
    elif position == "oppose":
        block.append(
            f"For {who}, the provision of concern is [the specific section] — "
            "[its practical effect and what it would change]."
        )
    else:
        block.append(
            "[Why this bill matters to the client — the specific provision, "
            "the practical effect, and what it would change.]"
        )
    return block


def build_seed(bill, client=None, position=None, hearing=None, profile=None, sections=None):
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
        *_argument_block(bill_label, client_name, position, sections, bill.get("description")),
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
