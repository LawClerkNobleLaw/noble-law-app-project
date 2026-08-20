#!/usr/bin/env python3
"""
Rotunda (formerly "Bill Search") — a small local web app.

Runs entirely on your machine (not hosted anywhere) so it has normal
internet access and can call the LegiScan API live, on demand, when you
search. Start it with `python3 app.py` (or `./start.sh`), then open
http://localhost:8420 in your browser.

Capabilities live here:

  - Live lookup (the original feature): search a California bill by
    number, call LegiScan on the spot, show the result. Nothing is
    stored by the search itself.
  - Flagged bills: sign in (see Individual accounts below) and click
    "Flag this bill" to add it to YOUR OWN personal list, at /flagged.
    This replaced an earlier "stored watch list" that was one single
    list shared by everyone with no owner — that made sense before
    individual accounts existed, but once they did, having two
    almost-identical buttons ("Add to watch list" vs "Flag this bill")
    was more confusing than useful, and the ownerless shared list had a
    real problem: anyone could silently remove a bill someone else was
    tracking. Flagging still reuses the exact same underlying mechanism
    (a bill gets upserted into `bills` and added to the shared
    `watchlist` table so the daily job keeps it fresh) — `flagged_bills`
    just adds the "which user cares about this one" layer the old
    single shared list had no room for. /watchlist now redirects
    (to /flagged if signed in, /login otherwise) rather than 404ing on
    anyone with the old page bookmarked.
  - Lobbying search (/lobbying): search the CAL-ACCESS firms/employers
    (lobbying_entities) and quarterly disclosures (lobbying_disclosures)
    that calaccess-pipeline/refresh_calaccess.py loads. Since roughly a
    third of clients named in a disclosure have no independent
    registration of their own (see that file's docstring), search
    matches both the registered-entity name AND the free-text
    client_name on disclosures, and a result's detail view shows BOTH
    directions: what this entity filed (if it's a firm/employer that
    files) and where this name was mentioned as someone else's client.
  - Individual accounts: /signup (email + password, then a CAL-ACCESS
    Form 601-style profile step), /login, /profile (view and edit), and
    the account-menu dropdown on every page (see ACCOUNT_MENU_SCRIPT) —
    real password hashing lives in accounts.py. There used to also be
    an outer shared LOOKUP_USER/PASSWORD login gating the whole site
    (a coworker-wide Basic Auth prompt just to view anything); that's
    gone now that individual accounts exist and actually work — the
    site itself (lookup, lobbying search, signup/login) is open to
    visit, and signing in is only required for the personal features
    (flagged bills, clients, action reports, profile).
  - Internal refresh triggers (added for hosted deployment — see
    render.yaml): when this app runs locally, the two daily refreshes
    (LegiScan watch-list, CAL-ACCESS ingestion) are separate scripts
    launchd runs on a schedule, each opening the database directly —
    that only works because they're all just processes on the same Mac
    sharing one local file. Hosted on Render, a Cron Job service can't
    attach a persistent disk at all, so the cron jobs are just thin
    triggers: POST /internal/refresh-watchlist and
    /internal/refresh-calaccess, gated on a shared secret, run the exact
    same refresh code in a background thread of THIS process — the one
    process that actually holds the disk. Locally, with no REFRESH_SECRET
    set, these routes don't exist at all (404) and launchd keeps working
    exactly as before — this is purely additive.

The actual "talk to LegiScan" logic lives in legiscan_client.py, and the
database logic lives in db.py — both files are shared with
refresh_watchlist.py so nothing here is duplicated between the live app
and the daily job. refresh_calaccess.py (and its own calaccess_db.py) live
in the sibling calaccess-pipeline/ folder and are imported the same way,
via a relative path — not copied in here.

The API key is read from the LEGISCAN_API_KEY environment variable. If
that's not set (e.g. you're running this from a non-login shell), it falls
back to reading the `export LEGISCAN_API_KEY=...` line out of ~/.zshrc.
"""

import datetime
import hmac
import json
import os
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import accounts
import db
import mailer
import pdf_forms
import refresh_watchlist
from legiscan_client import get_api_key, lookup_bill, get_bill_detail

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calaccess-pipeline"))
import refresh_calaccess  # noqa: E402 — must follow the sys.path insert above

PORT = int(os.environ.get("PORT", 8420))

# Gates the two /internal/refresh-* routes. Unset locally on purpose —
# see the module docstring above.
REFRESH_SECRET = os.environ.get("REFRESH_SECRET")

# Guards against a cron firing twice before the first run finishes —
# maps job name -> bool. Not persisted; a restart just clears it, which is
# fine, since the worst case is one extra run, not a corrupted one (every
# refresh is upsert-based already).
_refresh_running = {"watchlist": False, "calaccess": False}

# What /internal/status reports back for "did the last refresh actually
# work" — filled in by _trigger_refresh's run() below. Not persisted,
# same tradeoff as _refresh_running: a restart just means this is empty
# until the next refresh runs, not a corrupted answer.
_last_refresh = {"watchlist": None, "calaccess": None}
_refresh_lock = threading.Lock()

# Basic brute-force guard for /api/login — maps lowercased email to
# (failure_count, first_failure_time). Not persisted and not shared
# across processes, same tradeoff as _refresh_running above: a restart
# clears it, which just means a fresh five attempts, not a security hole.
# Keyed on email rather than IP since a home/office NAT can put many real
# users behind one IP, and this app has no other per-request identity to
# key on before login succeeds.
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_WINDOW = datetime.timedelta(minutes=15)
_login_failures = {}
_login_failures_lock = threading.Lock()


def _login_locked_out(email):
    with _login_failures_lock:
        entry = _login_failures.get(email)
        if not entry:
            return False
        count, first_failure = entry
        if datetime.datetime.now() - first_failure > LOGIN_LOCKOUT_WINDOW:
            del _login_failures[email]
            return False
        return count >= MAX_LOGIN_ATTEMPTS


def _record_login_failure(email):
    with _login_failures_lock:
        count, first_failure = _login_failures.get(email, (0, datetime.datetime.now()))
        if datetime.datetime.now() - first_failure > LOGIN_LOCKOUT_WINDOW:
            count, first_failure = 0, datetime.datetime.now()
        _login_failures[email] = (count + 1, first_failure)


def _clear_login_failures(email):
    with _login_failures_lock:
        _login_failures.pop(email, None)


STYLE = """
  :root {
    /* Monochrome — replaces the earlier indigo/Linear palette. No brand
       accent hue anywhere; --accent is now just an alias for --slate
       (kept as its own variable since several rules below reference it
       by name for what used to be a colored "watch/neutral" role — a
       plain "In committee" style tag reads better as neutral gray than
       as a leftover colored one). --accent-solid/--accent-solid-text
       are the button-fill pair, kept separate from --accent because in
       dark mode the fill inverts (light pill, dark text) while --accent
       stays a light-on-dark text color — one variable can't do both.
       --content-bg is only used by the sidebar-shell pages (app_shell())
       for the slightly-off-white area behind the shell's cards. */
    --ink: #171717; --paper: #ffffff; --content-bg: #fafafa; --surface: #ffffff;
    --slate: #6b6b6b; --rule: #e5e5e5; --accent: var(--slate);
    --accent-solid: #171717; --accent-solid-text: #ffffff;
    --accent-soft: #f5f5f5; --good: #15803d; --good-soft: #dcfce7;
    --error: #b91c1c; --error-soft: #fee2e2;
    --shadow-rest: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.03);
    --shadow-hover: 0 4px 12px rgba(0,0,0,0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink: #f5f5f5; --paper: #0a0a0a; --content-bg: #171717; --surface: #0a0a0a;
      --slate: #a3a3a3; --rule: #262626; --accent: var(--slate);
      --accent-solid: #f5f5f5; --accent-solid-text: #171717;
      --accent-soft: #262626; --good: #4ade80; --good-soft: #0e2817;
      --error: #f87171; --error-soft: #2f1313;
      --shadow-rest: 0 1px 3px rgba(0,0,0,0.4), 0 1px 2px rgba(0,0,0,0.3);
      --shadow-hover: 0 4px 14px rgba(0,0,0,0.5);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--content-bg); color: var(--ink);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  /* A handful of plain, unclassed <a> tags (e.g. a client's name in the
     flagged-bills list) had no color rule anywhere, so they fell back to
     the browser's own default link blue instead of the app's palette —
     this is the one place that acts as a fallback for those. Anywhere
     that already sets its own color (.secondary, .primary, .panel-link,
     etc.) keeps doing so — a class selector beats this bare-tag one. */
  a { color: var(--accent); }
  a:focus-visible, button:focus-visible, input:focus-visible,
  select:focus-visible, summary:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 6px;
  }
  .wrap { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.5rem 4rem; }
  /* A full-width bar, same grammar as the signed-in shell's .app-topbar
     (fixed height, border-bottom, solid --paper against the page's
     --content-bg) — public pages don't get the sidebar, but the top
     chrome should still read as "this app's header," not a different
     product's thin link row. The inner div re-centers content at the
     same width as .wrap so the bar's contents still line up with the
     page below it. */
  .top-nav {
    height: 4rem; display: flex; align-items: center; background: var(--paper);
    border-bottom: 1px solid var(--rule);
  }
  .top-nav-inner {
    width: 100%; max-width: 46rem; margin: 0 auto; padding: 0 1.5rem;
    display: flex; gap: 1.1rem; align-items: center;
  }
  .top-nav a { color: var(--accent); font-size: 0.85rem; text-decoration: none; }
  .top-nav a:hover { text-decoration: underline; }
  /* .top-brand is a real <a> now (links home) — .top-nav a above would
     otherwise win on color/font-size since element+class ties with
     class-only on specificity's middle term; .top-nav .top-brand (two
     classes) reliably beats it instead of relying on source order. */
  .top-nav .top-brand {
    display: flex; align-items: center; gap: 0.5rem; font-weight: 600; font-size: 0.9rem;
    color: var(--ink); margin-right: 0.4rem;
  }
  .top-nav .top-brand:hover { text-decoration: none; }
  .top-brand svg { color: var(--ink); }
  h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
  .sub { color: var(--slate); margin: 0 0 2rem; font-size: 0.92rem; }
  form {
    display: flex; gap: 0.6rem; margin-bottom: 2rem; flex-wrap: wrap;
  }
  input, select, textarea {
    font: inherit; padding: 0.6rem 0.75rem; border: 1px solid var(--rule);
    border-radius: 8px; background: var(--surface); color: var(--ink);
  }
  select { cursor: pointer; }
  input#bill { flex: 1; min-width: 8rem; }
  button {
    font: inherit; font-weight: 600; padding: 0.6rem 1.1rem; border: none;
    border-radius: 8px; background: var(--accent-solid); color: var(--accent-solid-text); cursor: pointer;
  }
  button:hover { opacity: 0.9; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.secondary { background: var(--accent-soft); color: var(--accent); }
  button.danger { background: var(--error-soft); color: var(--error); }
  /* Same button look for plain <a> links used as actions (e.g. "View"
     on LegiScan, "Report" to the action-report page) — button's base
     rule only targets <button>, so links need their own copy of the
     same properties plus the anchor-specific reset. */
  a.secondary, a.danger {
    display: inline-block; font: inherit; font-weight: 600;
    padding: 0.6rem 1.1rem; border-radius: 8px; text-decoration: none;
  }
  a.secondary { background: var(--accent-soft); color: var(--accent); }
  a.danger { background: var(--error-soft); color: var(--error); }
  /* Solid-fill counterpart to a.secondary/a.danger above, same reasoning
     — an <a> styled to look like the app's solid dark <button>. */
  a.primary {
    display: inline-flex; align-items: center; gap: 0.4rem; font: inherit; font-weight: 600;
    padding: 0.5rem 0.9rem; border-radius: 8px; text-decoration: none;
    background: var(--accent-solid); color: var(--accent-solid-text);
  }
  a.primary:hover { opacity: 0.9; }
  a.primary svg { width: 0.85rem; height: 0.85rem; }
  a.secondary:hover, a.danger:hover { opacity: 0.9; }
  #result { display: none; }
  #result.show { display: block; }
  .card {
    background: var(--surface); border: 1px solid var(--rule); box-shadow: var(--shadow-rest);
    border-radius: 18px; padding: 1.25rem 1.4rem; margin-bottom: 1rem;
  }
  .bill-id { font-family: ui-monospace, monospace; font-size: 0.8rem; color: var(--accent); margin-bottom: 0.4rem; }
  .bill-title { font-size: 1.15rem; font-weight: 700; margin: 0 0 0.3rem; }
  .bill-desc { color: var(--slate); font-size: 0.9rem; }
  .bill-link { display: inline-block; margin-top: 0.6rem; font-size: 0.85rem; }
  /* Just a neutral dot, not color-varied by status text — LegiScan's
     status_label is a freeform string (dozens of possible values across
     bills), and guessing which ones are "good" vs "bad" well enough to
     color them confidently isn't a call this restyle should make. */
  .status-badge {
    display: inline-flex; align-items: center; gap: 0.35rem; background: var(--accent-soft); color: var(--accent);
    font-size: 0.75rem; font-weight: 700; padding: 0.2rem 0.6rem; border-radius: 999px;
    margin-bottom: 0.5rem;
  }
  .status-badge::before { content: ""; width: 0.4rem; height: 0.4rem; border-radius: 999px; background: currentColor; flex: none; }
  .card-actions { margin-top: 0.9rem; display: flex; gap: 0.5rem; }
  h2.section { font-size: 0.95rem; margin: 1.6rem 0 0.6rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.87rem; }
  td, th { padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--rule); vertical-align: top; text-align: left; }
  td.date { font-family: ui-monospace, monospace; white-space: nowrap; color: var(--slate); }
  td.chamber {
    white-space: nowrap; font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
    color: var(--accent);
  }
  .sponsor-list { display: flex; flex-wrap: wrap; gap: 0.4rem; }
  .sponsor {
    background: var(--accent-soft); color: var(--accent); font-size: 0.8rem;
    padding: 0.25rem 0.6rem; border-radius: 999px;
  }
  #error {
    display: none; background: var(--error-soft); color: var(--error);
    padding: 0.8rem 1rem; border-radius: 8px; font-size: 0.88rem; margin-bottom: 1.5rem;
  }
  #error.show { display: block; }
  #loading { display: none; color: var(--slate); font-size: 0.9rem; }
  #loading.show { display: block; }
  .empty { color: var(--slate); font-size: 0.9rem; }
  tr.row-link { cursor: pointer; }
  tr.row-link:hover { background: var(--accent-soft); }
  .tag { display: inline-block; font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
    color: var(--slate); background: var(--accent-soft); padding: 0.1rem 0.5rem; border-radius: 999px; }
  /* A client's stance on a bill — same three values everywhere (the
     /flagged position selector and the /report page's read-only badge),
     colored consistently: support=good, oppose=error, watch=neutral. */
  .position-badge { display: inline-block; font-size: 0.75rem; font-weight: 700;
    padding: 0.2rem 0.6rem; border-radius: 999px; }
  .position-badge.support { background: var(--good-soft); color: var(--good); }
  .position-badge.oppose { background: var(--error-soft); color: var(--error); }
  .position-badge.watch { background: var(--accent-soft); color: var(--accent); }
  select.position-select.support { border-color: var(--good); color: var(--good); }
  select.position-select.oppose { border-color: var(--error); color: var(--error); }
  select.position-select.watch { border-color: var(--accent); color: var(--accent); }
  /* Filter tabs above a flagged/client-position list — same All/Support/
     Oppose/Watch vocabulary as the position badges above, just as a
     filter instead of a per-row value. */
  .filter-tabs { display: flex; gap: 0.4rem; margin-bottom: 1.1rem; }
  .filter-tab {
    font: inherit; font-size: 0.82rem; font-weight: 600; color: var(--slate);
    background: var(--surface); border: 1px solid var(--rule); border-radius: 999px;
    padding: 0.35rem 0.75rem; cursor: pointer;
  }
  .filter-tab:hover { border-color: var(--ink); }
  .filter-tab.active {
    background: var(--accent-solid); color: var(--accent-solid-text); border-color: var(--accent-solid);
  }
  .filter-tab .n { font-family: ui-monospace, monospace; font-size: 0.75rem; opacity: 0.7; margin-left: 0.35rem; }
  .account-menu { position: relative; font-size: 0.85rem; }
  .account-menu summary { cursor: pointer; color: var(--accent); list-style: none; }
  .account-menu summary::-webkit-details-marker { display: none; }
  .account-menu-content {
    position: absolute; right: 0; top: 1.5rem; background: var(--surface);
    border: 1px solid var(--rule); border-radius: 8px; padding: 0.5rem 0.7rem;
    display: flex; flex-direction: column; gap: 0.5rem; min-width: 9.5rem;
    box-shadow: 0 4px 14px rgba(45, 43, 43, 0.16); z-index: 20;
  }
  .account-menu-content a, .account-menu-content button {
    color: var(--accent); font-size: 0.85rem; background: none; border: none;
    padding: 0; text-align: left; font-weight: 400; cursor: pointer; text-decoration: none;
  }
  .account-menu-content a:hover, .account-menu-content button:hover { text-decoration: underline; }
  .account-menu-email { font-size: 0.75rem; color: var(--slate); border-bottom: 1px solid var(--rule);
    padding-bottom: 0.4rem; margin-bottom: 0.1rem; word-break: break-all; }

  /* ── Sidebar app shell (app_shell()) ──────────────────────────────
     Only for signed-in pages, rolled out one page at a time — see
     app_shell()'s own docstring. Public pages keep the plain .wrap
     single-column layout above and just inherit the new tokens. */
  .app-shell { display: flex; min-height: 100vh; }
  .app-sidebar {
    width: 14rem; flex: none; background: var(--paper); border-right: 1px solid var(--rule);
    display: flex; flex-direction: column; height: 100vh; position: sticky; top: 0;
  }
  .app-brand {
    display: flex; align-items: center; gap: 0.5rem; height: 4rem; padding: 0 1.25rem;
    border-bottom: 1px solid var(--rule); font-weight: 600; font-size: 0.9rem; flex: none;
  }
  /* No square badge — just the mark itself, colored like the rest of
     the app's text (--ink), sitting directly on --paper. Light mode:
     dark mark on white. Dark mode: white mark on black. */
  .app-brand-mark { display: flex; align-items: center; flex: none; color: var(--ink); }
  nav.app-nav { padding: 1rem 0.75rem; flex: 1; overflow-y: auto; }
  .app-nav ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.15rem; }
  .side-nav-item {
    height: 2rem; display: flex; align-items: center; gap: 0.6rem; border-radius: 8px; padding: 0 0.75rem;
    font-size: 0.82rem; font-weight: 500; color: var(--slate); text-decoration: none;
    transition: background .12s ease, color .12s ease;
  }
  .side-nav-item svg { width: 0.9rem; height: 0.9rem; flex: none; }
  .side-nav-item:hover { background: var(--accent-soft); color: var(--ink); }
  .side-nav-item.active { background: var(--accent-soft); color: var(--ink); }
  .side-nav-label {
    font-size: 0.68rem; font-weight: 600; color: var(--slate); letter-spacing: .05em;
    text-transform: uppercase; margin: 1.25rem 0.75rem 0.5rem;
  }
  .app-sidebar-foot { padding: 0.75rem; border-top: 1px solid var(--rule); flex: none; position: relative; }
  .app-account {
    display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 0.5rem; border-radius: 8px;
    cursor: pointer; transition: background .12s ease; background: none; border: none; width: 100%;
    text-align: left; color: var(--ink); /* button's own base rule sets color: var(--accent-solid-text) —
    white-on-white in light mode, dark-on-dark in dark — this overrides it back to the real text color. */
  }
  .app-account:hover { background: var(--accent-soft); }
  .app-avatar {
    height: 1.75rem; width: 1.75rem; border-radius: 999px; background: var(--ink); color: var(--paper);
    font-size: 0.7rem; font-weight: 600; display: flex; align-items: center; justify-content: center; flex: none;
  }
  .app-account-email {
    font-size: 0.8rem; font-weight: 500; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; flex: 1; min-width: 0;
  }
  .app-account-menu {
    position: absolute; left: 0.75rem; right: 0.75rem; bottom: 3.75rem; background: var(--surface);
    border: 1px solid var(--rule); border-radius: 8px; padding: 0.4rem; box-shadow: 0 4px 14px rgba(0,0,0,0.16);
    display: none; flex-direction: column; gap: 0.15rem; z-index: 20;
  }
  .app-account-menu.show { display: flex; }
  .app-account-menu button {
    background: none; border: none; color: var(--ink); font: inherit; font-weight: 500; font-size: 0.82rem;
    text-align: left; padding: 0.4rem 0.5rem; border-radius: 6px; cursor: pointer;
  }
  .app-account-menu button:hover { background: var(--accent-soft); }
  .app-body { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .app-topbar {
    height: 4rem; flex: none; display: flex; align-items: center; justify-content: space-between;
    padding: 0 1.5rem; border-bottom: 1px solid var(--rule); background: var(--paper); gap: 1rem;
  }
  .app-topbar-title { font-size: 0.95rem; font-weight: 600; }
  .app-topbar-sub { font-size: 0.78rem; color: var(--slate); margin-top: 0.1rem; }
  .app-topbar-actions { display: flex; align-items: center; gap: 0.5rem; flex: none; }
  /* Lives in the topbar (shared chrome), but what it actually filters is
     page-specific — each shell page's own script attaches its own
     'input' listener to #shell-search if that page has something to
     filter. A page that doesn't just leaves it unwired. */
  .search-box {
    display: flex; align-items: center; gap: 0.5rem; height: 2rem; width: 12.5rem; border-radius: 8px;
    background: var(--accent-soft); border: 1px solid var(--rule); padding: 0 0.6rem; font-size: 0.8rem; color: var(--slate);
  }
  .search-box svg { width: 0.75rem; height: 0.75rem; flex: none; }
  .search-box input {
    background: none; border: none; outline: none; color: var(--ink); font-size: 0.8rem;
    width: 100%; font-family: inherit;
  }
  .search-box input::placeholder { color: var(--slate); }
  .icon-btn {
    height: 2rem; width: 2rem; border-radius: 8px; border: 1px solid var(--rule); background: var(--paper);
    display: flex; align-items: center; justify-content: center; cursor: pointer; transition: background .12s ease;
  }
  .icon-btn:hover { background: var(--accent-soft); }
  .icon-btn svg { width: 0.85rem; height: 0.85rem; color: var(--slate); }
  @media (max-width: 700px) { .search-box { display: none; } }
  .app-main { flex: 1; overflow-y: auto; padding: 1.5rem; background: var(--content-bg); }
  /* The page's own big heading + description + right-aligned controls
     (e.g. filter tabs), living inside .app-main — distinct from the
     small, generic "Overview" label in .app-topbar above it. */
  .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 1rem; margin-bottom: 1.25rem; flex-wrap: wrap; }
  .page-head h1 { font-size: 1.3rem; font-weight: 600; letter-spacing: -0.01em; margin: 0; }
  .page-head .sub { font-size: 0.82rem; color: var(--slate); margin: 0.15rem 0 0; }
  .page-head .filter-tabs { margin-bottom: 0; }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 0.85rem; margin-bottom: 1rem; }
  .stat-card {
    background: var(--surface); border: 1px solid var(--rule); box-shadow: var(--shadow-rest);
    border-radius: 18px; padding: 1.1rem; transition: box-shadow .15s ease;
  }
  .stat-card:hover { box-shadow: var(--shadow-hover); }
  .stat-icon {
    height: 2.1rem; width: 2.1rem; border-radius: 8px; background: var(--accent-soft);
    display: flex; align-items: center; justify-content: center; color: var(--slate); margin-bottom: 0.85rem;
  }
  .stat-icon svg { width: 1rem; height: 1rem; }
  .stat-label { font-size: 0.78rem; font-weight: 500; color: var(--slate); margin-bottom: 0.25rem; }
  .stat-value { font-size: 1.7rem; font-weight: 600; letter-spacing: -0.01em; line-height: 1.1; }
  .stat-foot { font-size: 0.72rem; color: var(--slate); margin-top: 0.5rem; }
  /* A card meant to hold a whole list/table, with its own header row —
     same visual family as .card, just with a title/subtitle slot. */
  .panel {
    background: var(--surface); border: 1px solid var(--rule); box-shadow: var(--shadow-rest);
    border-radius: 18px; overflow: hidden;
  }
  .panel-head {
    display: flex; align-items: center; justify-content: space-between; gap: 1rem;
    padding: 1rem 1.15rem; border-bottom: 1px solid var(--rule);
  }
  .panel-head .title { font-size: 0.95rem; font-weight: 600; letter-spacing: -0.005em; }
  .panel-head .sub { font-size: 0.75rem; color: var(--slate); margin-top: 0.15rem; }
  .panel-link {
    height: 1.75rem; display: flex; align-items: center; gap: 0.35rem; border-radius: 8px;
    border: 1px solid var(--rule); background: var(--accent-soft); color: var(--slate);
    font: inherit; font-size: 0.72rem; font-weight: 500; padding: 0 0.6rem; cursor: pointer; flex: none;
  }
  .panel-link:hover:not(:disabled) { background: var(--content-bg); }
  .panel-link:disabled { cursor: not-allowed; opacity: 0.6; }
  .panel table { margin: 0; }
  .panel th { padding: 0.55rem 1.15rem; }
  .panel td { padding: 0.75rem 1.15rem; }
  .panel tbody tr:hover { background: var(--content-bg); }
  /* A bill row's icon+title+id block — same .bill-title/.bill-id classes
     the /report page's big card already uses, just sized down here via
     the .bill-row scope rather than given new parallel class names. */
  .bill-row { display: flex; align-items: center; gap: 0.6rem; }
  .bill-row .bill-icon {
    height: 1.75rem; width: 1.75rem; border-radius: 8px; background: var(--accent-soft);
    display: flex; align-items: center; justify-content: center; flex: none;
  }
  .bill-row .bill-icon svg { width: 0.75rem; height: 0.75rem; color: var(--slate); }
  .bill-row .bill-title { font-size: 0.85rem; font-weight: 500; margin: 0; }
  .bill-row .bill-id { font-size: 0.72rem; margin: 0.1rem 0 0; }
  .row-actions { display: flex; gap: 0.4rem; flex-wrap: wrap; margin-top: 0.35rem; }
  .row-actions a { font-size: 0.78rem; padding: 0.3rem 0.6rem; }
  /* A "..." overflow menu for row actions that are real but shouldn't
     shout — a bright red button on every row was too loud for
     something you do occasionally, not the primary action of the row. */
  .row-menu { position: relative; text-align: right; }
  .row-menu-btn {
    height: 1.75rem; width: 1.75rem; border-radius: 8px; border: 1px solid var(--rule); background: var(--surface);
    color: var(--slate); display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer; transition: background .12s ease, color .12s ease, border-color .12s ease;
  }
  .row-menu-btn:hover { background: var(--accent-soft); color: var(--ink); border-color: var(--ink); }
  .row-menu-btn svg { width: 1rem; height: 1rem; }
  .row-menu-dropdown {
    position: absolute; right: 0; top: calc(100% + 0.25rem); background: var(--surface);
    border: 1px solid var(--rule); border-radius: 8px; padding: 0.3rem; min-width: 9rem;
    box-shadow: 0 4px 14px rgba(0,0,0,0.16); z-index: 20; display: none; flex-direction: column; gap: 0.1rem;
  }
  .row-menu-dropdown.show { display: flex; }
  .row-menu-dropdown button {
    background: none; border: none; color: var(--error); font: inherit; font-weight: 500; font-size: 0.82rem;
    text-align: left; padding: 0.4rem 0.6rem; border-radius: 6px; cursor: pointer;
  }
  .row-menu-dropdown button:hover { background: var(--error-soft); }
  th {
    background: var(--content-bg); font-size: 0.68rem; font-weight: 600; color: var(--slate);
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  @media (max-width: 900px) {
    .app-sidebar { display: none; }
    .stat-grid { grid-template-columns: 1fr 1fr; }
  }
"""


def nav_links(current):
    """Links to whichever OTHER content pages exist — computed once per
    page constant below (these are built at import time, not
    per-request, so this only ever runs a handful of times total).
    Flagged bills isn't listed here on purpose — it's personal and tied
    to login, so it lives in the account menu next to "View profile"
    rather than in this always-visible row (see ACCOUNT_MENU_SCRIPT)."""
    pages = [("/lookup", "Lookup"), ("/lobbying", "Organization Search")]
    parts = []
    for href, label in pages:
        if href == current:
            continue
        parts.append(f'<a href="{href}">{"← " if href == "/lookup" else ""}{label}{"" if href == "/lookup" else " →"}</a>')
    return "".join(parts)


# The account menu's login state is per-request (whoever's browser this
# is), but every page constant below is a plain string built ONCE at
# import time — so unlike nav_links() above, this can't be baked into
# the static HTML. Instead each page ships an empty <span id="account-menu">
# plus this same small script, which fetches /api/me itself and fills
# the span in client-side — the same "server ships a shell, JS fetches
# JSON and renders" pattern already used everywhere else in this app
# (bill lookup, watch list, lobbying search), just applied to login state.
ACCOUNT_MENU_SLOT = '<span id="account-menu" style="margin-left:auto"></span>'
ACCOUNT_MENU_SCRIPT = """
<script>
(function() {
  const el = document.getElementById('account-menu');
  if (!el) return;
  fetch('/api/me').then(r => r.json()).then(me => {
    if (me.logged_in) {
      el.innerHTML = `
        <details class="account-menu">
          <summary>${me.email} ▾</summary>
          <div class="account-menu-content">
            <div class="account-menu-email">Signed in as ${me.email}</div>
            <a href="/profile">View profile</a>
            <a href="/flagged">My flagged bills</a>
            <a href="/clients">Clients</a>
            <a href="/disclosures">Disclosure forms</a>
            <button type="button" id="sign-out-btn">Sign out</button>
          </div>
        </details>
      `;
      document.getElementById('sign-out-btn').addEventListener('click', async () => {
        await fetch('/api/logout', { method: 'POST' });
        window.location.href = '/';
      });
    } else {
      el.innerHTML = '<a href="/login">Sign in</a> &nbsp;<a href="/signup">Sign up</a>';
    }
  }).catch(() => {});
})();
</script>
"""


TOP_BRAND = """<a href="/" class="top-brand">
  <svg width="18" height="11" viewBox="0 0 180 112" fill="none">
    <path d="M14 100 A76 76 0 0 1 82 24" stroke="currentColor" stroke-width="19"/>
    <path d="M98 24 A76 76 0 0 1 166 100" stroke="currentColor" stroke-width="19"/>
    <rect x="14" y="98.5" width="152" height="13.5" fill="currentColor"/>
  </svg>
  Rotunda
</a>"""


def top_nav(current, left_extra=""):
    """The full top-nav bar: the brand mark, the 3-page links (or a
    custom left_extra, e.g. signup's "Skip for now"), plus the account
    menu pushed to the right via the slot's own margin-left:auto. Meant
    to sit directly in <body>, outside .wrap — it's a full-width bar,
    not part of the centered content column."""
    left = left_extra if left_extra else nav_links(current)
    return (
        f'<div class="top-nav"><div class="top-nav-inner">{TOP_BRAND}{left}{ACCOUNT_MENU_SLOT}'
        f'</div></div>{ACCOUNT_MENU_SCRIPT}'
    )


# ── Sidebar app shell — for signed-in pages only, rolled out one page
# at a time rather than all 13 templates at once. Public pages (lookup,
# login, signup, lobbying search) keep top_nav() + .wrap above; a
# sidebar pointing at pages you can't use yet doesn't make sense before
# you're signed in. /flagged is the first page moved over — /clients,
# /disclosures, /profile, /report, and /clients/detail follow later.
SHELL_NAV_ITEMS = [
    ("/lookup", "Lookup",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<circle cx="6" cy="6" r="4"/><path d="M9.5 9.5L12.5 12.5" stroke-linecap="round"/></svg>'),
    ("/lobbying", "Organization Search",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<path d="M2 13V6l5-4 5 4v7" stroke-linejoin="round"/><path d="M5.5 13V8h3v5"/></svg>'),
    ("/flagged", "Flagged bills",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<path d="M2 1v12M2 2h8l-2 2.5L10 7H2" stroke-linejoin="round"/></svg>'),
    ("/clients", "Clients",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<circle cx="5.5" cy="4.5" r="2.5"/><path d="M1 12c0-2.5 2-4.2 4.5-4.2S10 9.5 10 12" stroke-linecap="round"/></svg>'),
    ("/disclosures", "Disclosures",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<rect x="3" y="1.5" width="8" height="11" rx="1"/>'
     '<path d="M5.2 6l1 1 2.2-2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>'),
]


def app_shell(current, body):
    """Sidebar + topbar chrome for a signed-in page. `body` is that
    page's own already-built inner HTML — its own heading, controls,
    table, script, whatever it needs — just wrapped in the shell. The
    topbar itself stays generic ("Overview" + today's date) rather than
    per-page; the page's actual title/description lives inside `body`
    as a .page-head, paired with that page's own controls (see
    FLAGGED_BODY) — matching the source template's own split between a
    small persistent header and each page's real heading.

    Every page that calls this has already 302'd to /login server-side
    if there's no session, so unlike ACCOUNT_MENU_SCRIPT above, the
    /api/me fetch here isn't an access check — it only learns which
    email to show in the sidebar footer."""
    nav_html = "".join(
        f'<li><a href="{href}" class="side-nav-item{" active" if href == current else ""}">{icon}{label}</a></li>'
        for href, label, icon in SHELL_NAV_ITEMS
    )
    profile_active = " active" if current == "/profile" else ""
    return f"""
<div class="app-shell">
  <aside class="app-sidebar">
    <div class="app-brand">
      <span class="app-brand-mark">
        <!-- Rotunda mark 1a — tentative logo/name. No badge behind it
             now, just the mark in --ink on --paper, so sized per the
             24px specimen (Rotunda Mark.dc.html) rather than the 16px
             one the smaller square badge needed. -->
        <svg width="22" height="14" viewBox="0 0 180 112" fill="none">
          <path d="M14 100 A76 76 0 0 1 82 24" stroke="currentColor" stroke-width="18"/>
          <path d="M98 24 A76 76 0 0 1 166 100" stroke="currentColor" stroke-width="18"/>
          <rect x="14" y="99" width="152" height="13" fill="currentColor"/>
        </svg>
      </span>
      Rotunda
    </div>
    <nav class="app-nav">
      <ul>{nav_html}</ul>
      <div class="side-nav-label">Account</div>
      <ul>
        <li><a href="/profile" class="side-nav-item{profile_active}">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <circle cx="7" cy="4.5" r="2.2"/><path d="M2.5 12c0-2.2 2-4 4.5-4s4.5 1.8 4.5 4" stroke-linecap="round"/>
          </svg>
          Profile
        </a></li>
      </ul>
    </nav>
    <div class="app-sidebar-foot">
      <button type="button" class="app-account" id="shell-account-btn">
        <span class="app-avatar" id="shell-avatar">&nbsp;</span>
        <span class="app-account-email" id="shell-email">&nbsp;</span>
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" style="color:var(--slate);flex:none">
          <path d="M3 4.5L6 7.5L9 4.5" stroke-linecap="round"/>
        </svg>
      </button>
      <div class="app-account-menu" id="shell-account-menu">
        <button type="button" id="shell-signout-btn">Sign out</button>
      </div>
    </div>
  </aside>
  <div class="app-body">
    <header class="app-topbar">
      <div>
        <div class="app-topbar-title">Overview</div>
        <div class="app-topbar-sub" id="shell-date"></div>
      </div>
      <div class="app-topbar-actions">
        <div class="search-box">
          <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="5" r="3.5"/><path d="M8 8l2 2" stroke-linecap="round"/></svg>
          <input id="shell-search" type="text" placeholder="Search bills...">
        </div>
        <button type="button" class="icon-btn" aria-label="Notifications">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M7 1.5A3.5 3.5 0 003.5 5v2L2 9.5h10L10.5 7V5A3.5 3.5 0 007 1.5z"/><path d="M5.5 9.5A1.5 1.5 0 008.5 9.5" stroke-linecap="round"/></svg>
        </button>
        <a href="/lookup" class="primary">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 2v10M2 7h10" stroke-linecap="round"/></svg>
          Flag a bill
        </a>
      </div>
    </header>
    <main class="app-main">{body}</main>
  </div>
</div>
<script>
(function() {{
  const today = new Date();
  document.getElementById('shell-date').textContent = today.toLocaleDateString('en-US', {{
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  }});

  fetch('/api/me').then(r => r.json()).then(me => {{
    if (!me.logged_in) return;
    const email = me.email || '';
    document.getElementById('shell-email').textContent = email;
    document.getElementById('shell-avatar').textContent = email.slice(0, 2).toUpperCase();
  }}).catch(() => {{}});

  const acctBtn = document.getElementById('shell-account-btn');
  const acctMenu = document.getElementById('shell-account-menu');
  acctBtn.addEventListener('click', () => acctMenu.classList.toggle('show'));
  document.addEventListener('click', (e) => {{
    if (!acctBtn.contains(e.target) && !acctMenu.contains(e.target)) acctMenu.classList.remove('show');
  }});
  document.getElementById('shell-signout-btn').addEventListener('click', async () => {{
    await fetch('/api/logout', {{ method: 'POST' }});
    window.location.href = '/';
  }});
}})();
</script>
"""


# The marketing homepage at "/" — everything else in this file is the
# actual product; this is the only page that sells it. Reuses the same
# STYLE (tokens, .card, .panel, .status-badge, .position-badge, buttons,
# top_nav()) as every other page — LANDING_STYLE below adds only what's
# genuinely new here (hero, feature grid, workflow, trust, footer).
# Renders as .mkt-wrap rather than .wrap — the shared .wrap is the
# app's narrow 46rem content column; a marketing page with a feature
# grid needs real width, so it gets its own container instead of
# overloading .wrap's meaning.
LANDING_STYLE = """
  .mkt-wrap { max-width: 72.5rem; margin: 0 auto; padding: 0 2rem; }
  .hero { position: relative; padding: 5.5rem 0 5rem; overflow: hidden; }
  .hero-inner { display: flex; flex-direction: column; align-items: center; text-align: center; }
  .eyebrow {
    display: inline-flex; align-items: center; gap: 0.45rem; font-size: 0.78rem; font-weight: 600;
    background: var(--accent-soft); border: 1px solid var(--rule); padding: 0.4rem 0.75rem;
    border-radius: 20px; margin-bottom: 1.4rem;
  }
  .eyebrow .dot { width: 0.4rem; height: 0.4rem; border-radius: 50%; background: var(--ink); }
  h1.headline { font-size: 3.5rem; line-height: 1.08; font-weight: 700; letter-spacing: -0.025em; max-width: 47rem; }
  .sub-lg { margin-top: 1.4rem; font-size: 1.15rem; line-height: 1.6; color: var(--slate); max-width: 35rem; }
  .hero-ctas { display: flex; gap: 0.75rem; margin-top: 2rem; }
  .hero-note { margin-top: 1rem; font-size: 0.82rem; color: var(--slate); }

  .frame {
    margin-top: 3.5rem; width: 100%; max-width: 55rem; border-radius: 18px; border: 1px solid var(--rule);
    background: var(--surface); box-shadow: 0 30px 60px -20px rgba(0,0,0,0.18); overflow: hidden; text-align: left;
  }
  .frame-body { display: flex; height: 23.75rem; }
  .frame-sidebar {
    width: 11.25rem; flex: none; background: var(--paper); border-right: 1px solid var(--rule);
    display: flex; flex-direction: column; padding: 0.9rem 0.6rem;
  }
  .frame-brand { display: flex; align-items: center; gap: 0.45rem; padding: 0.1rem 0.4rem 0.9rem; font-weight: 600; font-size: 0.78rem; }
  .frame-nav-item { height: 1.75rem; display: flex; align-items: center; gap: 0.5rem; border-radius: 7px; padding: 0 0.5rem; font-size: 0.72rem; font-weight: 500; color: var(--slate); }
  .frame-nav-item svg { width: 0.75rem; height: 0.75rem; flex: none; }
  .frame-nav-item.active { background: var(--accent-soft); color: var(--ink); }
  .frame-main { flex: 1; background: var(--content-bg); padding: 1rem 1.1rem; overflow: hidden; }
  .frame-topbar { margin-bottom: 0.85rem; }
  .frame-title { font-size: 0.875rem; font-weight: 600; }
  .frame-sub { font-size: 0.68rem; color: var(--slate); margin-top: 0.1rem; }
  .frame-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.5rem; margin-bottom: 0.75rem; }
  .frame-stat { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: 0.55rem 0.7rem; box-shadow: var(--shadow-rest); }
  .frame-stat .n { font-family: var(--mono); font-size: 1.05rem; font-weight: 600; }
  .frame-stat .l { font-size: 0.58rem; color: var(--slate); margin-top: 0.05rem; }
  .frame-table { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; overflow: hidden; }
  .frame-row { display: grid; grid-template-columns: 1fr 5.25rem 4.6rem 1.6rem; gap: 0.5rem; align-items: center; padding: 0.5rem 0.7rem; border-bottom: 1px solid var(--rule); font-size: 0.68rem; }
  .frame-row:last-child { border-bottom: none; }
  .frame-row .bill { font-weight: 600; }
  .frame-row .id { font-family: var(--mono); color: var(--slate); font-size: 0.58rem; margin-top: 0.05rem; }
  .frame-row .status-badge, .frame-row .position-badge { font-size: 0.58rem; padding: 0.1rem 0.4rem; }
  .frame-row .status-badge::before { width: 0.25rem; height: 0.25rem; }
  .frame-row .row-menu-btn { height: 1.3rem; width: 1.3rem; }
  .frame-row .row-menu-btn svg { width: 0.6rem; height: 0.6rem; }

  .strip { border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); background: var(--paper); }
  .strip-row { display: grid; grid-template-columns: 1fr 1fr 1fr; padding: 2rem 0; }
  .strip-item { padding: 0 1.75rem; border-left: 1px solid var(--rule); font-size: 0.9rem; color: var(--slate); line-height: 1.55; }
  .strip-item:first-child { border-left: none; padding-left: 0; }
  .strip-item b { color: var(--ink); font-weight: 600; }

  section.mkt-section { padding: 6.25rem 0; }
  .section-head { max-width: 37.5rem; margin-bottom: 3.5rem; }
  .kicker { font-family: var(--mono); font-size: 0.75rem; color: var(--slate); letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 0.875rem; }
  .section-head h2 { font-size: 2rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.2; }
  .section-head p { margin-top: 0.875rem; font-size: 1rem; color: var(--slate); line-height: 1.6; }

  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
  .feat { padding: 1.5rem; transition: box-shadow .2s ease, transform .2s ease; }
  .feat:hover { box-shadow: var(--shadow-hover); transform: translateY(-2px); }
  .feat.wide { grid-column: span 2; }
  .feat-icon {
    width: 2.125rem; height: 2.125rem; border-radius: 9px; background: var(--accent-soft); border: 1px solid var(--rule);
    display: flex; align-items: center; justify-content: center; color: var(--ink); margin-bottom: 1rem;
  }
  .feat h3 { font-size: 1rem; font-weight: 600; letter-spacing: -0.005em; }
  .feat p { margin-top: 0.55rem; font-size: 0.875rem; color: var(--slate); line-height: 1.6; }
  .feat-tag { display: inline-block; margin-top: 0.875rem; font-family: var(--mono); font-size: 0.69rem; color: var(--slate); border: 1px solid var(--rule); border-radius: 5px; padding: 0.2rem 0.45rem; }

  .flow { position: relative; display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
  .flow::before { content: ""; position: absolute; top: 1.45rem; left: calc(16.6% + 0.5rem); right: calc(16.6% + 0.5rem); height: 1px; background: var(--rule); }
  .flow-num {
    width: 2.875rem; height: 2.875rem; border-radius: 50%; background: var(--paper); border: 1px solid var(--rule);
    display: flex; align-items: center; justify-content: center; font-family: var(--mono); font-size: 0.875rem; color: var(--ink);
    margin-bottom: 1.4rem; position: relative; z-index: 1;
  }
  .flow-step h3 { font-size: 1.06rem; font-weight: 600; }
  .flow-step p { margin-top: 0.5rem; font-size: 0.875rem; color: var(--slate); line-height: 1.6; max-width: 18.75rem; }

  .trust { padding: 3rem 3.25rem; display: flex; gap: 2.375rem; align-items: flex-start; }
  .trust-icon {
    width: 3.125rem; height: 3.125rem; flex: none; border-radius: 12px; background: var(--accent-soft); border: 1px solid var(--rule);
    display: flex; align-items: center; justify-content: center; color: var(--ink);
  }
  .trust h2 { font-size: 1.56rem; font-weight: 700; letter-spacing: -0.015em; }
  .trust p { margin-top: 0.75rem; font-size: 0.94rem; color: var(--slate); line-height: 1.7; max-width: 40rem; }
  .trust p + p { margin-top: 0.625rem; }

  .cta-band { border-radius: 20px; text-align: center; padding: 4rem 2.5rem; background: var(--paper); }
  .cta-band h2 { font-size: 2rem; font-weight: 700; letter-spacing: -0.02em; max-width: 32.5rem; margin: 0 auto; }
  .cta-band .hero-ctas { justify-content: center; margin-top: 1.6rem; }
  .cta-band p.foot { margin-top: 1rem; font-size: 0.81rem; color: var(--slate); }

  .mkt-footer { border-top: 1px solid var(--rule); padding: 3.25rem 0 2.25rem; }
  .foot-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 2.5rem; flex-wrap: wrap; }
  .foot-brand { max-width: 16.25rem; }
  .foot-brand .top-brand { margin-bottom: 0.625rem; pointer-events: none; }
  .foot-brand p { font-size: 0.84rem; color: var(--slate); line-height: 1.6; }
  .foot-col h4 { font-size: 0.75rem; color: var(--slate); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.875rem; font-weight: 600; }
  .foot-col a { display: block; font-size: 0.875rem; color: var(--slate); margin-bottom: 0.625rem; transition: color .15s ease; }
  .foot-col a:hover { color: var(--ink); }
  .foot-bottom { margin-top: 3rem; padding-top: 1.375rem; border-top: 1px solid var(--rule); font-size: 0.78rem; color: var(--slate); }

  @media (max-width: 55rem) {
    h1.headline { font-size: 2.375rem; }
    .grid { grid-template-columns: 1fr; }
    .feat.wide { grid-column: span 1; }
    .flow { grid-template-columns: 1fr; }
    .flow::before { display: none; }
    .strip-row { grid-template-columns: 1fr; }
    .strip-item { border-left: none; padding: 0; border-top: 1px solid var(--rule); padding-top: 1.125rem; }
    .strip-item:first-child { border-top: none; padding-top: 0; }
    .strip-item + .strip-item { margin-top: 1.125rem; }
    .trust { flex-direction: column; padding: 1.875rem; }
    .frame-sidebar { display: none; }
    .frame-row { grid-template-columns: 1fr 4.6rem 1.6rem; }
    .frame-row .position-badge { display: none; }
  }
"""

LANDING_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rotunda</title>
<style>{STYLE}{LANDING_STYLE}</style>
</head>
<body>
{top_nav("/", left_extra='<a href="/lookup">Lookup</a><a href="#features">Product</a><a href="#workflow">Workflow</a><a href="#trust">Compliance</a>')}

<header class="hero">
  <div class="mkt-wrap hero-inner">
    <div class="eyebrow"><span class="dot"></span>Built for California lobbying compliance</div>
    <h1 class="headline">The system of record for every bill your clients care about.</h1>
    <p class="sub-lg">Rotunda watches Sacramento so you don't have to. Flag a bill, assign a client and a position, and get one plain-English digest the moment anything actually changes — then let it fill out your FPPC paperwork before the deadline finds you.</p>
    <div class="hero-ctas">
      <a href="/signup" class="btn" style="display:inline-flex;align-items:center;justify-content:center;min-height:2.75rem;padding:0 1.1rem;border-radius:8px;background:var(--accent-solid);color:var(--accent-solid-text);font-weight:600;font-size:0.875rem;">Start tracking bills</a>
      <a href="#features" class="secondary" style="display:inline-flex;align-items:center;justify-content:center;min-height:2.75rem;padding:0 1.1rem;border-radius:8px;">See how it works</a>
    </div>
    <p class="hero-note">Free to <a href="/lookup">look up any bill</a>. No account needed until you flag one.</p>

    <div class="frame">
      <div class="frame-body">
        <div class="frame-sidebar">
          <div class="frame-brand">
            <svg width="14" height="9" viewBox="0 0 180 112" fill="none">
              <path d="M14 100 A76 76 0 0 1 82 24" stroke="currentColor" stroke-width="20"/>
              <path d="M98 24 A76 76 0 0 1 166 100" stroke="currentColor" stroke-width="20"/>
              <rect x="14" y="98" width="152" height="14" fill="currentColor"/>
            </svg>
            Rotunda
          </div>
          <div class="frame-nav-item">
            <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="6" cy="6" r="4"/><path d="M9.5 9.5L12.5 12.5" stroke-linecap="round"/></svg>
            Lookup
          </div>
          <div class="frame-nav-item">
            <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 13V6l5-4 5 4v7" stroke-linejoin="round"/><path d="M5.5 13V8h3v5"/></svg>
            Organization Search
          </div>
          <div class="frame-nav-item active">
            <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 1v12M2 2h8l-2 2.5L10 7H2" stroke-linejoin="round"/></svg>
            Flagged bills
          </div>
          <div class="frame-nav-item">
            <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5.5" cy="4.5" r="2.5"/><path d="M1 12c0-2.5 2-4.2 4.5-4.2S10 9.5 10 12" stroke-linecap="round"/></svg>
            Clients
          </div>
          <div class="frame-nav-item">
            <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="1.5" width="8" height="11" rx="1"/><path d="M5.2 6l1 1 2.2-2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            Disclosures
          </div>
        </div>
        <div class="frame-main">
          <div class="frame-topbar">
            <div class="frame-title">Flagged Bills</div>
            <div class="frame-sub">8 bills across 3 clients</div>
          </div>
          <div class="frame-stats">
            <div class="frame-stat"><div class="n">8</div><div class="l">Flagged bills</div></div>
            <div class="frame-stat"><div class="n">3</div><div class="l">Active clients</div></div>
            <div class="frame-stat"><div class="n">1</div><div class="l">Needs a client</div></div>
          </div>
          <div class="frame-table">
            <div class="frame-row">
              <div><div class="bill">Lobbying Disclosure Modernization Act</div><div class="id">AB 1228</div></div>
              <div class="status-badge">Hearing sched.</div>
              <div><span class="position-badge support">Support</span></div>
              <div class="row-menu-btn"><svg viewBox="0 0 14 14" fill="currentColor"><circle cx="7" cy="3" r="1.4"/><circle cx="7" cy="7" r="1.4"/><circle cx="7" cy="11" r="1.4"/></svg></div>
            </div>
            <div class="frame-row">
              <div><div class="bill">Coastal Development Permit Streamlining</div><div class="id">SB 402</div></div>
              <div class="status-badge">Amended</div>
              <div><span class="position-badge watch">Watch</span></div>
              <div class="row-menu-btn"><svg viewBox="0 0 14 14" fill="currentColor"><circle cx="7" cy="3" r="1.4"/><circle cx="7" cy="7" r="1.4"/><circle cx="7" cy="11" r="1.4"/></svg></div>
            </div>
            <div class="frame-row">
              <div><div class="bill">Groundwater Extraction Fees</div><div class="id">SB 155</div></div>
              <div class="status-badge">Failed</div>
              <div><span class="position-badge oppose">Oppose</span></div>
              <div class="row-menu-btn"><svg viewBox="0 0 14 14" fill="currentColor"><circle cx="7" cy="3" r="1.4"/><circle cx="7" cy="7" r="1.4"/><circle cx="7" cy="11" r="1.4"/></svg></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</header>

<div class="strip">
  <div class="mkt-wrap strip-row">
    <div class="strip-item"><b>Refreshed daily,</b> not on every page load — flagged bills recheck once a day, straight from LegiScan and CAL-ACCESS.</div>
    <div class="strip-item"><b>One digest,</b> not fifty alerts — you hear about a bill only when its status, amendments, or hearings actually change.</div>
    <div class="strip-item"><b>You sign every filing.</b> Rotunda prepares the paperwork; nothing is final until you type your name and confirm it.</div>
  </div>
</div>

<section class="mkt-section" id="features">
  <div class="mkt-wrap">
    <div class="section-head">
      <div class="kicker">Product</div>
      <h2>Everything a lobbying compliance program needs. Nothing it doesn't.</h2>
      <p>Six tools that already run on plain bill numbers and real FPPC forms — not a generic project tracker wearing a legislative skin.</p>
    </div>

    <div class="grid">
      <div class="feat card wide">
        <div class="feat-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M12 3v4M12 17v4M3 12h4M17 12h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="12" cy="12" r="4.5" stroke="currentColor" stroke-width="1.6"/></svg></div>
        <h3>Flagged bills &amp; a daily digest that respects your inbox</h3>
        <p>Flag anything your clients care about. A background job re-checks only those bills once a day and diffs the old state against the new one — status, amendments, hearings, votes. Nothing changed means no email at all.</p>
        <span class="feat-tag">refresh_watchlist.py</span>
      </div>

      <div class="feat card">
        <div class="feat-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="6.5" stroke="currentColor" stroke-width="1.6"/><path d="M20 20l-4.3-4.3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></div>
        <h3>Live bill lookup</h3>
        <p>Search any California bill by number and see its current status straight from LegiScan. No login, nothing saved — just the answer.</p>
      </div>

      <div class="feat card">
        <div class="feat-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"><circle cx="9" cy="8" r="3.4" stroke="currentColor" stroke-width="1.6"/><path d="M3.5 19c0-3.3 2.5-5.6 5.5-5.6s5.5 2.3 5.5 5.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M15.5 6.2c1.6.5 2.7 1.9 2.7 3.6 0 1.5-.9 2.8-2.1 3.4M17.5 13.7c1.9.6 3.2 2.5 3.2 4.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></div>
        <h3>Clients &amp; positions</h3>
        <p>Keep each client's profile, industry, and CAL-ACCESS filer ID. Assign any flagged bill a position — Support, Oppose, or Watch — and change it any time.</p>
      </div>

      <div class="feat card">
        <div class="feat-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M7 3h8l4 4v14H5V3z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M9 12h6M9 16h6M9 8h3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></div>
        <h3>Action reports</h3>
        <p>One page per bill: current status, full history, amendments, upcoming hearings, and your client's position — ready to forward or print.</p>
      </div>

      <div class="feat card">
        <div class="feat-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M4 21V10l8-6 8 6v11" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M9 21v-7h6v7" stroke="currentColor" stroke-width="1.6"/></svg></div>
        <h3>Organization Search</h3>
        <p>Cross-reference California's CAL-ACCESS lobbying disclosure data alongside your bills — the same dataset, refreshed on its own daily pipeline.</p>
      </div>

      <div class="feat card">
        <div class="feat-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M5 4h14v16H5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M9 9l1.6 1.6L14 7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M9 15h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></div>
        <h3>Disclosure form prep</h3>
        <p>Generates a real FPPC Form 601, pre-filled from your profile and clients. You always review the filled PDF before anything is marked ready to file.</p>
      </div>
    </div>
  </div>
</section>

<section class="mkt-section" id="workflow" style="padding-top:0">
  <div class="mkt-wrap">
    <div class="section-head">
      <div class="kicker">Workflow</div>
      <h2>From "someone should watch this bill" to a filed disclosure.</h2>
      <p>The same three steps whether it's one client or forty.</p>
    </div>
    <div class="flow">
      <div class="flow-step">
        <div class="flow-num">01</div>
        <h3>Flag the bills that matter</h3>
        <p>Search, flag, and assign each one to a client with a position — Support, Oppose, or Watch.</p>
      </div>
      <div class="flow-step">
        <div class="flow-num">02</div>
        <h3>Get one digest when it moves</h3>
        <p>A daily job diffs every flagged bill and emails only the people affected, only when something changed.</p>
      </div>
      <div class="flow-step">
        <div class="flow-num">03</div>
        <h3>Generate, review, sign off</h3>
        <p>Pre-fill Form 601 from your clients, review the real PDF, then sign off when it's ready to file yourself.</p>
      </div>
    </div>
  </div>
</section>

<section class="mkt-section" id="trust" style="padding-top:0">
  <div class="mkt-wrap">
    <div class="trust card">
      <div class="trust-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 3l7 3v6c0 5-3.5 7.5-7 9-3.5-1.5-7-4-7-9V6l7-3z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M9 12l2.2 2.2L15.5 9.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
      <div>
        <h2>You file it. We never do.</h2>
        <p>Rotunda prepares your FPPC disclosures — it never submits anything to the FPPC or the Secretary of State on your behalf. The filled PDF is always shown for review first, and nothing is marked "ready to file" until you type your legal name and confirm it yourself.</p>
        <p>Fields we can't verify — subcontracted clients, individual lobbyists beyond the account holder — stay blank instead of being guessed, and the review page says so.</p>
      </div>
    </div>
  </div>
</section>

<section class="mkt-section" style="padding-top:0">
  <div class="mkt-wrap">
    <div class="cta-band card">
      <h2>Stop tracking bills in a spreadsheet.</h2>
      <div class="hero-ctas">
        <a href="/signup" class="btn" style="display:inline-flex;align-items:center;justify-content:center;min-height:2.75rem;padding:0 1.1rem;border-radius:8px;background:var(--accent-solid);color:var(--accent-solid-text);font-weight:600;font-size:0.875rem;">Start tracking bills</a>
      </div>
      <p class="foot">Look up your first bill in seconds. No account needed until you flag one.</p>
    </div>
  </div>
</section>

<footer class="mkt-footer">
  <div class="mkt-wrap">
    <div class="foot-row">
      <div class="foot-brand">
        {TOP_BRAND}
        <p>Legislative tracking and lobbying-disclosure prep for California lobbying firms. Built by Noble Law.</p>
      </div>
      <div class="foot-col">
        <h4>Product</h4>
        <a href="/lookup">Look up a bill</a>
        <a href="#features">Features</a>
        <a href="#workflow">Workflow</a>
        <a href="#trust">Compliance</a>
      </div>
    </div>
    <div class="foot-bottom">&copy; 2026 Rotunda. A Noble Law product. Not affiliated with the FPPC or California Secretary of State.</div>
  </div>
</footer>
</body>
</html>
"""


PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Look up a bill — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{top_nav("/lookup")}
<div class="wrap">
  <h1>Look up a bill</h1>
  <p class="sub">California bill status, sponsors, and history from LegiScan.</p>

  <div class="card">
    <form id="f" style="margin:0">
      <input id="bill" placeholder="e.g. SB122" autocomplete="off" required>
      <button type="submit">Look up</button>
    </form>
  </div>

  <div id="loading">Searching LegiScan…</div>
  <div id="error"></div>
  <div id="result"></div>
</div>

<script>
const form = document.getElementById('f');
const resultEl = document.getElementById('result');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');
let current = null;

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const bill = document.getElementById('bill').value.trim();
  if (!bill) return;

  errorEl.className = ''; resultEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button').disabled = true;

  try {{
    const res = await fetch(`/api/bill?bill=${{encodeURIComponent(bill)}}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Lookup failed');
    current = data;
    render(data);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }} finally {{
    loadingEl.className = '';
    form.querySelector('button').disabled = false;
  }}
}});

async function flagBill() {{
  if (!current) return;
  const btn = document.getElementById('flag-btn');
  btn.disabled = true;
  btn.textContent = 'Flagging…';
  try {{
    const res = await fetch('/api/flag', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ bill_id: current.id }}),
    }});
    const data = await res.json();
    if (!res.ok) {{
      if (res.status === 401) throw new Error('Sign in to flag bills — see the account menu, top right.');
      throw new Error(data.error || 'Could not flag this bill');
    }}
    btn.textContent = '🚩 Flagged';
  }} catch (err) {{
    btn.disabled = false;
    btn.textContent = 'Flag this bill';
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

function render(d) {{
  const sponsors = (d.sponsors || []).map(s =>
    `<span class="sponsor">${{s.name}}${{s.party ? ' (' + s.party + ')' : ''}}</span>`
  ).join('');

  const history = (d.history || []).map(h =>
    `<tr><td class="date">${{h.date || ''}}</td><td class="chamber">${{h.chamber || ''}}</td><td>${{h.action || ''}}</td></tr>`
  ).join('');

  resultEl.innerHTML = `
    <div class="card">
      <div class="bill-id">${{d.state}} ${{d.bill_number}}${{d.session_label ? ` — ${{d.session_label}}` : ''}}</div>
      ${{d.status_label ? `<div class="status-badge">${{d.status_label}}</div>` : ''}}
      <div class="bill-title">${{d.title || ''}}</div>
      <div class="bill-desc">${{d.description || ''}}</div>
      ${{d.url ? `<a class="bill-link" href="${{d.url}}" target="_blank" rel="noopener">View on LegiScan →</a>` : ''}}
      <div class="card-actions">
        <button id="flag-btn" class="secondary" onclick="flagBill()">Flag this bill</button>
      </div>
    </div>
    ${{sponsors ? `<h2 class="section">Sponsors</h2><div class="sponsor-list">${{sponsors}}</div>` : ''}}
    <div class="panel" style="margin-top:1rem">
      <div class="panel-head"><div class="title">History</div></div>
      <table>${{history || '<tr><td style="padding:1rem 1.15rem">No history available.</td></tr>'}}</table>
    </div>
  `;
  resultEl.className = 'show';
}}
</script>
</body>
</html>
"""


LOBBYING_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Organization Search — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{top_nav("/lobbying")}
<div class="wrap">
  <h1>Organization Search</h1>
  <p class="sub">California lobbying firms, employers, and quarterly disclosures from CAL-ACCESS.</p>

  <div class="card">
    <form id="f" style="margin:0 0 1rem">
      <input id="q" placeholder="Firm, employer, or client name" autocomplete="off" required style="flex:1">
      <button type="submit">Search</button>
    </form>
    <p class="sub" style="margin:0;font-size:0.82rem">
      <strong>Firm</strong> = hired by clients to lobby on their behalf &nbsp;·&nbsp;
      <strong>Employer</strong> = lobbies with its own in-house staff &nbsp;·&nbsp;
      <strong>Coalition</strong> = a group of organizations registered together
    </p>
  </div>

  <div id="loading">Searching…</div>
  <div id="error"></div>
  <div id="results"></div>
</div>

<script>
const form = document.getElementById('f');
const resultsEl = document.getElementById('results');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const q = document.getElementById('q').value.trim();
  if (!q) return;

  errorEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button').disabled = true;

  try {{
    const res = await fetch(`/api/lobbying/search?q=${{encodeURIComponent(q)}}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Search failed');
    renderResults(data);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }} finally {{
    loadingEl.className = '';
    form.querySelector('button').disabled = false;
  }}
}});

function detailUrl(r) {{
  const params = r.id ? `id=${{encodeURIComponent(r.id)}}` : `name=${{encodeURIComponent(r.name)}}`;
  return `/lobbying/detail?${{params}}`;
}}

function locationOrContext(r) {{
  // Entity-kind rows have a real registered address; client-only rows
  // (named in someone else's filing, never independently registered)
  // don't — those would otherwise show blank Location/Status next to
  // each other for near-identical names (several "Amazon" variants
  // that are only ever mentioned, not registered), which is exactly
  // what made them hard to tell apart. Showing how often and how
  // recently each one was mentioned gives a real distinguishing detail
  // instead of a blank cell.
  if (r.entity_type) return [r.city, r.state].filter(Boolean).join(', ');
  if (r.mention_count) {{
    const latest = r.latest_filed ? r.latest_filed.split(' ')[0] : 'unknown';
    return `Mentioned ${{r.mention_count}}×, latest ${{latest}}`;
  }}
  return '';
}}

function renderResults(rows) {{
  if (!rows.length) {{
    resultsEl.innerHTML = '<p class="empty">No firms, employers, or named clients match that.</p>';
    return;
  }}
  resultsEl.innerHTML = `
    <div class="panel">
      <table>
        <thead><tr><th>Name</th><th>Type</th><th>Location</th><th>Status</th><th></th></tr></thead>
        <tbody>
        ${{rows.map(r => `
          <tr>
            <td><a href="${{detailUrl(r)}}">${{r.name}}</a></td>
            <td>${{r.entity_type ? `<span class="tag">${{r.entity_type}}</span>` : `<span class="tag">named as client only</span>`}}</td>
            <td>${{locationOrContext(r)}}</td>
            <td>${{r.registration_status || ''}}</td>
            <td><a class="secondary" href="/clients?prefill_name=${{encodeURIComponent(r.name)}}${{r.id ? `&prefill_entity_id=${{r.id}}` : ''}}">+ Client</a></td>
          </tr>
        `).join('')}}
        </tbody>
      </table>
    </div>
  `;
}}

</script>
</body>
</html>
"""


# A real destination for one organization's detail, rather than an
# in-page div appended below the whole results list — that older layout
# meant scrolling past every result to find what you clicked. Reached
# via ?id=... (a registered entity) or ?name=... (a client only ever
# named in someone else's filing, never independently registered).
LOBBYING_DETAIL_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Organization Detail — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{top_nav("/lobbying", left_extra='<a href="/lobbying">← Organization Search</a>')}
<div class="wrap">
  <div id="error"></div>
  <div id="detail"><p class="empty">Loading…</p></div>
</div>

<script>
const errorEl = document.getElementById('error');
const detailEl = document.getElementById('detail');
const params = new URLSearchParams(window.location.search);

function money(n) {{
  return typeof n === 'number' ? '$' + n.toLocaleString(undefined, {{maximumFractionDigits: 0}}) : '';
}}

function highlight(text, name) {{
  return (text || '').toLowerCase() === (name || '').toLowerCase() ? `<strong>${{text}}</strong>` : (text || '');
}}

function relationshipRows(rows, selectedName) {{
  if (!rows.length) return '<div class="panel" style="padding:1rem 1.15rem"><p class="empty">No lobbying relationships found for this name.</p></div>';
  return `
    <div class="panel">
      <div class="panel-head"><div class="title">Lobbying relationships</div></div>
      <table>
        <thead><tr><th>Firm</th><th>Client / employer</th><th>Period</th><th>Amount</th><th>Bill / activity</th></tr></thead>
        <tbody>
        ${{rows.map(r => `
          <tr>
            <td>${{highlight(r.firm, selectedName)}}</td>
            <td>${{highlight(r.client, selectedName)}}</td>
            <td class="date">${{(r.period_start || '').split(' ')[0]}} – ${{(r.period_end || '').split(' ')[0]}}</td>
            <td>${{money(r.amount_spent)}}</td>
            <td>${{r.raw_bill_text || ''}}</td>
          </tr>
        `).join('')}}
        </tbody>
      </table>
    </div>
  `;
}}

function addClientUrl(d) {{
  const p = new URLSearchParams({{prefill_name: d.name}});
  if (d.entity && d.entity.id) p.set('prefill_entity_id', d.entity.id);
  return `/clients?${{p.toString()}}`;
}}

function renderDetail(d) {{
  const e = d.entity;
  detailEl.innerHTML = `
    <div class="card">
      <div class="bill-title">${{d.name}}</div>
      ${{e ? `
        <div style="margin-top:0.3rem">
          <span class="tag">${{e.entity_type || ''}}</span>
          ${{e.registration_status ? `<span class="tag">${{e.registration_status}}</span>` : ''}}
          ${{e.source_form ? `<span class="tag">Form ${{e.source_form}}</span>` : ''}}
        </div>
        <div class="bill-desc" style="margin-top:0.5rem">${{[e.address, e.city, e.state, e.zip].filter(Boolean).join(', ')}}</div>
      ` : '<div class="bill-desc" style="margin-top:0.3rem">Named as a client in a disclosure — no independent registration on file.</div>'}}
      <div class="card-actions">
        <a class="secondary" href="${{addClientUrl(d)}}">+ Add as client</a>
      </div>
    </div>
    <div style="margin-top:1rem">${{relationshipRows(d.relationships, d.name)}}</div>
  `;
}}

async function load() {{
  try {{
    const p = params.get('id') ? `id=${{encodeURIComponent(params.get('id'))}}` : `name=${{encodeURIComponent(params.get('name') || '')}}`;
    const res = await fetch(`/api/lobbying/detail?${{p}}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not load detail');
    renderDetail(data);
  }} catch (err) {{
    detailEl.innerHTML = '';
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

load();
</script>
</body>
</html>
"""


SIGNUP_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign up — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{top_nav("/signup", left_extra='<a href="/lookup">← Lookup</a><a href="/login">Log in →</a>')}
<div class="wrap">
  <h1>Create your account</h1>
  <p class="sub">Step 1 of 2 — after this, you'll fill in your CAL-ACCESS-style registration details.</p>

  <div class="card">
    <form id="f" style="margin:0">
      <input id="email" type="email" placeholder="you@example.com" autocomplete="email" required style="flex:1 1 100%">
      <input id="password" type="password" placeholder="Password (8+ characters)" autocomplete="new-password" required style="flex:1 1 100%">
      <button type="submit">Continue →</button>
    </form>
  </div>

  <div id="loading">Creating account…</div>
  <div id="error"></div>
</div>

<script>
const form = document.getElementById('f');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;

  errorEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button').disabled = true;

  try {{
    const res = await fetch('/api/signup', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ email, password }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not create account');
    window.location.href = '/signup/profile';
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
    loadingEl.className = '';
    form.querySelector('button').disabled = false;
  }}
}});
</script>
</body>
</html>
"""


LOGIN_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log in — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{top_nav("/login", left_extra='<a href="/lookup">← Lookup</a><a href="/signup">Sign up →</a>')}
<div class="wrap">
  <h1>Log in</h1>

  <div class="card">
    <form id="f" style="margin:0">
      <input id="email" type="email" placeholder="you@example.com" autocomplete="email" required style="flex:1 1 100%">
      <input id="password" type="password" placeholder="Password" autocomplete="current-password" required style="flex:1 1 100%">
      <button type="submit">Log in</button>
    </form>
  </div>

  <div id="loading">Logging in…</div>
  <div id="error"></div>
</div>

<script>
const form = document.getElementById('f');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;

  errorEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button').disabled = true;

  try {{
    const res = await fetch('/api/login', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ email, password }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not log in');
    window.location.href = '/flagged';
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
    loadingEl.className = '';
    form.querySelector('button').disabled = false;
  }}
}});
</script>
</body>
</html>
"""


PROFILE_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Registration details — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{top_nav("/signup/profile", left_extra='<a href="/flagged">Skip for now →</a>')}
<div class="wrap">
  <h1>Registration details</h1>
  <p class="sub">Step 2 of 2 — modeled on CAL-ACCESS Form 601 (Lobbying Firm Registration Statement), so the fields match what you'd already recognize from the state's own form.</p>

  <div class="card">
  <form id="f">
    <label style="flex:1 1 100%">
      <div class="sub" style="margin:0 0 0.3rem">Legal name of firm or individual</div>
      <input id="legal_name" required style="width:100%">
    </label>

    <div style="flex:1 1 100%">
      <div class="sub" style="margin:0 0 0.3rem">Registering as</div>
      <label style="display:inline-flex;align-items:center;gap:0.4rem;margin-right:1.2rem;font-size:0.9rem">
        <input type="radio" name="registrant_type" value="individual" style="width:auto" required> Individual lobbyist
      </label>
      <label style="display:inline-flex;align-items:center;gap:0.4rem;font-size:0.9rem">
        <input type="radio" name="registrant_type" value="firm" style="width:auto"> Firm
      </label>
    </div>

    <div style="flex:1 1 100%">
      <h2 class="section" style="margin-top:1.2rem">Business address</h2>
    </div>
    <input id="bus_addr1" placeholder="Street address" style="flex:1 1 100%">
    <input id="bus_city" placeholder="City" style="flex:2">
    <input id="bus_st" placeholder="State" maxlength="2" style="flex:1;text-transform:uppercase">
    <input id="bus_zip4" placeholder="ZIP" style="flex:1">

    <div style="flex:1 1 100%">
      <h2 class="section" style="margin-top:1.2rem">Mailing address</h2>
      <label style="display:inline-flex;align-items:center;gap:0.4rem;font-size:0.9rem;margin-bottom:0.7rem">
        <input type="checkbox" id="mail_same" checked style="width:auto"> Same as business address
      </label>
    </div>
    <div id="mail_fields" style="display:none;flex:1 1 100%;gap:0.6rem;flex-wrap:wrap">
      <input id="mail_addr1" placeholder="Street address" style="flex:1 1 100%">
      <input id="mail_city" placeholder="City" style="flex:2">
      <input id="mail_st" placeholder="State" maxlength="2" style="flex:1;text-transform:uppercase">
      <input id="mail_zip4" placeholder="ZIP" style="flex:1">
    </div>

    <div style="flex:1 1 100%">
      <h2 class="section" style="margin-top:1.2rem">Phone number</h2>
    </div>
    <input id="bus_phone" placeholder="(916) 555-0100" style="flex:1 1 100%">

    <div style="flex:1 1 100%">
      <h2 class="section" style="margin-top:1.2rem">California Secretary of State filer ID <span style="text-transform:none;font-weight:400;color:var(--slate)">(optional — if you already have one)</span></h2>
    </div>
    <input id="existing_filer_id" placeholder="e.g. 1486088" style="flex:1 1 100%">

    <button type="submit" style="margin-top:1rem">Save and finish →</button>
  </form>
  </div>

  <div id="loading">Saving…</div>
  <div id="error"></div>
</div>

<script>
const form = document.getElementById('f');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');
const mailSame = document.getElementById('mail_same');
const mailFields = document.getElementById('mail_fields');

mailSame.addEventListener('change', () => {{
  mailFields.style.display = mailSame.checked ? 'none' : 'flex';
}});

// This same form doubles as "edit profile" (linked from /profile) as
// well as sign-up step 2 — if a profile already exists, pre-fill it
// rather than showing a blank form the user has to redo from scratch.
(async function prefill() {{
  try {{
    const res = await fetch('/api/profile');
    if (!res.ok) return;
    const {{ profile }} = await res.json();
    if (!profile) return;
    document.getElementById('legal_name').value = profile.legal_name || '';
    const radio = form.querySelector(`input[name="registrant_type"][value="${{profile.registrant_type}}"]`);
    if (radio) radio.checked = true;
    document.getElementById('bus_addr1').value = profile.bus_addr1 || '';
    document.getElementById('bus_city').value = profile.bus_city || '';
    document.getElementById('bus_st').value = profile.bus_st || '';
    document.getElementById('bus_zip4').value = profile.bus_zip4 || '';
    mailSame.checked = !!profile.mail_same_as_bus;
    mailFields.style.display = mailSame.checked ? 'none' : 'flex';
    document.getElementById('mail_addr1').value = profile.mail_addr1 || '';
    document.getElementById('mail_city').value = profile.mail_city || '';
    document.getElementById('mail_st').value = profile.mail_st || '';
    document.getElementById('mail_zip4').value = profile.mail_zip4 || '';
    document.getElementById('bus_phone').value = profile.bus_phone || '';
    document.getElementById('existing_filer_id').value = profile.existing_filer_id || '';
    form.querySelector('button[type="submit"]').textContent = 'Save changes';
    document.querySelector('h1').textContent = 'Edit your registration details';
    document.querySelector('.sub').textContent =
      'Modeled on CAL-ACCESS Form 601 (Lobbying Firm Registration Statement).';
  }} catch (err) {{ /* no profile yet — leave the blank sign-up form as-is */ }}
}})();

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const registrantType = form.querySelector('input[name="registrant_type"]:checked');

  errorEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button').disabled = true;

  try {{
    const res = await fetch('/api/profile', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        legal_name: document.getElementById('legal_name').value.trim(),
        registrant_type: registrantType ? registrantType.value : '',
        bus_addr1: document.getElementById('bus_addr1').value.trim(),
        bus_city: document.getElementById('bus_city').value.trim(),
        bus_st: document.getElementById('bus_st').value.trim(),
        bus_zip4: document.getElementById('bus_zip4').value.trim(),
        mail_same_as_bus: mailSame.checked,
        mail_addr1: document.getElementById('mail_addr1').value.trim(),
        mail_city: document.getElementById('mail_city').value.trim(),
        mail_st: document.getElementById('mail_st').value.trim(),
        mail_zip4: document.getElementById('mail_zip4').value.trim(),
        bus_phone: document.getElementById('bus_phone').value.trim(),
        existing_filer_id: document.getElementById('existing_filer_id').value.trim(),
      }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not save');
    window.location.href = '/profile';
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
    loadingEl.className = '';
    form.querySelector('button').disabled = false;
  }}
}});
</script>
</body>
</html>
"""


PROFILE_BODY = f"""
<div class="page-head">
  <div>
    <h1>Your Profile</h1>
    <p class="sub">Your CAL-ACCESS registration details — used to pre-fill disclosure forms.</p>
  </div>
</div>
<div id="loading">Loading…</div>
<div id="error"></div>
<div id="content"></div>

<script>
const contentEl = document.getElementById('content');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');

function row(label, value) {{
  if (!value) return '';
  return `<div style="display:grid;grid-template-columns:11rem 1fr;padding:0.45rem 0;border-bottom:1px solid var(--rule)">
    <div class="sub" style="margin:0">${{label}}</div><div>${{value}}</div>
  </div>`;
}}

async function load() {{
  try {{
    const [meRes, profileRes] = await Promise.all([fetch('/api/me'), fetch('/api/profile')]);
    const me = await meRes.json();
    const {{ profile }} = await profileRes.json();

    if (!profile) {{
      contentEl.innerHTML = `
        <div class="panel" style="padding:1.5rem">
          <p class="empty">You haven't filled in your registration details yet.</p>
          <button type="button" onclick="window.location.href='/signup/profile'" style="margin-top:0.8rem">Add registration details →</button>
        </div>
      `;
      loadingEl.className = '';
      return;
    }}

    const mailing = profile.mail_same_as_bus
      ? 'Same as business address'
      : [profile.mail_addr1, profile.mail_city, profile.mail_st, profile.mail_zip4].filter(Boolean).join(', ');

    contentEl.innerHTML = `
      <div class="card">
        <div class="bill-id">${{me.email || ''}}</div>
        <div class="bill-title">${{profile.legal_name}}</div>
        <span class="tag">${{profile.registrant_type === 'firm' ? 'Firm' : 'Individual lobbyist'}}</span>
      </div>
      <div class="panel" style="margin-top:1rem">
        <div class="panel-head"><div class="title">Registration details</div></div>
        <div style="padding:0 1.15rem">
          ${{row('Business address', [profile.bus_addr1, profile.bus_city, profile.bus_st, profile.bus_zip4].filter(Boolean).join(', '))}}
          ${{row('Mailing address', mailing)}}
          ${{row('Phone', profile.bus_phone)}}
          ${{row('CA SOS filer ID', profile.existing_filer_id)}}
        </div>
        <div class="card-actions" style="padding:1rem 1.15rem">
          <button type="button" class="secondary" onclick="window.location.href='/signup/profile'">Edit →</button>
        </div>
      </div>
    `;
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }} finally {{
    loadingEl.className = '';
  }}
}}

load();
</script>
"""

PROFILE_VIEW_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your profile — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{app_shell("/profile", PROFILE_BODY)}
</body>
</html>
"""


FLAGGED_BODY = f"""
<div class="page-head">
  <div>
    <h1>Flagged Bills</h1>
    <p class="sub">Bills you've personally flagged — stored and re-checked daily the same way the shared watch list is, just scoped to your account.</p>
  </div>
  <div class="filter-tabs" id="tabs"></div>
</div>
<div id="error"></div>
<div class="stat-grid" id="stats"></div>

<div class="panel">
  <div class="panel-head">
    <div>
      <div class="title">Today's Digest</div>
      <div class="sub">Not tracked yet — coming soon</div>
    </div>
    <button type="button" class="panel-link" disabled title="Not built yet">View digest</button>
  </div>
</div>

<div class="panel">
  <div class="panel-head">
    <div class="title">Flagged Bills</div>
    <div class="sub">Sorted by bill number</div>
  </div>
  <div id="list"></div>
</div>

<script>
const listEl = document.getElementById('list');
const errorEl = document.getElementById('error');
const tabsEl = document.getElementById('tabs');
const statsEl = document.getElementById('stats');
const searchEl = document.getElementById('shell-search');
let allClients = [];
let currentRows = [];
let preparedFilings = [];
let activeFilter = 'all';

const ICON_FLAG = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 1v12M2 2h8l-2 2.5L10 7H2" stroke-linejoin="round"/></svg>';
const ICON_CLIENTS = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5.5" cy="4.5" r="2.5"/><path d="M1 12c0-2.5 2-4.2 4.5-4.2S10 9.5 10 12" stroke-linecap="round"/></svg>';
const ICON_ALERT = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="5.5"/><path d="M7 4.5v3" stroke-linecap="round"/><circle cx="7" cy="9.8" r="0.6" fill="currentColor" stroke="none"/></svg>';
const ICON_DISCLOSURE = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="1.5" width="8" height="11" rx="1"/><path d="M5.2 6l1 1 2.2-2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function renderStats() {{
  if (!currentRows.length) {{ statsEl.innerHTML = ''; return; }}
  const unassigned = currentRows.filter(r => !(r.assigned_clients || []).length).length;
  const pending = preparedFilings.filter(f => f.status === 'draft').length;
  statsEl.innerHTML = `
    <div class="stat-card">
      <div class="stat-icon">${{ICON_FLAG}}</div>
      <div class="stat-label">Flagged bills</div>
      <div class="stat-value">${{currentRows.length}}</div>
      <div class="stat-foot">Refreshed once a day</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">${{ICON_CLIENTS}}</div>
      <div class="stat-label">Active clients</div>
      <div class="stat-value">${{allClients.length}}</div>
      <div class="stat-foot">Clients you're tracking bills for</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">${{ICON_ALERT}}</div>
      <div class="stat-label">Needs a client</div>
      <div class="stat-value">${{unassigned}}</div>
      <div class="stat-foot">Flagged bills with no client assigned</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">${{ICON_DISCLOSURE}}</div>
      <div class="stat-label">Disclosures</div>
      <div class="stat-value">${{pending}}</div>
      <div class="stat-foot">${{pending ? 'Drafted, awaiting your sign-off' : 'None waiting on you'}}</div>
    </div>
  `;
}}

const FILTERS = [['all', 'All'], ['support', 'Support'], ['oppose', 'Oppose'], ['watch', 'Watch']];

function matchesFilter(r, filter) {{
  if (filter === 'all') return true;
  return (r.assigned_clients || []).some(c => (c.position || 'watch') === filter);
}}

function matchesSearch(r, q) {{
  if (!q) return true;
  return `${{r.bill_number || ''}} ${{r.title || ''}}`.toLowerCase().includes(q);
}}

function applyFilters() {{
  const q = (searchEl ? searchEl.value : '').trim().toLowerCase();
  render(currentRows.filter(r => matchesFilter(r, activeFilter) && matchesSearch(r, q)));
}}

function setFilter(filter) {{
  activeFilter = filter;
  renderTabs();
  applyFilters();
}}

function renderTabs() {{
  if (!currentRows.length) {{
    tabsEl.innerHTML = '';
    return;
  }}
  tabsEl.innerHTML = FILTERS.map(([value, label]) => {{
    const n = currentRows.filter(r => matchesFilter(r, value)).length;
    return `<button type="button" class="filter-tab ${{value === activeFilter ? 'active' : ''}}" onclick="setFilter('${{value}}')">${{label}}<span class="n">${{n}}</span></button>`;
  }}).join('');
}}

if (searchEl) {{
  searchEl.addEventListener('input', applyFilters);
}}

async function load() {{
  try {{
    const [flaggedRes, clientsRes, filingsRes] = await Promise.all([
      fetch('/api/flagged'), fetch('/api/clients'), fetch('/api/prepared-filings'),
    ]);
    if (flaggedRes.status === 401) {{
      window.location.href = '/login';
      return;
    }}
    allClients = await clientsRes.json();
    currentRows = await flaggedRes.json();
    preparedFilings = filingsRes.ok ? await filingsRes.json() : [];
    renderStats();
    renderTabs();
    applyFilters();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

function toggleRowMenu(e, billId) {{
  e.stopPropagation();
  const menu = document.getElementById(`row-menu-${{billId}}`);
  const wasOpen = menu.classList.contains('show');
  document.querySelectorAll('.row-menu-dropdown.show').forEach(m => m.classList.remove('show'));
  if (!wasOpen) menu.classList.add('show');
}}
document.addEventListener('click', () => {{
  document.querySelectorAll('.row-menu-dropdown.show').forEach(m => m.classList.remove('show'));
}});

async function unflag(billId) {{
  try {{
    const res = await fetch(`/api/flag?bill_id=${{billId}}`, {{ method: 'DELETE' }});
    if (!res.ok) {{
      const data = await res.json();
      throw new Error(data.error || 'Could not unflag bill');
    }}
    load();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

async function assignClient(billId, selectEl) {{
  const clientId = selectEl.value;
  if (!clientId) return;
  try {{
    const res = await fetch('/api/bill-clients', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ bill_id: billId, client_id: Number(clientId) }}),
    }});
    if (!res.ok) {{
      const data = await res.json();
      throw new Error(data.error || 'Could not assign client');
    }}
    load();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

async function unassignClient(billId, clientId) {{
  try {{
    const res = await fetch(`/api/bill-clients?bill_id=${{billId}}&client_id=${{clientId}}`, {{ method: 'DELETE' }});
    if (!res.ok) {{
      const data = await res.json();
      throw new Error(data.error || 'Could not remove assignment');
    }}
    load();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

const POSITIONS = [['watch', 'Watch'], ['support', 'Support'], ['oppose', 'Oppose']];

async function setPosition(billId, clientId, position) {{
  try {{
    const res = await fetch('/api/bill-clients', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ bill_id: billId, client_id: clientId, position }}),
    }});
    if (!res.ok) {{
      const data = await res.json();
      throw new Error(data.error || 'Could not update position');
    }}
    load();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

function positionSelect(r, c) {{
  const position = c.position || 'watch';
  const options = POSITIONS.map(([value, label]) =>
    `<option value="${{value}}" ${{position === value ? 'selected' : ''}}>${{label}}</option>`
  ).join('');
  return `<select class="position-select ${{position}}" onchange="setPosition(${{r.bill_id}}, ${{c.id}}, this.value); this.className = 'position-select ' + this.value" style="font-size:0.78rem;padding:0.3rem 0.5rem;font-weight:600">${{options}}</select>`;
}}

function clientCell(r) {{
  const chips = (r.assigned_clients || []).map(c => `
    <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.4rem">
      <a href="/clients/detail?id=${{c.id}}">${{c.name}}</a>
      ${{positionSelect(r, c)}}
      <a href="#" onclick="event.preventDefault(); unassignClient(${{r.bill_id}}, ${{c.id}})" style="color:var(--slate)" title="Remove client">×</a>
    </div>
  `).join('');

  const assignedIds = new Set((r.assigned_clients || []).map(c => c.id));
  const available = allClients.filter(c => !assignedIds.has(c.id));

  if (!allClients.length) {{
    return chips + '<div class="empty">No clients yet — <a href="/clients">add one</a>.</div>';
  }}
  const options = available.map(c => `<option value="${{c.id}}">${{c.name}}</option>`).join('');
  return `
    <div>${{chips}}</div>
    <select onchange="assignClient(${{r.bill_id}}, this); this.value=''" style="margin-top:0.2rem;font-size:0.8rem;padding:0.3rem 0.4rem">
      <option value="">${{available.length ? 'Assign to client…' : 'All clients assigned'}}</option>
      ${{options}}
    </select>
  `;
}}

function render(rows) {{
  if (!currentRows.length) {{
    listEl.innerHTML = '<p class="empty">Nothing flagged yet — look up a bill and click "Flag this bill" from there.</p>';
    return;
  }}
  if (!rows.length) {{
    const q = (searchEl ? searchEl.value : '').trim();
    const filterLabel = activeFilter !== 'all'
      ? ` marked "${{(FILTERS.find(([value]) => value === activeFilter) || [null, activeFilter])[1]}}"` : '';
    const searchNote = q ? ` matching "${{q}}"` : '';
    listEl.innerHTML = `<p class="empty">No flagged bills${{filterLabel}}${{searchNote}} right now — try <a href="#" onclick="event.preventDefault(); if (searchEl) searchEl.value=''; setFilter('all')">clearing filters</a>.</p>`;
    return;
  }}
  listEl.innerHTML = `
    <table>
      <thead><tr><th>Bill</th><th>Status</th><th>Last checked</th><th>Clients &amp; positions</th><th></th></tr></thead>
      <tbody>
      ${{rows.map(r => `
        <tr>
          <td>
            <div class="bill-row">
              <div class="bill-icon">
                <svg viewBox="0 0 14 14" fill="currentColor"><rect x="1" y="1" width="5" height="5" rx="1"/><rect x="8" y="1" width="5" height="5" rx="1"/><rect x="1" y="8" width="5" height="5" rx="1"/><rect x="8" y="8" width="5" height="5" rx="1"/></svg>
              </div>
              <div>
                <div class="bill-title">${{r.title || ''}}</div>
                <div class="bill-id">${{r.state}} ${{r.bill_number}}</div>
              </div>
            </div>
            <div class="row-actions">
              ${{r.url ? `<a class="secondary" href="${{r.url}}" target="_blank" rel="noopener">View</a>` : ''}}
              <a class="secondary" href="/report?bill_id=${{r.bill_id}}">Report</a>
            </div>
          </td>
          <td>${{r.status_label ? `<span class="status-badge">${{r.status_label}}</span>` : ''}}</td>
          <td class="date">${{(r.last_checked_at || '').replace('T', ' ').slice(0, 16)}}</td>
          <td>${{clientCell(r)}}</td>
          <td class="row-menu">
            <button type="button" class="row-menu-btn" onclick="toggleRowMenu(event, ${{r.bill_id}})" aria-label="Bill actions">
              <svg viewBox="0 0 14 14" fill="currentColor"><circle cx="7" cy="3" r="1.6"/><circle cx="7" cy="7" r="1.6"/><circle cx="7" cy="11" r="1.6"/></svg>
            </button>
            <div class="row-menu-dropdown" id="row-menu-${{r.bill_id}}">
              <button type="button" onclick="unflag(${{r.bill_id}})">Unflag this bill</button>
            </div>
          </td>
        </tr>
      `).join('')}}
      </tbody>
    </table>
  `;
}}

load();
</script>
"""

FLAGGED_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Flagged Bills — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{app_shell("/flagged", FLAGGED_BODY)}
</body>
</html>
"""


CLIENTS_BODY = f"""
<div class="page-head">
  <div>
    <h1>Clients</h1>
    <p class="sub">Modeled on CAL-ACCESS Forms 602/603, tied to your account.</p>
  </div>
  <button type="button" id="add-client-btn">+ Add client</button>
</div>

<div class="stat-grid" id="stats"></div>

<div class="card" id="form-card" style="display:none">
  <form id="f">
    <label style="flex:1 1 100%">
      <div class="sub" style="margin:0 0 0.3rem">Client / employer name</div>
      <input id="name" required style="width:100%">
    </label>

    <div style="flex:1 1 100%">
      <h2 class="section" style="margin-top:1.2rem">Business address</h2>
    </div>
    <input id="bus_addr1" placeholder="Street address" style="flex:1 1 100%">
    <input id="bus_city" placeholder="City" style="flex:2">
    <input id="bus_st" placeholder="State" maxlength="2" style="flex:1;text-transform:uppercase">
    <input id="bus_zip4" placeholder="ZIP" style="flex:1">

    <label style="flex:1 1 100%">
      <div class="sub" style="margin:1.2rem 0 0.3rem">Description of the client's industry or interests</div>
      <textarea id="interests" rows="2" style="width:100%"></textarea>
    </label>

    <label style="flex:1 1 100%">
      <div class="sub" style="margin:1.2rem 0 0.3rem">California Secretary of State filer ID <span style="font-weight:400">(optional — if you know it)</span></div>
      <input id="existing_filer_id" placeholder="e.g. 1486088" style="width:100%">
    </label>

    <div style="flex:1 1 100%">
      <h2 class="section" style="margin-top:1.2rem">For disclosure forms <span style="font-weight:400;text-transform:none">(optional — used to pre-fill Form 601, see /disclosures)</span></h2>
    </div>
    <label style="flex:1">
      <div class="sub" style="margin:0 0 0.3rem">Effective date</div>
      <input id="effective_date" type="date" style="width:100%">
    </label>
    <label style="flex:1">
      <div class="sub" style="margin:0 0 0.3rem">Period of contract</div>
      <input id="contract_period" placeholder="e.g. Ongoing, or a date range" style="width:100%">
    </label>
    <label style="flex:1 1 100%">
      <div class="sub" style="margin:1.2rem 0 0.3rem">Agencies to be lobbied on this client's behalf</div>
      <textarea id="agencies_lobbied" rows="2" style="width:100%"></textarea>
    </label>

    <button type="submit" id="submit-client-btn" style="margin-top:1rem">Add client →</button>
    <button type="button" id="cancel-client-btn" class="secondary" style="margin-top:1rem">Cancel</button>
  </form>
</div>

<div id="loading">Saving…</div>
<div id="error"></div>

<div class="panel">
  <div class="panel-head"><div class="title">Your clients</div></div>
  <div id="list"></div>
</div>

<script>
const form = document.getElementById('f');
const formCard = document.getElementById('form-card');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');
const listEl = document.getElementById('list');
const statsEl = document.getElementById('stats');
const addBtn = document.getElementById('add-client-btn');
const cancelBtn = document.getElementById('cancel-client-btn');
const submitBtn = document.getElementById('submit-client-btn');
let allClients = [];
let editingId = null;  // null = creating a new client; otherwise the id being edited

const ICON_CLIENTS = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5.5" cy="4.5" r="2.5"/><path d="M1 12c0-2.5 2-4.2 4.5-4.2S10 9.5 10 12" stroke-linecap="round"/><circle cx="10.5" cy="5.5" r="1.8"/><path d="M9 12c0-1.7 1.3-3 3-3" stroke-linecap="round"/></svg>';
const ICON_ALERT = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="5.5"/><path d="M7 4.5v3" stroke-linecap="round"/><circle cx="7" cy="9.8" r="0.6" fill="currentColor" stroke="none"/></svg>';

function renderStats() {{
  if (!allClients.length) {{ statsEl.innerHTML = ''; return; }}
  const missingFilerId = allClients.filter(c => !c.existing_filer_id).length;
  statsEl.innerHTML = `
    <div class="stat-card">
      <div class="stat-icon">${{ICON_CLIENTS}}</div>
      <div class="stat-label">Clients</div>
      <div class="stat-value">${{allClients.length}}</div>
      <div class="stat-foot">Tied to your account</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">${{ICON_ALERT}}</div>
      <div class="stat-label">Missing filer ID</div>
      <div class="stat-value">${{missingFilerId}}</div>
      <div class="stat-foot">Optional, but useful for cross-checking CAL-ACCESS</div>
    </div>
  `;
}}

// Arriving from Organization Search's "+ Client" link (?prefill_name=...
// and, for a registered entity, &prefill_entity_id=...) opens the form
// pre-filled from that entity's CAL-ACCESS record instead of leaving
// name/address/etc. to be typed in by hand. Everything stays a normal,
// editable field either way — there's no separate "manual mode," typing
// over a prefilled value IS completing it manually.
const urlParams = new URLSearchParams(window.location.search);
const prefillName = urlParams.get('prefill_name');
const prefillEntityId = urlParams.get('prefill_entity_id');

async function applyPrefill() {{
  if (!prefillName) return;
  showForm();
  document.getElementById('name').value = prefillName;
  if (!prefillEntityId) return;
  try {{
    const res = await fetch(`/api/lobbying/detail?id=${{encodeURIComponent(prefillEntityId)}}`);
    const data = await res.json();
    if (!res.ok || !data.entity) return;
    const e = data.entity;
    document.getElementById('bus_addr1').value = e.address || '';
    document.getElementById('bus_city').value = e.city || '';
    document.getElementById('bus_st').value = e.state || '';
    document.getElementById('bus_zip4').value = e.zip || '';
    if (e.filer_id) document.getElementById('existing_filer_id').value = e.filer_id;
    const note = document.createElement('p');
    note.className = 'sub';
    note.style.marginTop = '-0.5rem';
    note.textContent = "Prefilled from CAL-ACCESS — edit anything below if it looks wrong.";
    form.insertBefore(note, form.firstChild);
  }} catch (err) {{
    // Prefill is a convenience, not a requirement — if it fails, the form
    // is already open and named, same as clicking "+ Add client" directly.
  }}
}}

function showForm() {{
  formCard.style.display = 'block';
  addBtn.style.display = 'none';
}}

function hideForm() {{
  formCard.style.display = 'none';
  addBtn.style.display = '';
  form.reset();
  editingId = null;
  submitBtn.textContent = 'Add client →';
  errorEl.className = '';
}}

function editClient(id) {{
  const c = allClients.find(x => x.id === id);
  if (!c) return;
  editingId = id;
  document.getElementById('name').value = c.name || '';
  document.getElementById('bus_addr1').value = c.bus_addr1 || '';
  document.getElementById('bus_city').value = c.bus_city || '';
  document.getElementById('bus_st').value = c.bus_st || '';
  document.getElementById('bus_zip4').value = c.bus_zip4 || '';
  document.getElementById('interests').value = c.interests || '';
  document.getElementById('existing_filer_id').value = c.existing_filer_id || '';
  document.getElementById('effective_date').value = c.effective_date || '';
  document.getElementById('contract_period').value = c.contract_period || '';
  document.getElementById('agencies_lobbied').value = c.agencies_lobbied || '';
  submitBtn.textContent = 'Save changes →';
  showForm();
}}

addBtn.addEventListener('click', showForm);
cancelBtn.addEventListener('click', hideForm);

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  errorEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button[type="submit"]').disabled = true;

  try {{
    const body = {{
      name: document.getElementById('name').value.trim(),
      bus_addr1: document.getElementById('bus_addr1').value.trim(),
      bus_city: document.getElementById('bus_city').value.trim(),
      bus_st: document.getElementById('bus_st').value.trim(),
      bus_zip4: document.getElementById('bus_zip4').value.trim(),
      interests: document.getElementById('interests').value.trim(),
      existing_filer_id: document.getElementById('existing_filer_id').value.trim(),
      effective_date: document.getElementById('effective_date').value.trim(),
      contract_period: document.getElementById('contract_period').value.trim(),
      agencies_lobbied: document.getElementById('agencies_lobbied').value.trim(),
    }};
    if (editingId) body.id = editingId;
    const res = await fetch('/api/clients', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not save client');
    hideForm();
    load();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }} finally {{
    loadingEl.className = '';
    form.querySelector('button[type="submit"]').disabled = false;
  }}
}});

async function removeClient(id) {{
  try {{
    const res = await fetch(`/api/clients?id=${{id}}`, {{ method: 'DELETE' }});
    if (!res.ok) {{
      const data = await res.json();
      throw new Error(data.error || 'Could not remove client');
    }}
    load();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

async function load() {{
  try {{
    const res = await fetch('/api/clients');
    if (res.status === 401) {{
      window.location.href = '/login';
      return;
    }}
    const rows = await res.json();
    allClients = rows;
    renderStats();
    render(rows);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

function render(rows) {{
  if (!rows.length) {{
    listEl.innerHTML = '<p class="empty">No clients added yet — use the form above.</p>';
    return;
  }}
  listEl.innerHTML = `
    <table>
      <thead><tr><th>Name</th><th>Business address</th><th>Industry / interests</th><th>Filer ID</th><th></th></tr></thead>
      <tbody>
      ${{rows.map(c => `
        <tr>
          <td><a href="/clients/detail?id=${{c.id}}">${{c.name}}</a></td>
          <td>${{[c.bus_addr1, c.bus_city, c.bus_st, c.bus_zip4].filter(Boolean).join(', ')}}</td>
          <td>${{c.interests || ''}}</td>
          <td>${{c.existing_filer_id || ''}}</td>
          <td class="row-menu">
            <a class="secondary" href="#" onclick="event.preventDefault(); editClient(${{c.id}})" style="margin-right:0.4rem">Edit</a>
            <button type="button" class="row-menu-btn" onclick="toggleRowMenu(event, 'client-${{c.id}}')" aria-label="More actions">
              <svg viewBox="0 0 14 14" fill="currentColor"><circle cx="7" cy="3" r="1.6"/><circle cx="7" cy="7" r="1.6"/><circle cx="7" cy="11" r="1.6"/></svg>
            </button>
            <div class="row-menu-dropdown" id="row-menu-client-${{c.id}}">
              <button type="button" onclick="removeClient(${{c.id}})">Remove client</button>
            </div>
          </td>
        </tr>
      `).join('')}}
      </tbody>
    </table>
  `;
}}

function toggleRowMenu(e, key) {{
  e.stopPropagation();
  const menu = document.getElementById(`row-menu-${{key}}`);
  const wasOpen = menu.classList.contains('show');
  document.querySelectorAll('.row-menu-dropdown.show').forEach(m => m.classList.remove('show'));
  if (!wasOpen) menu.classList.add('show');
}}
document.addEventListener('click', () => {{
  document.querySelectorAll('.row-menu-dropdown.show').forEach(m => m.classList.remove('show'));
}});

load();
applyPrefill();
</script>
"""

CLIENTS_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clients — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{app_shell("/clients", CLIENTS_BODY)}
</body>
</html>
"""


# Action report — everything about one bill in one place: current
# One client's own page: org info, every bill assigned to them with its
# position, and a way to add a new bill starting from here rather than
# only from /flagged — the reverse direction of the existing
# flag-then-assign flow. Reached via ?id=..., e.g. from the Clients list
# or Organization Search's "+ Add as client" link.
CLIENT_DETAIL_BODY = f"""
<div class="page-head"><div><a href="/clients" class="sub">← Clients</a></div></div>
<div id="error"></div>
<div id="client"></div>

<div class="card" style="margin-top:1rem">
  <div class="bill-title" style="margin-bottom:0.8rem">Add a bill</div>
  <form id="add-bill-f">
    <input id="bill_number" placeholder="e.g. SB122" autocomplete="off" required style="flex:1;min-width:8rem">
    <select id="add-bill-position">
      <option value="watch">Watch</option>
      <option value="support">Support</option>
      <option value="oppose">Oppose</option>
    </select>
    <button type="submit">Add →</button>
  </form>
  <div id="add-bill-loading" class="empty" style="display:none;margin-top:0.6rem">Looking up bill…</div>
</div>

<div class="panel" style="margin-top:1rem">
  <div class="panel-head"><div class="title">Bills</div></div>
  <div id="bills"></div>
</div>

<script>
const clientId = new URLSearchParams(window.location.search).get('id');
const errorEl = document.getElementById('error');
const clientEl = document.getElementById('client');
const billsEl = document.getElementById('bills');
const addBillForm = document.getElementById('add-bill-f');
const addBillLoading = document.getElementById('add-bill-loading');
const POSITIONS = [['watch', 'Watch'], ['support', 'Support'], ['oppose', 'Oppose']];

function renderClient(d) {{
  const c = d.client;
  clientEl.innerHTML = `
    <div class="card">
      <div class="bill-title">${{c.name}}</div>
      <div class="bill-desc" style="margin-top:0.4rem">
        ${{[c.bus_addr1, c.bus_city, c.bus_st, c.bus_zip4].filter(Boolean).join(', ') || 'No address on file'}}
      </div>
      ${{c.interests ? `<div class="bill-desc" style="margin-top:0.4rem">${{c.interests}}</div>` : ''}}
      <div class="card-actions">
        <a class="secondary" href="/clients">Edit in Clients →</a>
        ${{d.entity_id ? `<a class="secondary" href="/lobbying/detail?id=${{d.entity_id}}">View CAL-ACCESS record →</a>` : ''}}
      </div>
    </div>
  `;
}}

function positionSelect(billId, position) {{
  const options = POSITIONS.map(([value, label]) =>
    `<option value="${{value}}" ${{position === value ? 'selected' : ''}}>${{label}}</option>`
  ).join('');
  return `<select class="position-select ${{position}}" onchange="setPosition(${{billId}}, this.value); this.className = 'position-select ' + this.value" style="font-size:0.78rem;padding:0.3rem 0.5rem;font-weight:600">${{options}}</select>`;
}}

function renderBills(bills) {{
  if (!bills.length) {{
    billsEl.innerHTML = '<p class="empty">No bills assigned to this client yet — add one above.</p>';
    return;
  }}
  billsEl.innerHTML = `
    <table>
      <thead><tr><th>Bill</th><th>Title</th><th>Status</th><th>Position</th><th></th></tr></thead>
      <tbody>
      ${{bills.map(b => `
        <tr>
          <td class="chamber">${{b.state}} ${{b.bill_number}}</td>
          <td>${{b.title || ''}}</td>
          <td>${{b.status_label ? `<span class="status-badge">${{b.status_label}}</span>` : ''}}</td>
          <td>${{positionSelect(b.bill_id, b.position || 'watch')}}</td>
          <td class="row-menu">
            <a class="secondary" href="/report?bill_id=${{b.bill_id}}" style="margin-right:0.4rem">Report</a>
            <button type="button" class="row-menu-btn" onclick="toggleRowMenu(event, 'bill-${{b.bill_id}}')" aria-label="More actions">
              <svg viewBox="0 0 14 14" fill="currentColor"><circle cx="7" cy="3" r="1.6"/><circle cx="7" cy="7" r="1.6"/><circle cx="7" cy="11" r="1.6"/></svg>
            </button>
            <div class="row-menu-dropdown" id="row-menu-bill-${{b.bill_id}}">
              <button type="button" onclick="removeBill(${{b.bill_id}})">Remove from client</button>
            </div>
          </td>
        </tr>
      `).join('')}}
      </tbody>
    </table>
  `;
}}

function toggleRowMenu(e, key) {{
  e.stopPropagation();
  const menu = document.getElementById(`row-menu-${{key}}`);
  const wasOpen = menu.classList.contains('show');
  document.querySelectorAll('.row-menu-dropdown.show').forEach(m => m.classList.remove('show'));
  if (!wasOpen) menu.classList.add('show');
}}
document.addEventListener('click', () => {{
  document.querySelectorAll('.row-menu-dropdown.show').forEach(m => m.classList.remove('show'));
}});

async function load() {{
  try {{
    const res = await fetch(`/api/clients/detail?id=${{clientId}}`);
    if (res.status === 401) {{ window.location.href = '/login'; return; }}
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not load client');
    renderClient(data);
    renderBills(data.bills);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

async function setPosition(billId, position) {{
  try {{
    const res = await fetch('/api/bill-clients', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ bill_id: billId, client_id: Number(clientId), position }}),
    }});
    if (!res.ok) {{
      const data = await res.json();
      throw new Error(data.error || 'Could not update position');
    }}
    load();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

async function removeBill(billId) {{
  try {{
    const res = await fetch(`/api/bill-clients?bill_id=${{billId}}&client_id=${{clientId}}`, {{ method: 'DELETE' }});
    if (!res.ok) {{
      const data = await res.json();
      throw new Error(data.error || 'Could not remove bill');
    }}
    load();
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

addBillForm.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const bill_number = document.getElementById('bill_number').value.trim();
  if (!bill_number) return;
  const position = document.getElementById('add-bill-position').value;
  errorEl.className = ''; addBillLoading.style.display = 'block';
  addBillForm.querySelector('button').disabled = true;
  try {{
    const res = await fetch('/api/client-bills', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ client_id: Number(clientId), bill_number, position }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not add bill');
    addBillForm.reset();
    renderBills(data.bills);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }} finally {{
    addBillLoading.style.display = 'none';
    addBillForm.querySelector('button').disabled = false;
  }}
}});

load();
</script>
"""

CLIENT_DETAIL_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Client — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{app_shell("/clients", CLIENT_DETAIL_BODY)}
</body>
</html>
"""


# Action report — everything about one bill in one place: current
# status, full status history, amendment history, upcoming hearings,
# and (if this signed-in user has assigned it to one of their own
# clients) that client's name and current position. Reached via
# ?bill_id=... — e.g. linked from a "Report" link on /flagged — rather
# than being a page anyone navigates to on its own.
REPORT_BODY = f"""
<div class="page-head"><div><a href="/flagged" class="sub">← Flagged bills</a></div></div>
<div id="error"></div>
<div id="report"></div>

<script>
const reportEl = document.getElementById('report');
const errorEl = document.getElementById('error');
const billId = new URLSearchParams(window.location.search).get('bill_id');
const POSITION_LABELS = {{ support: 'Support', oppose: 'Oppose', watch: 'Watch' }};

async function load() {{
  if (!billId) {{
    errorEl.textContent = 'Missing bill_id in the URL.';
    errorEl.className = 'show';
    return;
  }}
  try {{
    const res = await fetch(`/api/report?bill_id=${{billId}}`);
    if (res.status === 401) {{ window.location.href = '/login'; return; }}
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not load report');
    render(data);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

function render(r) {{
  const historyRows = (r.history || []).map(h => `
    <tr><td class="date">${{h.date || ''}}</td><td class="chamber">${{h.chamber || ''}}</td><td>${{h.action || ''}}</td></tr>
  `).join('');

  const amendmentRows = (r.amendments || []).map(a => `
    <tr>
      <td class="date">${{a.date || ''}}</td>
      <td class="chamber">${{a.chamber || ''}}</td>
      <td>${{a.title || a.description || ''}}${{a.adopted ? ' <span class="tag">Adopted</span>' : ''}}${{a.url ? ` — <a href="${{a.url}}" target="_blank" rel="noopener">view</a>` : ''}}</td>
    </tr>
  `).join('');

  const hearingRows = (r.upcoming_hearings || []).map(h => `
    <tr>
      <td class="date">${{h.date || ''}}${{h.time ? ' ' + h.time : ''}}</td>
      <td class="chamber">${{h.event_type || ''}}</td>
      <td>${{h.description || ''}}${{h.location ? ` — ${{h.location}}` : ''}}</td>
    </tr>
  `).join('');

  const clientBadges = (r.assigned_clients || []).map(c => {{
    const position = c.position || 'watch';
    return `
      <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.4rem">
        <a href="/clients/detail?id=${{c.id}}">${{c.name}}</a>
        <span class="position-badge ${{position}}">${{POSITION_LABELS[position] || position}}</span>
      </div>
    `;
  }}).join('');

  reportEl.innerHTML = `
    <div class="card">
      <div class="bill-id">${{r.state}} ${{r.bill_number}}</div>
      <div class="status-badge">${{r.status_label || 'Unknown'}}${{r.status_date ? ` — as of ${{r.status_date}}` : ''}}</div>
      <div class="bill-title">${{r.title || ''}}</div>
      ${{r.description ? `<div class="bill-desc">${{r.description}}</div>` : ''}}
      ${{r.url ? `<a class="bill-link" href="${{r.url}}" target="_blank" rel="noopener">View on LegiScan →</a>` : ''}}
    </div>

    <div class="panel" style="margin-top:1rem">
      <div class="panel-head"><div class="title">Assigned client${{(r.assigned_clients || []).length === 1 ? '' : 's'}}</div></div>
      <div style="padding:1rem 1.15rem">
        ${{clientBadges || '<p class="empty">Not currently assigned to any of your clients.</p>'}}
      </div>
    </div>

    <div class="panel" style="margin-top:1rem">
      <div class="panel-head"><div class="title">Status history</div></div>
      ${{historyRows
        ? `<table><thead><tr><th>Date</th><th>Chamber</th><th>Action</th></tr></thead><tbody>${{historyRows}}</tbody></table>`
        : '<p class="empty" style="padding:1rem 1.15rem">No status history recorded yet.</p>'}}
    </div>

    <div class="panel" style="margin-top:1rem">
      <div class="panel-head"><div class="title">Amendment history</div></div>
      ${{amendmentRows
        ? `<table><thead><tr><th>Date</th><th>Chamber</th><th>Amendment</th></tr></thead><tbody>${{amendmentRows}}</tbody></table>`
        : '<p class="empty" style="padding:1rem 1.15rem">No amendments recorded.</p>'}}
    </div>

    <div class="panel" style="margin-top:1rem">
      <div class="panel-head"><div class="title">Upcoming hearings</div></div>
      ${{hearingRows
        ? `<table><thead><tr><th>When</th><th>Type</th><th>Details</th></tr></thead><tbody>${{hearingRows}}</tbody></table>`
        : '<p class="empty" style="padding:1rem 1.15rem">No upcoming hearings scheduled.</p>'}}
    </div>
  `;
}}

load();
</script>
"""

REPORT_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Action Report — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{app_shell("/flagged", REPORT_BODY)}
</body>
</html>
"""


# "Prepare my disclosure form" — /disclosures (pick a form, generate a
# draft, see everything you've prepared before) and /disclosures/review
# (one draft: the actual filled PDF, known-gap notes, and the sign-off
# step). This app never files anything itself — see pdf_forms.py and
# db.sign_off_prepared_filing for where that boundary is enforced.
DISCLOSURES_BODY = f"""
<div class="page-head">
  <div>
    <h1>Disclosure Forms</h1>
    <p class="sub">Prepare a real FPPC disclosure form, pre-filled from your profile and clients. This app never files anything on your behalf — it only prepares the document for you to review, sign off on, and file yourself.</p>
  </div>
</div>

<div class="stat-grid" id="stats"></div>

<div class="card">
  <form id="f">
    <label style="flex:1 1 100%">
      <div class="sub" style="margin:0 0 0.3rem">Which form do you need?</div>
      <select id="form_type" style="width:100%">
        <option value="601">Form 601 — Lobbying Firm Registration Statement</option>
        <option value="" disabled>More forms coming later</option>
      </select>
    </label>
    <label id="period_row" style="flex:1 1 100%;display:none">
      <div class="sub" style="margin:1rem 0 0.3rem">Reporting period</div>
      <input id="period_label" style="width:100%">
    </label>
    <div id="period_note" class="sub" style="flex:1 1 100%;margin:0.6rem 0 0">
      Form 601 doesn't use a reporting period — it's tied to the current two-year legislative session, filled in automatically.
    </div>
    <button type="submit" style="margin-top:1rem">Generate draft →</button>
  </form>
</div>

<div id="loading">Generating…</div>
<div id="error"></div>

<div class="panel" style="margin-top:1rem">
  <div class="panel-head"><div class="title">Your prepared filings</div></div>
  <div id="list"></div>
</div>

<script>
// Only 601 exists today, but this stays keyed by form_type so adding a
// form that DOES need a period later is just one more entry here.
const FORM_META = {{
  "601": {{ label: "Form 601 — Lobbying Firm Registration Statement", requiresPeriod: false }},
}};

const form = document.getElementById('f');
const formType = document.getElementById('form_type');
const periodRow = document.getElementById('period_row');
const periodNote = document.getElementById('period_note');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');
const listEl = document.getElementById('list');
const statsEl = document.getElementById('stats');

const ICON_DISCLOSURE = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="1.5" width="8" height="11" rx="1"/><path d="M5.2 6l1 1 2.2-2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_ALERT = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="5.5"/><path d="M7 4.5v3" stroke-linecap="round"/><circle cx="7" cy="9.8" r="0.6" fill="currentColor" stroke="none"/></svg>';
const ICON_GOOD = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="5.5"/><path d="M4.5 7l1.7 1.7L9.7 5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function renderStats(rows) {{
  if (!rows.length) {{ statsEl.innerHTML = ''; return; }}
  const pending = rows.filter(r => r.status === 'draft').length;
  const ready = rows.filter(r => r.status === 'ready_to_file').length;
  statsEl.innerHTML = `
    <div class="stat-card">
      <div class="stat-icon">${{ICON_DISCLOSURE}}</div>
      <div class="stat-label">Prepared filings</div>
      <div class="stat-value">${{rows.length}}</div>
      <div class="stat-foot">All time</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">${{ICON_ALERT}}</div>
      <div class="stat-label">Awaiting sign-off</div>
      <div class="stat-value">${{pending}}</div>
      <div class="stat-foot">${{pending ? 'Review and confirm when ready' : 'Nothing waiting on you'}}</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">${{ICON_GOOD}}</div>
      <div class="stat-label">Ready to file</div>
      <div class="stat-value">${{ready}}</div>
      <div class="stat-foot">Signed off, yours to file</div>
    </div>
  `;
}}

function syncPeriodField() {{
  const meta = FORM_META[formType.value];
  const needsPeriod = !!(meta && meta.requiresPeriod);
  periodRow.style.display = needsPeriod ? 'block' : 'none';
  periodNote.style.display = needsPeriod ? 'none' : 'block';
}}
formType.addEventListener('change', syncPeriodField);
syncPeriodField();

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  errorEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button').disabled = true;
  try {{
    const meta = FORM_META[formType.value];
    const res = await fetch('/api/prepared-filings', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        form_type: formType.value,
        period_label: (meta && meta.requiresPeriod) ? document.getElementById('period_label').value.trim() : null,
      }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not generate draft');
    window.location.href = `/disclosures/review?id=${{data.id}}`;
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }} finally {{
    loadingEl.className = '';
    form.querySelector('button').disabled = false;
  }}
}});

async function load() {{
  try {{
    const res = await fetch('/api/prepared-filings');
    if (res.status === 401) {{ window.location.href = '/login'; return; }}
    const rows = await res.json();
    renderStats(rows);
    render(rows);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

function render(rows) {{
  if (!rows.length) {{
    listEl.innerHTML = '<p class="empty">Nothing prepared yet — use the form above.</p>';
    return;
  }}
  listEl.innerHTML = `
    <table>
      <thead><tr><th>Form</th><th>Period</th><th>Status</th><th>Created</th><th></th></tr></thead>
      <tbody>
      ${{rows.map(r => {{
        const meta = FORM_META[r.form_type];
        const statusBadge = r.status === 'ready_to_file'
          ? '<span class="position-badge support">Ready to file</span>'
          : '<span class="position-badge watch">Draft</span>';
        return `
          <tr>
            <td>${{(meta && meta.label) || ('Form ' + r.form_type)}}</td>
            <td>${{r.period_label || '—'}}</td>
            <td>${{statusBadge}}</td>
            <td class="date">${{(r.created_at || '').slice(0, 16)}}</td>
            <td><a class="secondary" href="/disclosures/review?id=${{r.id}}">Review</a></td>
          </tr>
        `;
      }}).join('')}}
      </tbody>
    </table>
  `;
}}

load();
</script>
"""

DISCLOSURES_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Disclosure Forms — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{app_shell("/disclosures", DISCLOSURES_BODY)}
</body>
</html>
"""


DISCLOSURE_REVIEW_BODY = f"""
<div class="page-head"><div><a href="/disclosures" class="sub">← Disclosures</a></div></div>
<div id="error"></div>
<div id="content"></div>

<script>
const errorEl = document.getElementById('error');
const contentEl = document.getElementById('content');
const filingId = new URLSearchParams(window.location.search).get('id');
const FORM_LABELS = {{ "601": "Form 601 — Lobbying Firm Registration Statement" }};

async function load() {{
  if (!filingId) {{
    errorEl.textContent = 'Missing filing id in the URL.';
    errorEl.className = 'show';
    return;
  }}
  try {{
    const res = await fetch(`/api/prepared-filings?id=${{filingId}}`);
    if (res.status === 401) {{ window.location.href = '/login'; return; }}
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not load this filing');
    render(data);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

async function signOff(e) {{
  e.preventDefault();
  const signedName = document.getElementById('signed_name').value.trim();
  const confirmed = document.getElementById('confirmed_accurate').checked;
  const btn = document.getElementById('sign-btn');
  btn.disabled = true;
  try {{
    const res = await fetch('/api/prepared-filings/sign', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ id: Number(filingId), signed_name: signedName, confirmed_accurate: confirmed }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not record sign-off');
    render(data);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
    btn.disabled = false;
  }}
}}

function render(r) {{
  const pdfUrl = `/api/prepared-filings/pdf?id=${{r.id}}`;
  const ready = r.status === 'ready_to_file';
  const statusBadge = ready
    ? '<span class="position-badge support">Ready to file</span>'
    : '<span class="position-badge watch">Draft — not yet signed off</span>';

  const signOffSection = ready ? `
    <div class="card">
      <div class="bill-title" style="margin-bottom:0.4rem">Signed off</div>
      <div class="bill-desc">Confirmed accurate by <strong>${{r.signed_name}}</strong> on ${{(r.signed_at || '').replace('T', ' ').slice(0, 16)}}.</div>
      <div class="sub" style="margin:0.6rem 0 0">This app has not filed anything. Download the PDF above and file it yourself with the FPPC / Secretary of State.</div>
    </div>
  ` : `
    <div class="card">
      <div class="bill-title" style="margin-bottom:0.4rem">Sign-off — required before this can be marked ready to file</div>
      <form id="sign-form" onsubmit="signOff(event)">
        <label style="flex:1 1 100%">
          <div class="sub" style="margin:0 0 0.3rem">Type your full legal name to confirm</div>
          <input id="signed_name" required style="width:100%">
        </label>
        <label style="flex:1 1 100%;display:flex;align-items:flex-start;gap:0.5rem;margin-top:0.8rem;font-size:0.9rem">
          <input type="checkbox" id="confirmed_accurate" required style="margin-top:0.2rem;width:auto">
          <span>I confirm the information in this form is accurate to the best of my knowledge.</span>
        </label>
        <button type="submit" id="sign-btn" style="margin-top:1rem">Mark ready to file</button>
      </form>
      <div class="sub" style="margin-top:0.8rem">This only marks the draft reviewed on your end — it does not submit or send anything anywhere.</div>
    </div>
  `;

  contentEl.innerHTML = `
    <h1>${{FORM_LABELS[r.form_type] || ('Form ' + r.form_type)}}</h1>
    <p class="sub">${{r.period_label ? 'Period: ' + r.period_label + ' — ' : ''}}Prepared ${{(r.created_at || '').slice(0, 16)}}.</p>
    <div style="margin-bottom:1rem">${{statusBadge}}</div>

    <div class="card">
      <div class="bill-title" style="margin-bottom:0.4rem">Known gaps in this draft</div>
      <div class="bill-desc">
        This app doesn't collect every field the real form asks for. Left blank on purpose, rather than guessed:
        subcontracted-client information, and any individual lobbyists beyond your own name.
        Fill those in by hand before filing if they apply to you. Per-client effective date, period of
        contract, and agencies lobbied are pulled in automatically when set on the client — add them from
        <a href="/clients">Clients</a> if a row below looks empty.
      </div>
    </div>

    <div class="card" style="padding:0;overflow:hidden">
      <iframe src="${{pdfUrl}}" style="width:100%;height:70vh;border:none" title="Filled ${{r.form_type}} preview"></iframe>
    </div>
    <div class="card-actions" style="margin:-0.5rem 0 1.5rem">
      <a class="secondary" href="${{pdfUrl}}" target="_blank" rel="noopener">Open PDF in a new tab →</a>
    </div>

    ${{signOffSection}}
  `;
}}

load();
</script>
"""

DISCLOSURE_REVIEW_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review Disclosure Form — Rotunda</title>
<style>{STYLE}</style>
</head>
<body>
{app_shell("/disclosures", DISCLOSURE_REVIEW_BODY)}
</body>
</html>
"""


def search_lobbying(conn, q):
    """Matches BOTH the registered-entity name and the free-text
    client_name on disclosures — see the module docstring for why the
    second half matters (a client doesn't need its own registration to
    be named in someone else's filing)."""
    like = f"%{q}%"
    entities = conn.execute(
        "SELECT id, name, entity_type, city, state, registration_status "
        "FROM lobbying_entities WHERE name LIKE ? ORDER BY name LIMIT 40",
        (like,),
    ).fetchall()
    seen_lower = {(r["name"] or "").lower() for r in entities}

    results = [
        {
            "kind": "entity", "id": r["id"], "name": r["name"], "entity_type": r["entity_type"],
            "city": r["city"], "state": r["state"], "registration_status": r["registration_status"],
        }
        for r in entities
    ]

    client_rows = conn.execute(
        "SELECT DISTINCT client_name FROM lobbying_disclosures WHERE client_name LIKE ? LIMIT 40",
        (like,),
    ).fetchall()
    for row in client_rows:
        name = row["client_name"]
        if not name or name.lower() in seen_lower:
            continue
        seen_lower.add(name.lower())
        # Never independently registered, so there's no address/status to
        # show — which made near-identical names (several "Amazon"
        # variants that only ever show up as free text on someone else's
        # filing) impossible to tell apart. How often, and how recently,
        # this exact name was mentioned is real distinguishing context in
        # its place.
        context = conn.execute(
            "SELECT COUNT(*) AS n, MAX(filed_date) AS latest FROM lobbying_disclosures WHERE client_name = ?",
            (name,),
        ).fetchone()
        results.append({
            "kind": "client", "id": None, "name": name, "entity_type": None,
            "city": None, "state": None, "registration_status": None,
            "mention_count": context["n"], "latest_filed": context["latest"],
        })

    results.sort(key=lambda r: (r["name"] or "").lower())
    return results[:50]


def lobbying_detail(conn, entity_id, name):
    """Returns entity info (if this is a registered entity) plus every
    disclosure line this name is involved in, either as the filer or as
    the free-text 'other party' (client_name) — that second half is what
    surfaces the ~35% of clients that only ever appear as free text on
    someone else's filing, never independently registered.

    The tricky part, found by testing against real data rather than
    assumed from the column name: `client_name` does NOT always mean
    "the client" — it's LPAY_CD's EMPLR_NAML field, reused across form
    types for whichever party isn't the filer. On a firm's Form 625P2,
    that's genuinely the paying client. On an EMPLOYER's Form 635P3B,
    the filer already IS the employer/client, and the same field holds
    the FIRM they paid instead — e.g. Meta Platforms' own 635P3B filings
    list "Axiom Advisors" as client_name, meaning Meta paid Axiom, not
    the reverse. So every row is resolved to real (firm, client) pairs
    based on form_type, rather than assuming client_name always means
    "the client" — labeling a firm as Meta's "client" would have been
    backwards.
    """
    entity = None
    if entity_id:
        row = conn.execute(
            "SELECT id, name, entity_type, filer_id, address, city, state, zip, registration_status, source_form "
            "FROM lobbying_entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row:
            entity = dict(row)
            name = row["name"]

    raw_rows = []
    if entity_id:
        raw_rows += conn.execute(
            "SELECT d.form_type, d.period_start, d.period_end, d.amount_spent, d.raw_bill_text, "
            "d.filed_date, e.name AS filer_name, d.client_name AS other_party "
            "FROM lobbying_disclosures d JOIN lobbying_entities e ON e.id = d.filer_entity_id "
            "WHERE d.filer_entity_id = ? ORDER BY d.filed_date DESC LIMIT 100",
            (entity_id,),
        ).fetchall()
    if name:
        raw_rows += conn.execute(
            "SELECT d.form_type, d.period_start, d.period_end, d.amount_spent, d.raw_bill_text, "
            "d.filed_date, e.name AS filer_name, d.client_name AS other_party "
            "FROM lobbying_disclosures d JOIN lobbying_entities e ON e.id = d.filer_entity_id "
            "WHERE d.client_name = ? ORDER BY d.filed_date DESC LIMIT 100",
            (name,),
        ).fetchall()

    relationships = []
    for r in raw_rows:
        row = dict(r)
        # F625P2 = filed by the FIRM (other_party is who paid them).
        # F635P3B = filed by the EMPLOYER (other_party is who THEY paid).
        # Anything else: no reliable convention confirmed, so don't guess
        # a direction — show the filer as "firm" rather than mislabel.
        if row["form_type"] == "F635P3B":
            firm, client = row["other_party"], row["filer_name"]
        else:
            firm, client = row["filer_name"], row["other_party"]
        relationships.append({
            "firm": firm, "client": client, "form_type": row["form_type"],
            "period_start": row["period_start"], "period_end": row["period_end"],
            "amount_spent": row["amount_spent"], "raw_bill_text": row["raw_bill_text"],
            "filed_date": row["filed_date"],
        })
    relationships.sort(key=lambda r: r.get("filed_date") or "", reverse=True)

    return {"entity": entity, "name": name, "relationships": relationships}


def _trigger_refresh(job_name, target_fn):
    """Starts target_fn() in a background thread unless that job is
    already running. Returns False (caller should respond 409) if a run
    is already in flight, True once a new one has been started."""
    with _refresh_lock:
        if _refresh_running[job_name]:
            return False
        _refresh_running[job_name] = True

    def run():
        try:
            result = target_fn()
            _last_refresh[job_name] = {"at": datetime.datetime.now().isoformat(), "crashed": False, "result": result}
        except Exception as e:
            # Each job's own log() already records its own failures in
            # detail (see refresh_one/sync_disclosures etc.) — this is
            # just a backstop for anything that escapes those, e.g. a
            # crash before that job's own logging even starts.
            print(f"[{job_name} refresh] crashed: {e}")
            _last_refresh[job_name] = {"at": datetime.datetime.now().isoformat(), "crashed": True, "error": str(e)}
        finally:
            with _refresh_lock:
                _refresh_running[job_name] = False

    threading.Thread(target=run, daemon=True, name=f"refresh-{job_name}").start()
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def _send_json(self, status, payload, set_cookie=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status, html, set_cookie=None):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status, content_type, body, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _session_cookie_header(self, token, clear=False):
        """clear=True builds an immediately-expiring cookie, for logout.
        Secure is only set when the request actually arrived over HTTPS
        (Render terminates TLS and forwards plain HTTP internally, so
        this checks the standard X-Forwarded-Proto header rather than
        self.request_version) — a Secure cookie set while testing over
        plain http://127.0.0.1 would just get silently dropped."""
        is_https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        parts = [
            f"{accounts.SESSION_COOKIE}={'' if clear else token}",
            "Path=/", "HttpOnly", "SameSite=Lax",
        ]
        if clear:
            parts.append("Max-Age=0")
        if is_https:
            parts.append("Secure")
        return "; ".join(parts)

    def _current_user_id(self, conn):
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(accounts.SESSION_COOKIE)
        if not morsel:
            return None
        return accounts.user_id_for_session(conn, morsel.value)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/internal/status":
            # Same secret-gate as the two POST refresh routes, not the
            # human Basic Auth login — a plain-JSON read of what's
            # actually in the database and whether a refresh is running,
            # so "did the last refresh actually work" doesn't depend on
            # catching print() output in Render's log stream (which, per
            # a real incident, can sit in a stdout buffer indefinitely
            # inside a long-lived process — see refresh_calaccess.log()).
            if not self._authorized_for_refresh():
                self.send_response(404)
                self.end_headers()
                return
            conn = db.get_connection()
            try:
                counts = {
                    "bills": conn.execute("SELECT COUNT(*) AS n FROM bills").fetchone()["n"],
                    "watchlist": conn.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"],
                    "lobbying_entities": conn.execute("SELECT COUNT(*) AS n FROM lobbying_entities").fetchone()["n"],
                    "lobbying_disclosures": conn.execute("SELECT COUNT(*) AS n FROM lobbying_disclosures").fetchone()["n"],
                }
            finally:
                conn.close()
            self._send_json(
                200,
                {
                    "counts": counts,
                    "refresh_running": dict(_refresh_running),
                    # Direct, unambiguous answer to "is the digest email
                    # actually able to send" — checking this beats inferring
                    # it from refresh.log's "digest: N sent, N not sent..."
                    # line, which reads all-zero both when SMTP genuinely
                    # isn't configured AND when a refresh simply found no
                    # bill changes to send about (send_all_digests returns
                    # early in that case, before ever checking SMTP at all).
                    "smtp_configured": mailer.is_configured(),
                    # Timestamp + outcome of the last refresh of each job —
                    # None until the first one runs after a restart. Same
                    # motivation as smtp_configured above: a direct answer
                    # instead of having to dig through Render's log stream.
                    "last_refresh": dict(_last_refresh),
                },
            )
            return

        qs = parse_qs(parsed.query)

        if parsed.path == "/":
            self._send_html(200, LANDING_PAGE)
            return

        if parsed.path == "/lookup":
            self._send_html(200, PAGE)
            return

        if parsed.path == "/watchlist":
            # Retired — consolidated into personal flagged bills (see
            # the module docstring). A bare 404 would be a jarring dead
            # end for anyone with this bookmarked, so redirect somewhere
            # that actually still exists.
            conn = db.get_connection()
            try:
                logged_in = bool(self._current_user_id(conn))
            finally:
                conn.close()
            self.send_response(302)
            self.send_header("Location", "/flagged" if logged_in else "/login")
            self.end_headers()
            return

        if parsed.path == "/lobbying":
            self._send_html(200, LOBBYING_PAGE)
            return

        if parsed.path == "/lobbying/detail":
            self._send_html(200, LOBBYING_DETAIL_PAGE)
            return

        if parsed.path == "/signup":
            self._send_html(200, SIGNUP_PAGE)
            return

        if parsed.path == "/login":
            self._send_html(200, LOGIN_PAGE)
            return

        if parsed.path == "/signup/profile":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
            finally:
                conn.close()
            if not user_id:
                # No active session — most likely someone bookmarked this
                # or came back later without logging in. Send them to
                # sign up rather than show a form with nothing to save
                # against.
                self.send_response(302)
                self.send_header("Location", "/signup")
                self.end_headers()
                return
            self._send_html(200, PROFILE_PAGE)
            return

        if parsed.path == "/profile":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
            finally:
                conn.close()
            if not user_id:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._send_html(200, PROFILE_VIEW_PAGE)
            return

        if parsed.path == "/flagged":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
            finally:
                conn.close()
            if not user_id:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._send_html(200, FLAGGED_PAGE)
            return

        if parsed.path == "/api/flagged":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Not logged in."})
                    return
                self._send_json(200, db.list_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/clients":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
            finally:
                conn.close()
            if not user_id:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._send_html(200, CLIENTS_PAGE)
            return

        if parsed.path == "/api/clients":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Not logged in."})
                    return
                self._send_json(200, db.list_clients(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/clients/detail":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
            finally:
                conn.close()
            if not user_id:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._send_html(200, CLIENT_DETAIL_PAGE)
            return

        if parsed.path == "/api/clients/detail":
            client_id = (qs.get("id") or [""])[0]
            if not client_id:
                self._send_json(400, {"error": "Missing id parameter."})
                return
            try:
                client_id = int(client_id)
            except ValueError:
                self._send_json(400, {"error": "id must be a number."})
                return
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Not logged in."})
                    return
                client = db.get_client(conn, user_id, client_id)
                if not client:
                    self._send_json(404, {"error": "No client found with that ID."})
                    return
                bills = db.get_client_bills(conn, user_id, client_id)
                # If this client has a CAL-ACCESS filer ID on file, resolve
                # it to that entity's own id — completes the connection the
                # other direction (Organization Search -> Client, added
                # above) by letting the client page link back to its own
                # organization's lobbying detail, instead of the two
                # staying siblings that never reference each other.
                entity_id = None
                if client.get("existing_filer_id"):
                    row = conn.execute(
                        "SELECT id FROM lobbying_entities WHERE filer_id = ?",
                        (client["existing_filer_id"],),
                    ).fetchone()
                    entity_id = row["id"] if row else None
                self._send_json(200, {"client": client, "bills": bills, "entity_id": entity_id})
            finally:
                conn.close()
            return

        if parsed.path == "/report":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
            finally:
                conn.close()
            if not user_id:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._send_html(200, REPORT_PAGE)
            return

        if parsed.path == "/api/report":
            bill_id = (qs.get("bill_id") or [""])[0]
            if not bill_id:
                self._send_json(400, {"error": "Missing bill_id parameter."})
                return
            try:
                # Cast now, not left as the raw query-string value —
                # get_bill_report keys its client-assignment lookup off
                # bill_id values that come back from SQLite as integers,
                # and a str/int mismatch there silently returns "no
                # client assigned" even when one is. Found by testing
                # this against a real assigned bill, not assumed.
                bill_id = int(bill_id)
            except ValueError:
                self._send_json(400, {"error": "bill_id must be a number."})
                return
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Not logged in."})
                    return
                report = db.get_bill_report(conn, user_id, bill_id)
                if not report:
                    self._send_json(404, {"error": "No bill found with that ID."})
                    return
                self._send_json(200, report)
            finally:
                conn.close()
            return

        if parsed.path == "/disclosures":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
            finally:
                conn.close()
            if not user_id:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._send_html(200, DISCLOSURES_PAGE)
            return

        if parsed.path == "/disclosures/review":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
            finally:
                conn.close()
            if not user_id:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._send_html(200, DISCLOSURE_REVIEW_PAGE)
            return

        if parsed.path == "/api/prepared-filings":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Not logged in."})
                    return
                filing_id = (qs.get("id") or [""])[0]
                if filing_id:
                    try:
                        filing_id = int(filing_id)
                    except ValueError:
                        self._send_json(400, {"error": "id must be a number."})
                        return
                    filing = db.get_prepared_filing(conn, user_id, filing_id)
                    if not filing:
                        self._send_json(404, {"error": "No prepared filing found with that id."})
                        return
                    self._send_json(200, filing)
                else:
                    self._send_json(200, db.list_prepared_filings(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/prepared-filings/pdf":
            filing_id = (qs.get("id") or [""])[0]
            try:
                filing_id = int(filing_id)
            except ValueError:
                self._send_json(400, {"error": "Missing or invalid id parameter."})
                return
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Not logged in."})
                    return
                filing = db.get_prepared_filing(conn, user_id, filing_id)
            finally:
                conn.close()
            if not filing:
                self._send_json(404, {"error": "No prepared filing found with that id."})
                return
            try:
                pdf_bytes = pdf_forms.render_prepared_filing(filing)
            except Exception as e:
                self._send_json(500, {"error": f"Could not render PDF: {e}"})
                return
            self._send_bytes(200, "application/pdf", pdf_bytes, filename=f"form_{filing['form_type']}.pdf")
            return

        if parsed.path == "/api/me":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(200, {"logged_in": False})
                    return
                row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
            finally:
                conn.close()
            self._send_json(200, {"logged_in": True, "email": row["email"] if row else None})
            return

        if parsed.path == "/api/profile":
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Not logged in."})
                    return
                self._send_json(200, {"profile": accounts.get_profile(conn, user_id)})
            finally:
                conn.close()
            return

        if parsed.path == "/api/lobbying/search":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                self._send_json(400, {"error": "Missing q parameter."})
                return
            conn = db.get_connection()
            try:
                self._send_json(200, search_lobbying(conn, q))
            finally:
                conn.close()
            return

        if parsed.path == "/api/lobbying/detail":
            entity_id = (qs.get("id") or [""])[0]
            name = (qs.get("name") or [""])[0].strip()
            if not entity_id and not name:
                self._send_json(400, {"error": "Missing id or name parameter."})
                return
            conn = db.get_connection()
            try:
                self._send_json(200, lobbying_detail(conn, entity_id, name))
            finally:
                conn.close()
            return

        if parsed.path == "/api/bill":
            bill = (qs.get("bill") or [""])[0]
            if not bill:
                self._send_json(400, {"error": "Missing bill parameter."})
                return
            try:
                data = lookup_bill(bill)
                self._send_json(200, data)
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        self.send_response(404)
        self.end_headers()

    def _authorized_for_refresh(self):
        """These routes are hit by a cron job with no browser and no
        individual account, gated on their own secret instead — and if
        REFRESH_SECRET was never set (the local/default case), the routes
        don't exist at all, same as any other unrecognized path."""
        if not REFRESH_SECRET:
            return False
        supplied = self.headers.get("X-Refresh-Secret", "")
        return hmac.compare_digest(supplied, REFRESH_SECRET)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/internal/refresh-watchlist", "/internal/refresh-calaccess"):
            if not self._authorized_for_refresh():
                self.send_response(404)  # not 401 — don't reveal the route exists
                self.end_headers()
                return
            job = "watchlist" if parsed.path == "/internal/refresh-watchlist" else "calaccess"
            target = refresh_watchlist.main if job == "watchlist" else refresh_calaccess.main
            if _trigger_refresh(job, target):
                self._send_json(202, {"status": f"{job} refresh started"})
            else:
                self._send_json(409, {"status": f"{job} refresh already running"})
            return

        if parsed.path == "/api/signup":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            conn = db.get_connection()
            try:
                try:
                    user_id = accounts.create_user(conn, body.get("email"), body.get("password"))
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                token = accounts.create_session(conn, user_id)
                self._send_json(200, {"status": "created"}, set_cookie=self._session_cookie_header(token))
            finally:
                conn.close()
            return

        if parsed.path == "/api/login":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            email = (body.get("email") or "").strip().lower()
            if _login_locked_out(email):
                self._send_json(
                    429,
                    {"error": "Too many failed attempts. Try again in a few minutes."},
                )
                return
            conn = db.get_connection()
            try:
                user_id = accounts.verify_login(conn, email, body.get("password"))
                if not user_id:
                    _record_login_failure(email)
                    self._send_json(401, {"error": "Incorrect email or password."})
                    return
                _clear_login_failures(email)
                token = accounts.create_session(conn, user_id)
                self._send_json(200, {"status": "logged in"}, set_cookie=self._session_cookie_header(token))
            finally:
                conn.close()
            return

        if parsed.path == "/api/logout":
            jar = SimpleCookie()
            jar.load(self.headers.get("Cookie") or "")
            morsel = jar.get(accounts.SESSION_COOKIE)
            if morsel:
                conn = db.get_connection()
                try:
                    accounts.destroy_session(conn, morsel.value)
                finally:
                    conn.close()
            self._send_json(200, {"status": "logged out"}, set_cookie=self._session_cookie_header(None, clear=True))
            return

        if parsed.path == "/api/profile":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "You need to sign up or log in first."})
                    return
                if not (body.get("legal_name") or "").strip():
                    self._send_json(400, {"error": "Legal name is required."})
                    return
                if body.get("registrant_type") not in ("individual", "firm"):
                    self._send_json(400, {"error": "Choose individual or firm."})
                    return
                accounts.save_profile(conn, user_id, body)
                self._send_json(200, {"status": "saved"})
            finally:
                conn.close()
            return

        if parsed.path == "/api/flag":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            bill_id = body.get("bill_id")
            if not bill_id:
                self._send_json(400, {"error": "Missing bill_id."})
                return

            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to flag bills."})
                    return
                try:
                    # Same "re-fetch fresh rather than trust the client"
                    # pattern as /api/watchlist — see that route.
                    bill = get_bill_detail(bill_id)
                except Exception as e:
                    self._send_json(502, {"error": str(e)})
                    return
                db.upsert_bill(conn, bill)
                db.flag_bill(conn, user_id, bill_id)
                conn.commit()
                self._send_json(200, {"status": "flagged"})
            finally:
                conn.close()
            return

        if parsed.path == "/api/clients":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            if not (body.get("name") or "").strip():
                self._send_json(400, {"error": "Client / employer name is required."})
                return

            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to add clients."})
                    return
                client_id = body.get("id")
                if client_id:
                    db.update_client(conn, user_id, client_id, body)
                else:
                    db.create_client(conn, user_id, body)
                conn.commit()
                self._send_json(200, db.list_clients(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/bill-clients":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            bill_id, client_id = body.get("bill_id"), body.get("client_id")
            if not bill_id or not client_id:
                self._send_json(400, {"error": "Missing bill_id or client_id."})
                return

            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to assign clients."})
                    return
                position = body.get("position") or "watch"
                try:
                    db.link_bill_to_client(conn, user_id, bill_id, client_id, position)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, db.list_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/client-bills":
            # The reverse of the existing flag-then-assign flow: add a
            # bill starting from a client's own page (search LegiScan by
            # number, flag it, and link it to this client's position) in
            # one request, instead of three separate trips through
            # /flagged. Same three underlying operations either way —
            # this just does them together for this specific entry point.
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            client_id, bill_number = body.get("client_id"), body.get("bill_number")
            if not client_id or not bill_number:
                self._send_json(400, {"error": "Missing client_id or bill_number."})
                return
            try:
                client_id = int(client_id)
            except (ValueError, TypeError):
                self._send_json(400, {"error": "client_id must be a number."})
                return

            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to add bills to a client."})
                    return
                if not db.get_client(conn, user_id, client_id):
                    self._send_json(404, {"error": "No client found with that ID."})
                    return
                try:
                    bill = lookup_bill(bill_number)
                except Exception as e:
                    self._send_json(502, {"error": str(e)})
                    return
                db.upsert_bill(conn, bill)
                db.flag_bill(conn, user_id, bill["id"])
                position = body.get("position") or "watch"
                try:
                    db.link_bill_to_client(conn, user_id, bill["id"], client_id, position)
                except ValueError as e:
                    conn.commit()  # keep the flag even if the position was invalid
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, {
                    "client": db.get_client(conn, user_id, client_id),
                    "bills": db.get_client_bills(conn, user_id, client_id),
                })
            finally:
                conn.close()
            return

        if parsed.path == "/api/prepared-filings":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            form_type = body.get("form_type")
            if form_type not in pdf_forms.TEMPLATES_BY_FORM_TYPE:
                self._send_json(400, {"error": "Unknown or unsupported form type."})
                return

            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to prepare a disclosure form."})
                    return
                profile = accounts.get_profile(conn, user_id)
                if not profile:
                    self._send_json(400, {
                        "error": "Complete your registration profile before preparing a disclosure form.",
                    })
                    return
                user_row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
                clients = db.list_clients(conn, user_id)

                if form_type == "601":
                    field_data = pdf_forms.values_for_form_601(
                        profile, clients, user_row["email"], sign_off=None, today=datetime.date.today()
                    )

                filing_id = db.create_prepared_filing(conn, user_id, form_type, body.get("period_label"), field_data)
                conn.commit()
                self._send_json(200, db.get_prepared_filing(conn, user_id, filing_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/prepared-filings/sign":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            filing_id = body.get("id")
            if not filing_id:
                self._send_json(400, {"error": "Missing id."})
                return

            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to sign off on a filing."})
                    return
                try:
                    filing = db.sign_off_prepared_filing(
                        conn, user_id, filing_id, body.get("signed_name"), bool(body.get("confirmed_accurate")),
                    )
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, filing)
            finally:
                conn.close()
            return

        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/api/flag":
            bill_id = (qs.get("bill_id") or [""])[0]
            if not bill_id:
                self._send_json(400, {"error": "Missing bill_id parameter."})
                return
            try:
                # Cast now, not left as the raw query-string value — same
                # str/int mismatch class /api/report was bitten by (see
                # that route's comment). Working today only because
                # SQLite's implicit type affinity happens to coerce the
                # comparison; an explicit cast doesn't depend on that.
                bill_id = int(bill_id)
            except ValueError:
                self._send_json(400, {"error": "bill_id must be a number."})
                return
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to manage flagged bills."})
                    return
                db.unflag_bill(conn, user_id, bill_id)
                conn.commit()
                self._send_json(200, db.list_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/clients":
            client_id = (qs.get("id") or [""])[0]
            if not client_id:
                self._send_json(400, {"error": "Missing id parameter."})
                return
            try:
                client_id = int(client_id)
            except ValueError:
                self._send_json(400, {"error": "id must be a number."})
                return
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to manage clients."})
                    return
                db.delete_client(conn, user_id, client_id)
                conn.commit()
                self._send_json(200, db.list_clients(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/bill-clients":
            bill_id = (qs.get("bill_id") or [""])[0]
            client_id = (qs.get("client_id") or [""])[0]
            if not bill_id or not client_id:
                self._send_json(400, {"error": "Missing bill_id or client_id parameter."})
                return
            try:
                bill_id = int(bill_id)
                client_id = int(client_id)
            except ValueError:
                self._send_json(400, {"error": "bill_id and client_id must be numbers."})
                return
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if not user_id:
                    self._send_json(401, {"error": "Sign in to manage client assignments."})
                    return
                db.unlink_bill_from_client(conn, user_id, bill_id, client_id)
                conn.commit()
                self._send_json(200, db.list_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        self.send_response(404)
        self.end_headers()


def main():
    db.init_db()

    if not get_api_key():
        print("⚠️  No LEGISCAN_API_KEY found in your environment or ~/.zshrc.")
        print("    Set it with: export LEGISCAN_API_KEY=your_key_here")
        print("    (the app will still start, but lookups will fail until it's set)\n")

    is_hosted = bool(os.environ.get("RENDER") or os.environ.get("PORT_ASSIGNED_BY_HOST"))

    # ThreadingHTTPServer, not HTTPServer — a plain HTTPServer handles one
    # request at a time, so an /internal/refresh-calaccess trigger firing
    # off a multi-minute background job wouldn't itself block (that part
    # runs in its own thread already), but every OTHER visitor hitting the
    # site while that request is even being accepted would queue behind
    # it. Threading it costs nothing for the low request volume this app
    # actually sees.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}" if not is_hosted else f"port {PORT}"
    print(f"Rotunda running on {url}  (Ctrl+C to stop)")
    if not is_hosted:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
