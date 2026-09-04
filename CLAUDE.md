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
Read those before proposing anything large. Treat the concept doc as an aspiration under
active diligence, not a spec: several of its pillars (hearing video, PAC contributions,
"one-click" CAL-ACCESS e-filing, multi-state) have unresolved legal or licensing questions
recorded in `docs/roadmap.md`, and **nothing in it is built until it's in this repo.** When a
request maps to a concept-doc feature, say what exists today rather than describing the doc's
version as if it shipped.

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
pytest                                  # run the full suite
pytest tests/test_db.py                 # one file
pytest tests/test_db.py::test_flag_bill_adds_to_flagged_and_watchlist   # one test
```

No build step, no linter/formatter configured. `requirements.txt` has exactly one entry (`pypdf`, used only by `pdf_forms.py`) — everything else in the app is the Python standard library on purpose (see that file's own comment).

Tests run entirely against an in-memory SQLite DB (`conftest.py`'s `conn` fixture calls `db.init_db(conn=...)` on `sqlite3.connect(":memory:")` — the exact same schema/migration path a real boot uses). They never touch `db/billwatch.db` or call LegiScan, and need no `LEGISCAN_API_KEY`.

## Architecture

**Not Flask, not any framework.** `app.py`'s `Handler` class subclasses `http.server.BaseHTTPRequestHandler` directly, served by a `ThreadingHTTPServer`. Routes are manual `if parsed.path == "/foo":` checks inside `_do_GET`/`_do_POST`/`_do_DELETE` — there's no `@app.route`, no blueprints, no middleware chain. When adding a route, find the right method's dispatch block and add another `if` (existing ones are grouped by concern, not alphabetically). This is a single ~2900-line file; use grep for `parsed.path ==`, a page-body constant name (`REPORT_BODY`, `FLAGGED_BODY`, etc.), or a function name rather than trying to read it linearly.

**Pages are HTML files under `legiscan-lookup/templates/`, filled in by `_render_template()`.** Each page is a module-level constant in `app.py` (e.g. `REPORT_BODY`, `LOOKUP_BODY`, `CLIENTS_BODY`) whose value is read from `templates/<lower_case_name>.html` **once at import**, not per request — so editing a template needs a restart, same as editing the Python. Placeholders are `{{name}}`, deliberately not `str.format()`'s `{}` (every CSS brace would read as a format field) and not `string.Template`'s `$name` (every JS `${...}` would collide); `_render_template()` raises at boot on a slot with no value or a value with no slot. Those constants are assembled by shared helpers (`page()` wraps `<html>`/`<head>`, `app_shell()` adds the sidebar+topbar chrome, `account_widget()` is the one avatar/dropdown component used everywhere). Page-specific interactivity is still an inline `<script>` in the same template, fetching the page's own `/api/...` JSON endpoint client-side rather than server-rendering data into the HTML.

**CSS and shared JS are real static files, served, not inlined.** `static/style.css` is the one shared stylesheet (light/dark via `data-theme` + `prefers-color-scheme`, not a second stylesheet); comments in `app.py` that say "see STYLE" mean that file. `static/js/` holds the blocks shared across pages (`bill_tables`, `bill_clients`, `bill_status`, `client_quickadd`, `confirm_delete`, `hearing_time`, `title_case`, `row_menu`), pulled in with `<script src>` ahead of each page's own inline `<script>`. Both are read into module-level constants at import and served by `Handler._send_static` from the `STATIC_ASSETS` dict — an explicit name → bytes mapping, not a filesystem join, so there's no path traversal to get wrong. Every URL carries `?v=<content hash>` (`_asset_url()`), which is what makes the year-long `Cache-Control` on that route safe. **Adding a static file means adding it to `STATIC_ASSETS`**, not just dropping it in the directory.

**Every module below `app.py` has one job, and `app.py` is the only thing that imports the web-facing pieces together:**
- `db.py` — all SQLite access for the app's own tables (users, clients, bills, flagged_bills, bill_client_links, prepared_filings, ...). `db/schema.sql` is the one canonical schema, every `CREATE TABLE` using `IF NOT EXISTS`, applied fresh on every boot (`init_db()`). Since `CREATE TABLE IF NOT EXISTS` can't add a column to an already-existing table, any new column goes in **two** places: the `CREATE TABLE` in `schema.sql` (for brand-new DBs) *and* a guarded `ALTER TABLE ... ADD COLUMN` in `db.py`'s `_migrate()` (for existing ones) — follow the existing pattern there (check `PRAGMA table_info`, only alter if the column's missing) rather than a numbered migrations folder.
- `legiscan_client.py` — the only module that talks to LegiScan's API. Two distinct search modes matter here: `getSearch(bill=...)` is a precise, cheap, number-shaped match (and — checked live — already returns every bill type/chamber sharing a bare number, e.g. searching `72` returns AB72/SB72/ACR72/etc. at once); `getSearch(query=...)` is free-text relevance search and is comparatively noisy for a number-shaped query. Both return lightweight list rows; full detail (`getBill`) is a separate, more expensive call, only made per-bill, never per search result row.
- `accounts.py` — password hashing/verification, sessions, login-lockout.
- `mailer.py` / `digest.py` — the daily "what changed" email. `digest.py` only emails a user when a diff (`db.snapshot_bill_state`/`db.diff_bill_state`) actually found a change on one of *their* flagged bills — no digest, no email. `mailer.py` degrades to logging instead of sending when SMTP env vars aren't set, rather than failing.
- `pdf_forms.py` — fills real FPPC PDF form fields (starts with Form 601) via `pypdf`. The app **never files** anything; it only prepares a document for the user to review and file themselves (`db.sign_off_prepared_filing` is an explicit human sign-off gate, not a submission).
- `config.py` — every environment variable the app reads, in one place. `validate()` is called once at real startup (not at import time, so tests importing `app`/`db` don't need every prod env var set) and raises listing every missing required setting at once.

**Local vs. hosted refresh — same refresh code, two different triggers.** Locally, `launchd` runs `refresh_watchlist.py` / `calaccess-pipeline/refresh_calaccess.py` directly on a schedule, each opening the DB file itself — works because everything's one process family on one Mac. Hosted on Render, cron job services can't attach a persistent disk, so `render.yaml` defines two thin cron services that just `curl` an internal, secret-gated endpoint (`POST /internal/refresh-watchlist` / `/internal/refresh-calaccess`) on the one always-on web service that *does* hold the disk; that endpoint runs the same refresh code in a background thread. `REFRESH_SECRET` unset (the local case) means those routes 404 and don't exist at all.

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

**This repo's working directory may be shared by more than one concurrent Claude Code session** (observed directly: a `git checkout`/`git reset --hard` from another session mid-task silently discarded this session's uncommitted edits, and separately, two sessions' uncommitted edits to the same file ended up swept into the same commit). Before trusting that your edits are still on disk, re-check with `git status`/`git diff` rather than assuming — and commit + push promptly once a change is verified working, rather than leaving substantial uncommitted work sitting in the working tree.
