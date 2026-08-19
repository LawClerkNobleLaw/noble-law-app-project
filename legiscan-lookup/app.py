#!/usr/bin/env python3
"""
Bill Search — a small local web app.

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
    --ink: #1c2333; --paper: #f4f5f2; --surface: #ffffff;
    --slate: #5a6272; --rule: #dcded3; --accent: #2f5d8a;
    --accent-soft: #e4ecf3; --good: #2e6b45; --good-soft: #dcebe0;
    --error: #a3372c; --error-soft: #f6e2df;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink: #eae7dd; --paper: #14171c; --surface: #1a1e25;
      --slate: #9ca3b3; --rule: #2b2f38; --accent: #6ea3d6;
      --accent-soft: #1c2d3c; --good: #78c091; --good-soft: #1d3324;
      --error: #d9847a; --error-soft: #3a2320;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.5rem 4rem; }
  .top-nav { display: flex; gap: 1.1rem; align-items: baseline; margin-bottom: 0.25rem; }
  .top-nav a { color: var(--accent); font-size: 0.85rem; text-decoration: none; }
  .top-nav a:hover { text-decoration: underline; }
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
    border-radius: 8px; background: var(--accent); color: white; cursor: pointer;
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
  a.secondary:hover, a.danger:hover { opacity: 0.9; }
  #result { display: none; }
  #result.show { display: block; }
  .card {
    background: var(--surface); border: 1px solid var(--rule);
    border-radius: 10px; padding: 1.25rem 1.4rem; margin-bottom: 1rem;
  }
  .bill-id { font-family: ui-monospace, monospace; font-size: 0.8rem; color: var(--accent); margin-bottom: 0.4rem; }
  .bill-title { font-size: 1.15rem; font-weight: 700; margin: 0 0 0.3rem; }
  .bill-desc { color: var(--slate); font-size: 0.9rem; }
  .bill-link { display: inline-block; margin-top: 0.6rem; font-size: 0.85rem; }
  .status-badge {
    display: inline-block; background: var(--accent-soft); color: var(--accent);
    font-size: 0.75rem; font-weight: 700; padding: 0.2rem 0.55rem; border-radius: 999px;
    margin-bottom: 0.5rem;
  }
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
"""


def nav_links(current):
    """Links to whichever OTHER content pages exist — computed once per
    page constant below (these are built at import time, not
    per-request, so this only ever runs a handful of times total).
    Flagged bills isn't listed here on purpose — it's personal and tied
    to login, so it lives in the account menu next to "View profile"
    rather than in this always-visible row (see ACCOUNT_MENU_SCRIPT)."""
    pages = [("/", "Lookup"), ("/lobbying", "Organization Search")]
    parts = []
    for href, label in pages:
        if href == current:
            continue
        parts.append(f'<a href="{href}">{"← " if href == "/" else ""}{label}{"" if href == "/" else " →"}</a>')
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


def top_nav(current, left_extra=""):
    """The full top-nav row: the 3-page links (or a custom left_extra,
    e.g. signup's "Skip for now"), plus the account menu pushed to the
    right via the slot's own margin-left:auto."""
    left = left_extra if left_extra else nav_links(current)
    return f'<div class="top-nav">{left}{ACCOUNT_MENU_SLOT}</div>{ACCOUNT_MENU_SCRIPT}'


PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/")}
  <h1>Bill Search</h1>
  <p class="sub">California bill status, sponsors, and history from LegiScan.</p>

  <form id="f">
    <input id="bill" placeholder="e.g. SB122" autocomplete="off" required>
    <button type="submit">Look up</button>
  </form>

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
    <h2 class="section">History</h2>
    <table>${{history || '<tr><td>No history available.</td></tr>'}}</table>
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
<title>Organization Search — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/lobbying")}
  <h1>Organization Search</h1>
  <p class="sub">California lobbying firms, employers, and quarterly disclosures from CAL-ACCESS.</p>

  <form id="f">
    <input id="q" placeholder="Firm, employer, or client name" autocomplete="off" required style="flex:1">
    <button type="submit">Search</button>
  </form>

  <p class="sub" style="margin-top:-1.2rem;font-size:0.82rem">
    <strong>Firm</strong> = hired by clients to lobby on their behalf &nbsp;·&nbsp;
    <strong>Employer</strong> = lobbies with its own in-house staff &nbsp;·&nbsp;
    <strong>Coalition</strong> = a group of organizations registered together
  </p>

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
    <table>
      <tr><th>Name</th><th>Type</th><th>Location</th><th>Status</th><th></th></tr>
      ${{rows.map(r => `
        <tr>
          <td><a href="${{detailUrl(r)}}">${{r.name}}</a></td>
          <td>${{r.entity_type ? `<span class="tag">${{r.entity_type}}</span>` : `<span class="tag">named as client only</span>`}}</td>
          <td>${{locationOrContext(r)}}</td>
          <td>${{r.registration_status || ''}}</td>
          <td><a class="secondary" href="/clients?prefill_name=${{encodeURIComponent(r.name)}}${{r.id ? `&prefill_entity_id=${{r.id}}` : ''}}">+ Client</a></td>
        </tr>
      `).join('')}}
    </table>
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
<title>Organization Detail — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/lobbying", left_extra='<a href="/lobbying">← Organization Search</a>')}
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
  if (!rows.length) return '<p class="empty">No lobbying relationships found for this name.</p>';
  return `
    <table>
      <tr><th>Firm</th><th>Client / employer</th><th>Period</th><th>Amount</th><th>Bill / activity</th></tr>
      ${{rows.map(r => `
        <tr>
          <td>${{highlight(r.firm, selectedName)}}</td>
          <td>${{highlight(r.client, selectedName)}}</td>
          <td class="date">${{(r.period_start || '').split(' ')[0]}} – ${{(r.period_end || '').split(' ')[0]}}</td>
          <td>${{money(r.amount_spent)}}</td>
          <td>${{r.raw_bill_text || ''}}</td>
        </tr>
      `).join('')}}
    </table>
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
    <h2 class="section">Lobbying relationships</h2>
    ${{relationshipRows(d.relationships, d.name)}}
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
<title>Sign up — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/signup", left_extra='<a href="/">← Lookup</a><a href="/login">Log in →</a>')}
  <h1>Create your account</h1>
  <p class="sub">Step 1 of 2 — after this, you'll fill in your CAL-ACCESS-style registration details.</p>

  <form id="f">
    <input id="email" type="email" placeholder="you@example.com" autocomplete="email" required style="flex:1 1 100%">
    <input id="password" type="password" placeholder="Password (8+ characters)" autocomplete="new-password" required style="flex:1 1 100%">
    <button type="submit">Continue →</button>
  </form>

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
<title>Log in — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/login", left_extra='<a href="/">← Lookup</a><a href="/signup">Sign up →</a>')}
  <h1>Log in</h1>

  <form id="f">
    <input id="email" type="email" placeholder="you@example.com" autocomplete="email" required style="flex:1 1 100%">
    <input id="password" type="password" placeholder="Password" autocomplete="current-password" required style="flex:1 1 100%">
    <button type="submit">Log in</button>
  </form>

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
    window.location.href = '/';
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
<title>Registration details — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/signup/profile", left_extra='<a href="/">Skip for now →</a>')}
  <h1>Registration details</h1>
  <p class="sub">Step 2 of 2 — modeled on CAL-ACCESS Form 601 (Lobbying Firm Registration Statement), so the fields match what you'd already recognize from the state's own form.</p>

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


PROFILE_VIEW_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your profile — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/profile")}
  <h1>Your profile</h1>
  <div id="loading">Loading…</div>
  <div id="error"></div>
  <div id="content"></div>
</div>

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
        <p class="empty">You haven't filled in your registration details yet.</p>
        <button type="button" onclick="window.location.href='/signup/profile'">Add registration details →</button>
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
      <h2 class="section">Registration details</h2>
      <div class="card">
        ${{row('Business address', [profile.bus_addr1, profile.bus_city, profile.bus_st, profile.bus_zip4].filter(Boolean).join(', '))}}
        ${{row('Mailing address', mailing)}}
        ${{row('Phone', profile.bus_phone)}}
        ${{row('CA SOS filer ID', profile.existing_filer_id)}}
      </div>
      <div class="card-actions">
        <button type="button" class="secondary" onclick="window.location.href='/signup/profile'">Edit →</button>
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
</body>
</html>
"""


FLAGGED_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Flagged Bills — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/flagged")}
  <h1>My Flagged Bills</h1>
  <p class="sub">Bills you've personally flagged — stored and re-checked daily the same way the shared watch list is, just scoped to your account.</p>
  <div id="error"></div>
  <div id="list"></div>
</div>

<script>
const listEl = document.getElementById('list');
const errorEl = document.getElementById('error');
let allClients = [];

async function load() {{
  try {{
    const [flaggedRes, clientsRes] = await Promise.all([fetch('/api/flagged'), fetch('/api/clients')]);
    if (flaggedRes.status === 401) {{
      window.location.href = '/login';
      return;
    }}
    allClients = await clientsRes.json();
    const rows = await flaggedRes.json();
    render(rows);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

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
  if (!rows.length) {{
    listEl.innerHTML = '<p class="empty">Nothing flagged yet — look up a bill and click "Flag this bill" from there.</p>';
    return;
  }}
  listEl.innerHTML = `
    <table>
      <tr><th>Bill</th><th>Title</th><th>Status</th><th>Last checked</th><th>Client</th><th></th></tr>
      ${{rows.map(r => `
        <tr>
          <td class="chamber">
            ${{r.state}} ${{r.bill_number}}
            <div style="display:flex;gap:0.4rem;flex-wrap:wrap;margin-top:0.4rem;text-transform:none;font-weight:400;font-size:0.85rem">
              ${{r.url ? `<a class="secondary" href="${{r.url}}" target="_blank" rel="noopener">View</a>` : ''}}
              <a class="secondary" href="/report?bill_id=${{r.bill_id}}">Report</a>
            </div>
          </td>
          <td>${{r.title || ''}}</td>
          <td>${{r.status_label || ''}}</td>
          <td class="date">${{(r.last_checked_at || '').replace('T', ' ').slice(0, 16)}}</td>
          <td>${{clientCell(r)}}</td>
          <td><button class="danger" onclick="unflag(${{r.bill_id}})">Unflag</button></td>
        </tr>
      `).join('')}}
    </table>
  `;
}}

load();
</script>
</body>
</html>
"""


CLIENTS_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clients — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/clients")}
  <h1>Clients</h1>
  <p class="sub">Modeled on CAL-ACCESS Forms 602/603, tied to your account.</p>

  <button type="button" id="add-client-btn">+ Add client</button>

  <form id="f" style="display:none;margin-top:1rem">
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

  <div id="loading">Saving…</div>
  <div id="error"></div>

  <h2 class="section" style="margin-top:2rem">Your clients</h2>
  <div id="list"></div>
</div>

<script>
const form = document.getElementById('f');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');
const listEl = document.getElementById('list');
const addBtn = document.getElementById('add-client-btn');
const cancelBtn = document.getElementById('cancel-client-btn');
const submitBtn = document.getElementById('submit-client-btn');
let allClients = [];
let editingId = null;  // null = creating a new client; otherwise the id being edited

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
  form.style.display = 'flex';
  addBtn.style.display = 'none';
}}

function hideForm() {{
  form.style.display = 'none';
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
      <tr><th>Name</th><th>Business address</th><th>Industry / interests</th><th>Filer ID</th><th></th></tr>
      ${{rows.map(c => `
        <tr>
          <td><a href="/clients/detail?id=${{c.id}}">${{c.name}}</a></td>
          <td>${{[c.bus_addr1, c.bus_city, c.bus_st, c.bus_zip4].filter(Boolean).join(', ')}}</td>
          <td>${{c.interests || ''}}</td>
          <td>${{c.existing_filer_id || ''}}</td>
          <td>
            <div style="display:flex;gap:0.4rem;flex-wrap:wrap">
              <a class="secondary" href="#" onclick="event.preventDefault(); editClient(${{c.id}})">Edit</a>
              <button class="danger" onclick="removeClient(${{c.id}})">Remove</button>
            </div>
          </td>
        </tr>
      `).join('')}}
    </table>
  `;
}}

load();
applyPrefill();
</script>
</body>
</html>
"""


# Action report — everything about one bill in one place: current
# One client's own page: org info, every bill assigned to them with its
# position, and a way to add a new bill starting from here rather than
# only from /flagged — the reverse direction of the existing
# flag-then-assign flow. Reached via ?id=..., e.g. from the Clients list
# or Organization Search's "+ Add as client" link.
CLIENT_DETAIL_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Client — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/clients", left_extra='<a href="/clients">← Clients</a>')}
  <div id="error"></div>
  <div id="client"></div>

  <h2 class="section" style="margin-top:2rem">Add a bill</h2>
  <form id="add-bill-f">
    <input id="bill_number" placeholder="e.g. SB122" autocomplete="off" required style="flex:1;min-width:8rem">
    <select id="add-bill-position">
      <option value="watch">Watch</option>
      <option value="support">Support</option>
      <option value="oppose">Oppose</option>
    </select>
    <button type="submit">Add →</button>
  </form>
  <div id="add-bill-loading" class="empty" style="display:none">Looking up bill…</div>

  <h2 class="section" style="margin-top:2rem">Bills</h2>
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
      <tr><th>Bill</th><th>Title</th><th>Status</th><th>Position</th><th></th></tr>
      ${{bills.map(b => `
        <tr>
          <td class="chamber">${{b.state}} ${{b.bill_number}}</td>
          <td>${{b.title || ''}}</td>
          <td>${{b.status_label || ''}}</td>
          <td>${{positionSelect(b.bill_id, b.position || 'watch')}}</td>
          <td>
            <div style="display:flex;gap:0.4rem;flex-wrap:wrap">
              <a class="secondary" href="/report?bill_id=${{b.bill_id}}">Report</a>
              <button class="danger" onclick="removeBill(${{b.bill_id}})">Remove</button>
            </div>
          </td>
        </tr>
      `).join('')}}
    </table>
  `;
}}

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
</body>
</html>
"""


# Action report — everything about one bill in one place: current
# status, full status history, amendment history, upcoming hearings,
# and (if this signed-in user has assigned it to one of their own
# clients) that client's name and current position. Reached via
# ?bill_id=... — e.g. linked from a "Report" link on /flagged — rather
# than being a page anyone navigates to on its own.
REPORT_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Action Report — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/report")}
  <div id="error"></div>
  <div id="report"></div>
</div>

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

    <h2 class="section">Assigned client${{(r.assigned_clients || []).length === 1 ? '' : 's'}}</h2>
    ${{clientBadges || '<p class="empty">Not currently assigned to any of your clients.</p>'}}

    <h2 class="section">Status history</h2>
    ${{historyRows
      ? `<table><tr><th>Date</th><th>Chamber</th><th>Action</th></tr>${{historyRows}}</table>`
      : '<p class="empty">No status history recorded yet.</p>'}}

    <h2 class="section">Amendment history</h2>
    ${{amendmentRows
      ? `<table><tr><th>Date</th><th>Chamber</th><th>Amendment</th></tr>${{amendmentRows}}</table>`
      : '<p class="empty">No amendments recorded.</p>'}}

    <h2 class="section">Upcoming hearings</h2>
    ${{hearingRows
      ? `<table><tr><th>When</th><th>Type</th><th>Details</th></tr>${{hearingRows}}</table>`
      : '<p class="empty">No upcoming hearings scheduled.</p>'}}
  `;
}}

load();
</script>
</body>
</html>
"""


# "Prepare my disclosure form" — /disclosures (pick a form, generate a
# draft, see everything you've prepared before) and /disclosures/review
# (one draft: the actual filled PDF, known-gap notes, and the sign-off
# step). This app never files anything itself — see pdf_forms.py and
# db.sign_off_prepared_filing for where that boundary is enforced.
DISCLOSURES_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Disclosure Forms — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/disclosures")}
  <h1>Disclosure Forms</h1>
  <p class="sub">Prepare a real FPPC disclosure form, pre-filled from your profile and clients. This app never files anything on your behalf — it only prepares the document for you to review, sign off on, and file yourself.</p>

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

  <div id="loading">Generating…</div>
  <div id="error"></div>

  <h2 class="section" style="margin-top:2rem">Your prepared filings</h2>
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
      <tr><th>Form</th><th>Period</th><th>Status</th><th>Created</th><th></th></tr>
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
    </table>
  `;
}}

load();
</script>
</body>
</html>
"""


DISCLOSURE_REVIEW_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review Disclosure Form — Bill Search</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  {top_nav("/disclosures")}
  <div id="error"></div>
  <div id="content"></div>
</div>

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

    <div class="card" style="padding:0">
      <iframe src="${{pdfUrl}}" style="width:100%;height:70vh;border:none;border-radius:10px" title="Filled ${{r.form_type}} preview"></iframe>
    </div>
    <div class="card-actions" style="margin:-0.5rem 0 1.5rem">
      <a class="secondary" href="${{pdfUrl}}" target="_blank" rel="noopener">Open PDF in a new tab →</a>
    </div>

    ${{signOffSection}}
  `;
}}

load();
</script>
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
    print(f"Bill Search running on {url}  (Ctrl+C to stop)")
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
