# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Rotunda ("Everything Under the Dome", formerly "BillWatch") — a web app for California
Capitol government-affairs work. **What exists today** is a working single-firm tool: track
LegiScan bill activity, flag bills per-client with a position (support/oppose/watch/seek
amendments/neutral) under a timestamped position history, get a daily change digest by email,
run saved searches that auto-adopt newly matching bills, cross-reference CAL-ACCESS lobbying
disclosures, draft position letters, and prepare (never file) FPPC disclosure forms.

**Where it's headed** is a broader product — see `docs/Rotunda_Concept_Summary.docx` (the
business/product concept) and `docs/roadmap.md` (the build sequence against this codebase).
Read those before proposing anything large, and not otherwise — `roadmap.md` alone is 400
lines. Treat the concept doc as an aspiration under active diligence, not a spec: several of
its pillars (hearing video, PAC contributions, "one-click" CAL-ACCESS e-filing, multi-state)
have unresolved legal or licensing questions recorded in `docs/roadmap.md`, and **nothing in
it is built until it's in this repo.** When a request maps to a concept-doc feature, say what
exists today rather than describing the doc's version as if it shipped.

Two top-level projects share one SQLite database:

- `legiscan-lookup/` — the actual web app (LegiScan tracking, accounts, clients, letters, disclosures)
- `calaccess-pipeline/` — a separate ingestion pipeline for California's CAL-ACCESS lobbying-disclosure data, writing into the same DB file so it can eventually be joined against bill data

Most work happens in `legiscan-lookup/`.

## Commands

All run from `legiscan-lookup/`:

```bash
./start.sh                              # run the app locally (http://localhost:8420)
python3 app.py                          # same, directly

pip3 install -r requirements-dev.txt    # one-time, for tests
pytest -q                               # the whole suite — always run this one
pytest tests/test_db.py                 # one file
pytest tests/test_db.py::test_flag_bill_adds_to_flagged_and_watchlist   # one test

python3 build_bill_corpus.py --dry-run   # what full-text search is missing
python3 build_bill_corpus.py             # top up the corpus (budgeted, resumable)
python3 build_bill_corpus.py --reparse   # re-derive code citations only (no API calls)
```

## Finding things without reading files

`app.py` is ~4,400 lines and `db.py` ~3,300. Reading either one whole costs more than most
tasks are worth, and neither rewards linear reading. Every index below regenerates from the
file itself, so none of them can go stale:

```bash
grep -n '# ──' app.py db.py          # the section index of both big files
grep -n 'parsed.path ==' app.py      # every route, in dispatch order
grep -n '_BODY = ' app.py            # the page constants
ls templates/ static/js/ tests/      # never enumerate these in prose — they change
```

Mechanical mappings, so there is nothing to look up:

- page constant `FOO_BODY` ⇄ `templates/foo_body.html`
- module `foo.py` ⇄ `tests/test_foo.py`
- a module's **why** is its own docstring: `sed -n '1,60p' bill_diff.py`. The module bullets
  under Architecture below are the tripwires only — the reasoning behind each one lives in
  the file, and is one cheap read away when it's actually needed.

## Established facts — don't re-check these

- `pytest -q` is ~640 tests in ~13 seconds and prints about five lines. Run the whole suite;
  never hand-pick files to save time, and never skip it to save tokens.
- Tests run against an in-memory SQLite DB (`conftest.py`'s `conn` fixture calls
  `db.init_db(conn=...)` on `sqlite3.connect(":memory:")` — the same schema/migration path a
  real boot uses). They never touch `db/billwatch.db`, never call LegiScan, and need no
  `LEGISCAN_API_KEY` or any other env var.
- No build step, and no linter, formatter or type checker is configured. There is nothing to
  run and nothing to add.
- `requirements.txt` has exactly one entry (`pypdf`, used only by `pdf_forms.py`). That is
  deliberate — see Scope boundaries.
- Templates and static files are read into constants **at import**, so editing one needs an
  app restart, same as editing the Python.

## Architecture

**Not Flask, not any framework.** `app.py`'s `Handler` class subclasses
`http.server.BaseHTTPRequestHandler` directly, served by a `ThreadingHTTPServer`. Routes are
manual `if parsed.path == "/foo":` checks inside `_do_GET`/`_do_POST`/`_do_DELETE` — no
`@app.route`, no blueprints, no middleware chain. Each dispatch block is divided by `# ──`
banners into sections by concern (flagged bills, clients and directory, disclosures, …), the
same convention `db.py` uses. **A new route goes inside the banner it belongs to**, not at the
end of the method — see the insertion-point rule under Working in this repo.

**Pages are HTML files under `templates/`, filled in by `_render_template()`.** Each page is a
module-level constant in `app.py` (`REPORT_BODY`, `LOOKUP_BODY`, …) read from
`templates/<lower_case_name>.html`. Placeholders are `{{name}}`, deliberately not
`str.format()`'s `{}` (every CSS brace would read as a format field) and not
`string.Template`'s `$name` (every JS `${...}` would collide); `_render_template()` raises at
boot on a slot with no value or a value with no slot. Shared helpers assemble the constants:
`page()` wraps `<html>`/`<head>`, `app_shell()` adds the sidebar+topbar chrome,
`account_widget()` is the one avatar/dropdown used everywhere. Page-specific interactivity is
an inline `<script>` in the same template, fetching that page's own `/api/...` JSON endpoint
rather than server-rendering data into the HTML.

**CSS and shared JS are real static files, served, not inlined.** `static/style.css` is the one
shared stylesheet (light/dark via `data-theme` + `prefers-color-scheme`, not a second
stylesheet); comments saying "see STYLE" mean that file. `static/js/` holds the blocks shared
across pages, pulled in with `<script src>` ahead of each page's own inline `<script>`. Both
are read into module-level constants at import and served by `Handler._send_static` from the
`STATIC_ASSETS` dict — an explicit name → bytes mapping, not a filesystem join, so there is no
path traversal to get wrong. Every URL carries `?v=<content hash>` (`_asset_url()`), which is
what makes the year-long `Cache-Control` on that route safe. **Adding a static file means
adding it to `STATIC_ASSETS`**, not just dropping it in the directory.

**Every module below `app.py` has one job, and `app.py` is the only thing that imports the
web-facing pieces together.** Each bullet is the constraint that would be expensive to
rediscover; the reasoning is in the module's docstring.

- `db.py` — all SQLite access for the app's own tables. `db/schema.sql` is the one canonical
  schema, every `CREATE TABLE` using `IF NOT EXISTS`, applied fresh on every boot
  (`init_db()`). **A new column goes in two places**: the `CREATE TABLE` in `schema.sql` (new
  DBs) *and* a guarded `ALTER TABLE ... ADD COLUMN` in `db.py`'s `_migrate()` (existing ones,
  which `IF NOT EXISTS` cannot reach). Follow the pattern there — check `PRAGMA table_info`,
  alter only if missing — rather than a numbered migrations folder.
- `bill_text.py` / `build_bill_corpus.py` — the searchable bill corpus behind `/lookup`'s
  "Full bill text" mode; `bill_texts` + an FTS5 external-content table kept in sync by triggers
  in `schema.sql`. **Deliberately separate from `bills`/`watchlist`** — an index joined against
  those could only find bills someone already found. Builder is budgeted and resumable
  (`--budget`, default 1,200) because a first full build is ~8,500 LegiScan calls; ongoing cost
  is two calls per bill that moved. **Current version only** — a deferral, not an omission.
- `bill_diff.py` — the redline between two versions (US-A4), behind "Compare versions" on
  `/report`. **On-demand, not indexed** (~24,600 calls and ~857MB to hold every version);
  cached in `bill_text_versions`, keyed by LegiScan `doc_id` and *not* org-scoped, since public
  bill text is the same for everyone. Every step is a click, because every step spends quota.
  Blocks are compared by **words, not characters** — character similarity pairs unrelated prose
  into a fake mutual rewrite.
- `code_sections.py` — which California code sections a bill touches, behind `/lookup`'s "Code
  section" mode. Parses **only the Legislative Counsel's preamble**, never the body. Code names
  match a **fixed 29-entry vocabulary**, not a `[A-Z][a-z]+ Code` shape. Derived at ingest, so
  `--reparse` re-derives with no API calls. **Ranges are stored, not expanded**; indirect
  amendment is out of scope.
- `deadlines.py` — the Legislature's deadline calendar (US-B3), on `/flagged/calendar` and the
  dashboard attention queue. `bill_hearings` says when a bill is heard; this says when it dies.
  **No dates are hard-coded in this repo and none may be** — they are set each session by house
  resolution. Firms paste the published calendar; `parse_calendar` reads it and the page shows
  what it read before storing. **Classification order is load-bearing** (one deadline's wording
  is a substring of another's).
- `directory.py` — the Capitol staff directory (`/directory`), imported from the crowdsourced
  wide sheet through a **guess-then-confirm** flow (`inspect()` proposes, the page confirms,
  `build_records()` applies). Unrecognised columns default to a committee assignment. **Org-
  scoped as a boundary, not a convenience**: real contact details for identifiable people, so
  no cross-org read and nothing seeded in the repo. An import **replaces** rather than merges
  (a sheet is a snapshot), but user-set stale flags carry across.
- `routing.py` — who to address a position letter to (US-I2). Reads the bill's committee off
  `bill_hearings.description` rather than adding a column. Match rule: **every** significant
  token of the sheet's label must be covered by the bill's committee. Strict on purpose — a
  wrong suggestion is worse than none. **Suggests only**: names and copy buttons, never
  addresses or sends.
- `vcard.py` — the directory as a `.vcf`/`.csv` (US-I5). vCard **3.0**, not 4.0 — 3.0 is what
  iOS/Android/Outlook all import without argument. Served from a URL rather than built in the
  browser, because on iOS navigating to a `.vcf` is what hands it to Contacts. Exports whatever
  the page is currently showing, filters included. An export, not a sync.
- `legiscan_client.py` — the only module that talks to LegiScan. `getSearch(bill=...)` is a
  precise, cheap, number-shaped match and already returns every type/chamber sharing a bare
  number (`72` → AB72/SB72/ACR72/…); `getSearch(query=...)` is noisy free-text relevance. Both
  return light rows — `getBill` is a separate, more expensive call, **never made per search
  result row**.
- `accounts.py` — password hashing/verification, sessions, login-lockout.
- `mailer.py` / `digest.py` — the daily "what changed" email. `digest.py` emails a user only
  when a diff (`db.snapshot_bill_state`/`db.diff_bill_state`) found a change on one of *their*
  flagged bills — no change, no email. `mailer.py` degrades to logging when SMTP env vars
  aren't set, rather than failing.
- `pdf_forms.py` — fills real FPPC PDF form fields (Form 601 first) via `pypdf`. The app
  **never files**; `db.sign_off_prepared_filing` is a human sign-off gate, not a submission.
- `config.py` — every environment variable in one place. `validate()` runs once at real startup
  (not at import, so tests importing `app`/`db` need no prod env) and raises listing every
  missing setting at once.

**Local vs. hosted refresh — same refresh code, two triggers.** Locally, `launchd` runs
`refresh_watchlist.py` / `calaccess-pipeline/refresh_calaccess.py` on a schedule, each opening
the DB file itself. Hosted on Render, cron services can't attach a persistent disk, so
`render.yaml` defines thin cron services that `curl` an internal, secret-gated endpoint
(`POST /internal/refresh-watchlist` / `/internal/refresh-calaccess`) on the one always-on web
service that *does* hold the disk; that endpoint runs the same code in a background thread.
`REFRESH_SECRET` unset (the local case) means those routes 404 and don't exist at all.

## Scope boundaries that are decisions, not gaps

Three properties of this codebase look like missing features but are deliberate. Changing any
of them is a product decision to raise with the user first, not a cleanup to do in passing.

- **Sharing is firm-wide, not per-matter.** `db.ORG_SCOPE` scopes almost every query to the
  user's *organization*, so a firm's flagged bills, clients, positions and notes are visible to
  every seat in that firm. The concept doc's US-D1/US-D3 want per-client isolation and
  per-matter user assignment inside a firm; that is a real future change (new access-control
  layer + a role model), not a bug in the current queries. Don't "fix" `ORG_SCOPE` toward
  per-user scoping without being asked.
- **The app never sends and never files.** `letter_drafts.py` produces a first draft and
  stops; `pdf_forms.py` fills a form and stops; `db.sign_off_prepared_filing` is a human
  sign-off gate. There is no outbound send path for letters and no e-file integration, on
  purpose. Any feature that would transmit something on the user's behalf needs an explicit
  decision, not just an endpoint.
- **Standard library only, plus `pypdf`.** No framework, no ORM, no HTTP client library, no
  LLM SDK — `requirements.txt` has one line and `legiscan_client.py` talks to LegiScan through
  `urllib`. The concept doc's AI-assisted drafting would be the first dependency that breaks
  this rule (and the first time client strategy leaves the machine); propose it explicitly
  rather than importing something.

## Working in this repo

Every change ships as its own branch + PR into `main` (see recent PR history) — branch off `origin/main`, not off whatever another branch happens to be checked out, and open a PR rather than committing straight to `main`.

**Add new code next to what it relates to, not at whatever anchor is convenient.** `app.py`'s
dispatch blocks and `db.py` are both sectioned by `# ──` banners, `db/schema.sql` groups a
feature's tables with a comment block, and `static/style.css` has a region per page. Put a new
route, function, table or rule inside the section it belongs to.

This is a merge-conflict rule as much as a tidiness one. Five parallel PRs (#65–#69) each
appended their new `db.py` functions immediately before `def code_section_stats(conn):` — one
170-line gap in a 3,000-line file — and produced **four separate conflicts**, every one of them
purely additive: independent blocks of new functions that git could not order because they
shared an insertion point and no surrounding context. Inserting each feature's queries beside
its own section instead would have merged cleanly with no rebase at all, and would not have
left the deadline-calendar section wedged into the middle of the bill-corpus one. The same
applies to `app.py`, `schema.sql` and `style.css`, where blind appending has the same effect.

When a conflict does happen this way, the fix is to keep both sides — check first that the two
sides define disjoint names (`def`/`CREATE TABLE`/route path/class selector), since a name
defined on both sides means it is a real conflict and not two additions.

**Rebase an open PR onto `main` as soon as a sibling PR merges**, rather than waiting for the
user to report "CONFLICTING". Parallel branches off one base all collide the moment the first
of them lands, and each subsequent merge re-conflicts the rest. Prefer independent PRs off
`origin/main`; stack branches only when a slice genuinely depends on unmerged work, since a
stacked PR has to be reviewed against its parent rather than against `main` and blocks
everything above it if one slice is deferred.

**This repo's working directory may be shared by more than one concurrent Claude Code session** (observed directly: a `git checkout`/`git reset --hard` from another session mid-task silently discarded this session's uncommitted edits, and separately, two sessions' uncommitted edits to the same file ended up swept into the same commit). Before trusting that your edits are still on disk, re-check with `git status`/`git diff` rather than assuming — and commit + push promptly once a change is verified working, rather than leaving substantial uncommitted work sitting in the working tree.

## Working style

The costs that matter, in order: how much of the repo gets read to find the thing, how long
the session runs, how many times finished work is re-verified. These rules attack all three.

- **Don't re-read a file to confirm an edit landed.** The edit would have failed loudly.
- **Don't echo back file contents, diffs, or code just written** unless asked to.
- **Report tests as the result line**, not the output. `638 passed in 12.9s` is the whole value.
- **Close with what changed and where, in a few lines** — not a walkthrough of the work.
- **Don't start the app to verify something the tests already cover.** Run it when the change
  is visual or interactive, and say so; otherwise the suite is the verification.
- `.claude/PROMPTING.md` is the other half of this, written for the user rather than for
  Claude. It doesn't need reading to do the work.
