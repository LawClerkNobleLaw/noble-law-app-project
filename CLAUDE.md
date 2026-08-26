# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Rotunda (a.k.a. "BillWatch") — a small web app for a CA lobbying law firm to track LegiScan bill activity, flag bills per-client with a position (support/oppose/watch), get a daily change digest by email, cross-reference CAL-ACCESS lobbying disclosures, and prepare (not file) FPPC disclosure forms. Two top-level projects share one SQLite database:

- `legiscan-lookup/` — the actual web app (LegiScan tracking, accounts, clients, disclosures)
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

**Not Flask, not any framework.** `app.py`'s `Handler` class subclasses `http.server.BaseHTTPRequestHandler` directly, served by a `ThreadingHTTPServer`. Routes are manual `if parsed.path == "/foo":` checks inside `_do_GET`/`_do_POST`/`_do_DELETE` — there's no `@app.route`, no blueprints, no middleware chain. When adding a route, find the right method's dispatch block and add another `if` (existing ones are grouped by concern, not alphabetically). This is a single ~6000-line file; use grep for `parsed.path ==`, a page-body constant name (`REPORT_BODY`, `FLAGGED_BODY`, etc.), or a function name rather than trying to read it linearly.

**Pages are big Python f-strings, not templates.** Each page's HTML/CSS/JS lives inline as a module-level constant (e.g. `REPORT_BODY`, `LOOKUP_BODY`, `CLIENTS_BODY`), built by shared helpers (`page()` wraps `<html>`/`<head>`, `app_shell()` adds the sidebar+topbar chrome, `account_widget()` is the one avatar/dropdown component used everywhere). Page-specific interactivity is inline `<script>` in the same f-string, fetching the page's own `/api/...` JSON endpoint client-side rather than server-rendering data into the HTML. `STYLE` is one shared CSS block (light/dark via `data-theme` + `prefers-color-scheme`, not a second stylesheet).

**Every module below `app.py` has one job, and `app.py` is the only thing that imports the web-facing pieces together:**
- `db.py` — all SQLite access for the app's own tables (users, clients, bills, flagged_bills, bill_client_links, prepared_filings, ...). `db/schema.sql` is the one canonical schema, every `CREATE TABLE` using `IF NOT EXISTS`, applied fresh on every boot (`init_db()`). Since `CREATE TABLE IF NOT EXISTS` can't add a column to an already-existing table, any new column goes in **two** places: the `CREATE TABLE` in `schema.sql` (for brand-new DBs) *and* a guarded `ALTER TABLE ... ADD COLUMN` in `db.py`'s `_migrate()` (for existing ones) — follow the existing pattern there (check `PRAGMA table_info`, only alter if the column's missing) rather than a numbered migrations folder.
- `legiscan_client.py` — the only module that talks to LegiScan's API. Two distinct search modes matter here: `getSearch(bill=...)` is a precise, cheap, number-shaped match (and — checked live — already returns every bill type/chamber sharing a bare number, e.g. searching `72` returns AB72/SB72/ACR72/etc. at once); `getSearch(query=...)` is free-text relevance search and is comparatively noisy for a number-shaped query. Both return lightweight list rows; full detail (`getBill`) is a separate, more expensive call, only made per-bill, never per search result row.
- `accounts.py` — password hashing/verification, sessions, login-lockout.
- `mailer.py` / `digest.py` — the daily "what changed" email. `digest.py` only emails a user when a diff (`db.snapshot_bill_state`/`db.diff_bill_state`) actually found a change on one of *their* flagged bills — no digest, no email. `mailer.py` degrades to logging instead of sending when SMTP env vars aren't set, rather than failing.
- `pdf_forms.py` — fills real FPPC PDF form fields (starts with Form 601) via `pypdf`. The app **never files** anything; it only prepares a document for the user to review and file themselves (`db.sign_off_prepared_filing` is an explicit human sign-off gate, not a submission).
- `config.py` — every environment variable the app reads, in one place. `validate()` is called once at real startup (not at import time, so tests importing `app`/`db` don't need every prod env var set) and raises listing every missing required setting at once.

**Local vs. hosted refresh — same refresh code, two different triggers.** Locally, `launchd` runs `refresh_watchlist.py` / `calaccess-pipeline/refresh_calaccess.py` directly on a schedule, each opening the DB file itself — works because everything's one process family on one Mac. Hosted on Render, cron job services can't attach a persistent disk, so `render.yaml` defines two thin cron services that just `curl` an internal, secret-gated endpoint (`POST /internal/refresh-watchlist` / `/internal/refresh-calaccess`) on the one always-on web service that *does* hold the disk; that endpoint runs the same refresh code in a background thread. `REFRESH_SECRET` unset (the local case) means those routes 404 and don't exist at all.

## Working in this repo

Every change ships as its own branch + PR into `main` (see recent PR history) — branch off `origin/main`, not off whatever another branch happens to be checked out, and open a PR rather than committing straight to `main`.

**This repo's working directory may be shared by more than one concurrent Claude Code session** (observed directly: a `git checkout`/`git reset --hard` from another session mid-task silently discarded this session's uncommitted edits, and separately, two sessions' uncommitted edits to the same file ended up swept into the same commit). Before trusting that your edits are still on disk, re-check with `git status`/`git diff` rather than assuming — and commit + push promptly once a change is verified working, rather than leaving substantial uncommitted work sitting in the working tree.
