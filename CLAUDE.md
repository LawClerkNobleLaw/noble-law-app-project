# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

Rotunda ("Everything Under the Dome", formerly "BillWatch") — a web app for California
Capitol government-affairs work. **What exists today** is a working single-firm tool: track
LegiScan bill activity, flag bills per-client with a position under a timestamped position
history, get a daily change digest by email, run saved searches that auto-adopt newly
matching bills, cross-reference CAL-ACCESS lobbying disclosures, draft position letters, and
prepare (never file) FPPC disclosure forms.

**Where it's headed** is a broader product — `docs/Rotunda_Concept_Summary.docx` (concept)
and `docs/roadmap.md` (build sequence). Read those before proposing anything large, and not
otherwise (roadmap.md alone is 400 lines). The concept doc is an aspiration under diligence,
not a spec — several pillars (hearing video, PAC contributions, one-click CAL-ACCESS
e-filing, multi-state) have unresolved legal/licensing questions in `roadmap.md`, and
**nothing in it is built until it's in this repo.** When a request maps to a concept-doc
feature, say what exists today rather than describing the doc's version as if it shipped.

Two top-level projects share one SQLite DB file:
- `legiscan-lookup/` — the actual web app. **Most work happens here.**
- `calaccess-pipeline/` — a separate ingestion pipeline for CAL-ACCESS lobbying data,
  writing into the same DB so it can eventually be joined against bill data.

## Commands

All run from `legiscan-lookup/`:

```bash
./start.sh                              # run the app locally (http://localhost:8420)
pip3 install -r requirements-dev.txt    # one-time, for tests
pytest -q                               # the whole suite — always run this one
python3 build_bill_corpus.py --dry-run  # what full-text search is missing
python3 build_bill_corpus.py            # top up the corpus (budgeted, resumable)
python3 build_bill_corpus.py --reparse  # re-derive code citations only (no API calls)
```

## Finding things without reading files

`app.py` is ~4,400 lines and `db.py` ~3,300 — reading either whole costs more than most
tasks are worth. Every index below regenerates from the file, so none can go stale:

```bash
grep -n '# ──' app.py db.py          # the section index of both big files
grep -n 'parsed.path ==' app.py      # every route, in dispatch order
grep -n '_BODY = ' app.py            # the page constants
ls templates/ static/js/ tests/      # never enumerate these in prose — they change
```

Mechanical mappings, so there's nothing to look up:
- page constant `FOO_BODY` ⇄ `templates/foo_body.html`
- module `foo.py` ⇄ `tests/test_foo.py`
- a module's **why** is its own docstring (`sed -n '1,60p' bill_diff.py`); the bullets under
  Architecture are the tripwires only.

## Established facts — don't re-check these

- `pytest -q` is ~640 tests in ~13s. Run the whole suite; never hand-pick files.
- Tests run against in-memory SQLite (`conftest.py`'s `conn` fixture, same schema/migration
  path as a real boot). They never touch `db/billwatch.db`, never call LegiScan, need no env.
- No build step, linter, formatter or type checker is configured — nothing to run or add.
- `requirements.txt` has one entry (`pypdf`, used only by `pdf_forms.py`) — deliberate.
- Templates and static files are read into constants **at import** — editing one needs an app
  restart, same as editing the Python.

## Architecture

**Not Flask, not any framework.** `app.py`'s `Handler` subclasses
`http.server.BaseHTTPRequestHandler`, served by `ThreadingHTTPServer`. Routes are manual
`if parsed.path == "/foo":` checks in `_do_GET`/`_do_POST`/`_do_DELETE`, divided by `# ──`
banners into sections by concern (the same convention `db.py` uses). **A new route goes
inside the banner it belongs to**, not at the end of the method.

**Pages are HTML under `templates/`, filled by `_render_template()`.** Each page is a
module-level constant in `app.py` (`REPORT_BODY`, …) read from `templates/<name>.html`.
Placeholders are `{{name}}` (not `str.format`/`string.Template`, which would collide with CSS
braces / JS `${...}`); `_render_template()` raises at boot on a slot with no value or a value
with no slot. Helpers assemble the constants: `page()` wraps `<html>`/`<head>`, `app_shell()`
adds sidebar+topbar, `account_widget()` is the shared avatar/dropdown. Page interactivity is
an inline `<script>` fetching that page's own `/api/...` JSON endpoint.

**CSS and shared JS are real static files, served not inlined.** `static/style.css` is the
one stylesheet (light/dark via `data-theme` + `prefers-color-scheme`). `static/js/` holds
shared blocks, pulled in with `<script src>` before each page's inline script. Both are read
into constants at import and served by `_send_static` from the `STATIC_ASSETS` dict (explicit
name → bytes, no filesystem join, no path traversal). Every URL carries `?v=<hash>`
(`_asset_url()`), which makes the year-long `Cache-Control` safe. **Adding a static file means
adding it to `STATIC_ASSETS`.**

**Every module below `app.py` has one job; `app.py` alone imports the web-facing pieces.**
Each bullet is the expensive-to-rediscover constraint; the reasoning is in the docstring.

- `db.py` — all SQLite access for the app's tables. `db/schema.sql` is the canonical schema,
  every `CREATE TABLE` using `IF NOT EXISTS`, applied fresh each boot (`init_db()`). **A new
  column goes in two places**: the `CREATE TABLE` in `schema.sql` *and* a guarded
  `ALTER TABLE ... ADD COLUMN` in `_migrate()` (check `PRAGMA table_info`, alter if missing) —
  not a numbered migrations folder.
- `bill_text.py` / `build_bill_corpus.py` — the searchable corpus behind `/lookup`'s
  full-text mode; `bill_texts` + an FTS5 external-content table kept in sync by triggers.
  Deliberately separate from `bills`/`watchlist`. Builder is budgeted/resumable. Current
  version only (a deferral, not an omission).
- `bill_diff.py` — the redline between two versions (behind "Compare versions" on `/report`).
  On-demand, not indexed; cached in `bill_text_versions` by `doc_id`, not org-scoped. Every
  step is a click because it spends quota. Blocks compared by **words, not characters**.
- `code_sections.py` — which CA code sections a bill touches. Parses **only the Legislative
  Counsel's preamble**; matches a **fixed 29-entry vocabulary**. Derived at ingest
  (`--reparse` needs no API calls). Ranges stored not expanded.
- `deadlines.py` — the Legislature's deadline calendar. **No dates are hard-coded and none
  may be** (set each session by house resolution); firms paste the published calendar,
  `parse_calendar` reads it, the page shows what it read before storing. Classification order
  is load-bearing (one deadline's wording is a substring of another's).
- `directory.py` — the Capitol staff directory, imported via a **guess-then-confirm** flow.
  Org-scoped as a boundary (real contact details); no cross-org read, nothing seeded. An
  import **replaces** rather than merges, but user-set stale flags carry across.
- `routing.py` — who to address a position letter to. Reads the committee off
  `bill_hearings.description`. Match rule: **every** significant token of the sheet's label
  must be covered by the bill's committee. Suggests only — never addresses or sends.
- `vcard.py` — the directory as `.vcf`/`.csv`. vCard **3.0** (what iOS/Android/Outlook
  import). Served from a URL. Exports whatever the page is showing.
- `legiscan_client.py` — the only module that talks to LegiScan. `getSearch(bill=...)` is a
  cheap number match (returns every type/chamber sharing a number); `getSearch(query=...)` is
  noisy free-text. Both return light rows — `getBill` is separate and expensive, **never made
  per search-result row**.
- `accounts.py` — password hashing, sessions, login-lockout.
- `mailer.py` / `digest.py` — the daily "what changed" email. `digest.py` emails a user only
  when a diff found a change on one of *their* flagged bills. `mailer.py` degrades to logging
  when SMTP env isn't set.
- `pdf_forms.py` — fills real FPPC PDF fields (Form 601) via `pypdf`. The app **never files**;
  `db.sign_off_prepared_filing` is a human sign-off gate.
- `config.py` — every env var in one place. `validate()` runs once at real startup (not at
  import, so tests need no prod env) and raises listing every missing setting at once.

**Local vs. hosted refresh — same code, two triggers.** Locally, `launchd` runs
`refresh_watchlist.py` / `calaccess-pipeline/refresh_calaccess.py` on a schedule. On Render,
`render.yaml` cron services `curl` a secret-gated endpoint
(`POST /internal/refresh-watchlist` / `/internal/refresh-calaccess`) on the one always-on web
service that holds the disk; it runs the same code in a thread. `REFRESH_SECRET` unset (the
local case) means those routes 404.

## Scope boundaries that are decisions, not gaps

Three properties look like missing features but are deliberate. Changing any is a product
decision to raise with the user first, not a cleanup.

- **Sharing is firm-wide, not per-matter.** `db.ORG_SCOPE` scopes almost every query to the
  user's organization. Per-client isolation / per-matter assignment (concept doc US-D1/US-D3)
  is a real future change (new access-control layer + role model), not a bug. Don't "fix"
  `ORG_SCOPE` toward per-user scoping unasked.
- **The app never sends and never files.** `letter_drafts.py` produces a first draft and
  stops; `pdf_forms.py` fills a form and stops; `sign_off_prepared_filing` is a sign-off
  gate. Any feature that transmits on the user's behalf needs an explicit decision.
- **Standard library only, plus `pypdf`.** No framework, ORM, HTTP client library, or LLM
  SDK; `legiscan_client.py` uses `urllib`. The concept doc's AI drafting would be the first
  dependency to break this (and the first time client strategy leaves the machine) — propose
  it explicitly.

## Working in this repo

Every change ships as its own branch + PR into `main` — branch off `origin/main`, not off
whatever is checked out.

**Add new code next to what it relates to, not at whatever anchor is convenient.** `app.py`'s
dispatch, `db.py`, `db/schema.sql` and `static/style.css` are all sectioned; put a new route,
function, table or rule inside the section it belongs to. This is a merge-conflict rule as
much as a tidiness one: parallel PRs that all append at the same insertion point produce
purely-additive conflicts git can't order, while inserting beside the related section merges
cleanly. When such a conflict does happen, **keep both sides** (check first that the two sides
define disjoint names — a name on both sides is a real conflict, not two additions).

**Rebase an open PR onto `main` as soon as a sibling merges**, rather than waiting for
"CONFLICTING". Prefer independent PRs off `origin/main`; stack only when a slice genuinely
depends on unmerged work.

**The working directory may be shared by more than one concurrent Claude Code session** — a
`git checkout`/`reset --hard` from another session has silently discarded uncommitted edits,
and two sessions' edits have been swept into one commit. Re-check `git status`/`git diff`
before trusting your edits are on disk, and commit + push promptly once a change works.

## Working style

The costs that matter, in order: how much of the repo gets read to find the thing, how long
the session runs, how many times finished work is re-verified.

- **Don't re-read a file to confirm an edit landed** — it would have failed loudly.
- **Don't echo back file contents, diffs, or code just written** unless asked.
- **Report tests as the result line**, not the output (`638 passed in 12.9s` is the value).
- **Close with what changed and where, in a few lines** — not a walkthrough.
- **Don't start the app to verify something the tests already cover.** Run it when the change
  is visual or interactive, and say so; otherwise the suite is the verification.
- `.claude/PROMPTING.md` is the other half of this, written for the user, not Claude.
