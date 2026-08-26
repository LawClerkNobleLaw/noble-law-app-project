# Disclosure form editor — in-place HTML editing before PDF export

Status: **implemented** (`disclosure_fields.py`, the `db.py`/`pdf_forms.py`
changes, and the new `/disclosures/review` editor in `app.py`). This doc
now records the design decisions the implementation follows, not a
future plan.

## Problem

Today, `/disclosures/review` (`app.py:4598`) is read-only: it embeds the
filled Form 601 PDF in an iframe and offers sign-off, nothing else.
`field_data` is a snapshot taken once, at draft-generation time
(`values_for_form_601()`), and never re-pulled from `profile`/`clients`
afterward. Fixing a wrong field today means leaving the review page,
editing the client record on `/clients`, and generating an entirely new
filing — there's no way to correct a draft in place.

## What this changes

Add an editable HTML form in front of the existing PDF-fill pipeline
(`pdf_forms.py` is unchanged). The lobbyist edits discrete fields — not a
single flowing textarea — that map to the same named PDF fields
`values_for_form_601()` already produces. The real, official PDF stays
the artifact that actually gets filed.

## Flow

```
edit fields (HTML form)  →  generate PDF  →  review PDF  →  sign off  →  export/print
```

- **No live PDF while editing.** The PDF is only (re)generated on an
  explicit action, not on every keystroke — `fill_form()` reruns pypdf
  over the whole template each call, so this isn't free.
- **Sign-off happens against the real generated PDF**, not the HTML form
  — required by the app's own stated promise on the marketing page
  (`app.py:1510`): *"The filled PDF is always shown for review first,
  and nothing is marked 'ready to file' until you type your legal name
  and confirm it yourself."*
- **Editing after a PDF's been generated invalidates that PDF** and
  blocks sign-off until it's regenerated — see "Staleness guard" below.
  Autosave fires on blur with a dirty-check (skip the write if nothing
  actually changed), but the dirty-check is a UX optimization only, not
  the safety mechanism.
- **Editing after sign-off is allowed** — it reopens the filing to
  `draft` and clears `signed_name`/`signed_at`, requiring a fresh PDF
  and fresh sign-off. No version history of the prior sign-off is kept
  in v1 — deliberately deferred, not an oversight. If a lobbyist ever
  needs a record of "what exactly did I attest to and when" before an
  edit overwrote it, that's a follow-up.

## Staleness guard (server-enforced, not client-trusted)

The client-side dirty-check is only there to avoid spamming autosave
calls. The actual guarantee — *sign-off can't happen against data that's
diverged from the reviewed PDF* — has to hold even against a buggy
client, a second tab, or someone hitting the API directly. So it's
enforced in the DB layer, not the UI:

1. Add a nullable `pdf_field_data_hash` column to `prepared_filings`.
2. The autosave write path always sets `pdf_field_data_hash = NULL` in
   the same `UPDATE` as the `field_data` write — unconditionally, every
   time. Staleness is the default state; "verified fresh" has to be
   earned.
3. The generate-PDF endpoint reads `field_data`, builds the PDF via
   `pdf_forms.fill_form()`, and in the same request computes
   `sha256(canonical_json(field_data))` into `pdf_field_data_hash`. This
   hash is server-computed only, never client-supplied.
4. `sign_off_prepared_filing()` (`db.py:684`) — already the single
   chokepoint every sign-off goes through — gets one more precondition
   before flipping status to `ready_to_file`: recompute the hash of the
   current `field_data` and require it to equal the stored
   `pdf_field_data_hash` (and require that column to be non-null at
   all). Mismatch raises the same kind of `ValueError` it already raises
   for a missing name/checkbox: *"This filing has changed since the PDF
   was generated — regenerate it before signing off."*

Open UX call, not a safety call: whether a hash mismatch at sign-off time
auto-triggers a regenerate on the frontend, or just surfaces as a
blocking error the lobbyist has to act on.

## Editing model

- **Discrete regions, not free text.** Each editable field maps 1:1 to a
  named AcroForm field, same shape as `values_for_form_601()`'s output
  dict.
- **Layout: plain labeled web form, grouped by section.** No attempt to
  visually replicate the real form's multi-page geometry — the PDF is
  generated separately, so there's no need for the HTML view to look
  like the PDF.
- **Data writeback: filing-only.** Edits update only that filing's
  `field_data` snapshot. They never write back to `clients`/`profile`.
  Known, accepted tradeoff: a correction made here doesn't carry forward
  to next quarter's filing — the lobbyist has to separately fix the
  client/profile record on `/clients` if they want it to stick.

## Validation

- Format + required-ness enforced **only on fields the app already
  sources from `profile`/`clients`** (business address, phone, email,
  lobbyist name, each client row's employer/effective date/period/
  description/agencies).
- The two structural gaps called out in `pdf_forms.py`'s docstring —
  Part II Section B (subcontracted clients) and additional individual
  lobbyists beyond the account holder — **stay out of scope.** The app
  has no data model for either today; making them "mandatory" would mean
  building new schema and new PDF field mappings, which is a real
  feature on its own, not a side effect of "add validation to the
  editor." They stay blank-and-disclosed via the existing "Known gaps"
  card (`app.py:4693`).

## >9 clients

`values_for_form_601()` currently does `clients[:9]` — if a firm has more
than 9 clients, the rest are silently dropped in whatever order
`list_clients()` returns. This editor removes that silence: the lobbyist
gets to see, choose, and reorder which clients land in the 9 available
rows when there are more than 9.

## Form-type scope

Architect the field/validation definitions **generically**, config-driven
per form type — not hardcoded the way `_CLIENT_ROW_FIELDS`/
`_HEADER_FIELDS` are today — so that adding Form 603 or 615 later is "add
a new config," not "rewrite the editor." That said, **v1 ships Form 601
only, end-to-end.** 603/615 field mappings themselves are a later,
separate effort — only the scaffolding needs to not preclude them.
