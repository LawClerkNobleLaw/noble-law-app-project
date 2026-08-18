#!/usr/bin/env python3
"""
LegiScan Bill Lookup — a small local web app.

Runs entirely on your machine (not hosted anywhere) so it has normal
internet access and can call the LegiScan API live, on demand, when you
search. Start it with `python3 app.py` (or `./start.sh`), then open
http://localhost:8420 in your browser.

Four capabilities live here:

  - Live lookup (the original feature): search a bill by state + number,
    call LegiScan on the spot, show the result. Nothing is stored.
  - Stored watch list (added in Phase 1, Session 5): add a bill to a
    watch list and its current status/sponsors/history get saved to
    the database. See /watchlist.
  - Lobbying search (/lobbying): search the CAL-ACCESS firms/employers
    (lobbying_entities) and quarterly disclosures (lobbying_disclosures)
    that calaccess-pipeline/refresh_calaccess.py loads. Since roughly a
    third of clients named in a disclosure have no independent
    registration of their own (see that file's docstring), search
    matches both the registered-entity name AND the free-text
    client_name on disclosures, and a result's detail view shows BOTH
    directions: what this entity filed (if it's a firm/employer that
    files) and where this name was mentioned as someone else's client.
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

import base64
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db
import refresh_watchlist
from legiscan_client import get_api_key, lookup_bill, get_bill_detail

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calaccess-pipeline"))
import refresh_calaccess  # noqa: E402 — must follow the sys.path insert above

PORT = int(os.environ.get("PORT", 8420))

# Optional shared-login protection. Leave LOOKUP_USER / LOOKUP_PASSWORD
# unset for frictionless local use; set both when hosting this somewhere
# reachable by other people.
AUTH_USER = os.environ.get("LOOKUP_USER")
AUTH_PASSWORD = os.environ.get("LOOKUP_PASSWORD")

# Gates the two /internal/refresh-* routes. Unset locally on purpose —
# see the module docstring above.
REFRESH_SECRET = os.environ.get("REFRESH_SECRET")

# Guards against a cron firing twice before the first run finishes —
# maps job name -> bool. Not persisted; a restart just clears it, which is
# fine, since the worst case is one extra run, not a corrupted one (every
# refresh is upsert-based already).
_refresh_running = {"watchlist": False, "calaccess": False}
_refresh_lock = threading.Lock()


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
  input {
    font: inherit; padding: 0.6rem 0.75rem; border: 1px solid var(--rule);
    border-radius: 8px; background: var(--surface); color: var(--ink);
  }
  input#state { width: 5rem; text-transform: uppercase; }
  input#bill { flex: 1; min-width: 8rem; }
  button {
    font: inherit; font-weight: 600; padding: 0.6rem 1.1rem; border: none;
    border-radius: 8px; background: var(--accent); color: white; cursor: pointer;
  }
  button:hover { opacity: 0.9; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.secondary { background: var(--accent-soft); color: var(--accent); }
  button.danger { background: var(--error-soft); color: var(--error); }
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
"""


def nav_links(current):
    """The 3 pages link to whichever OTHER two pages exist — computed
    once per page constant below (these are built at import time, not
    per-request, so this only ever runs 3 times total)."""
    pages = [("/", "Lookup"), ("/watchlist", "Watch list"), ("/lobbying", "Lobbying search")]
    parts = []
    for href, label in pages:
        if href == current:
            continue
        parts.append(f'<a href="{href}">{"← " if href == "/" else ""}{label}{"" if href == "/" else " →"}</a>')
    return "".join(parts)


PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LegiScan Bill Lookup</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <div class="top-nav">{nav_links("/")}</div>
  <h1>LegiScan Bill Lookup</h1>

  <form id="f">
    <input id="state" placeholder="CA" maxlength="2" autocomplete="off" required>
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
  const state = document.getElementById('state').value.trim();
  const bill = document.getElementById('bill').value.trim();
  if (!state || !bill) return;

  errorEl.className = ''; resultEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button').disabled = true;

  try {{
    const res = await fetch(`/api/bill?state=${{encodeURIComponent(state)}}&bill=${{encodeURIComponent(bill)}}`);
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

async function addToWatchlist() {{
  if (!current) return;
  const btn = document.getElementById('watch-btn');
  btn.disabled = true;
  btn.textContent = 'Adding…';
  try {{
    const res = await fetch('/api/watchlist', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ bill_id: current.id }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not add to watch list');
    btn.textContent = '✓ On watch list';
  }} catch (err) {{
    btn.disabled = false;
    btn.textContent = 'Add to watch list';
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
      <div class="bill-id">${{d.state}} ${{d.bill_number}}</div>
      ${{d.status_label ? `<div class="status-badge">${{d.status_label}}</div>` : ''}}
      <div class="bill-title">${{d.title || ''}}</div>
      <div class="bill-desc">${{d.description || ''}}</div>
      ${{d.url ? `<a class="bill-link" href="${{d.url}}" target="_blank" rel="noopener">View on LegiScan →</a>` : ''}}
      <div class="card-actions">
        <button id="watch-btn" class="secondary" onclick="addToWatchlist()">Add to watch list</button>
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


WATCHLIST_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watch list — LegiScan Bill Lookup</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <div class="top-nav">{nav_links("/watchlist")}</div>
  <h1>Watch list</h1>
  <p class="sub">Bills here are stored in the database and re-checked once a day, not looked up live.</p>
  <div id="error"></div>
  <div id="list"></div>
</div>

<script>
const listEl = document.getElementById('list');
const errorEl = document.getElementById('error');

async function load() {{
  try {{
    const res = await fetch('/api/watchlist');
    const rows = await res.json();
    render(rows);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

async function remove(billId) {{
  try {{
    const res = await fetch(`/api/watchlist?bill_id=${{billId}}`, {{ method: 'DELETE' }});
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

function render(rows) {{
  if (!rows.length) {{
    listEl.innerHTML = '<p class="empty">Nothing watched yet — look up a bill and add it from there.</p>';
    return;
  }}
  listEl.innerHTML = `
    <table>
      <tr><th>Bill</th><th>Title</th><th>Status</th><th>Last checked</th><th></th></tr>
      ${{rows.map(r => `
        <tr>
          <td class="chamber">${{r.state}} ${{r.bill_number}}</td>
          <td>${{r.title || ''}}${{r.url ? ` — <a href="${{r.url}}" target="_blank" rel="noopener">view</a>` : ''}}</td>
          <td>${{r.status_label || ''}}</td>
          <td class="date">${{(r.last_checked_at || '').replace('T', ' ').slice(0, 16)}}</td>
          <td><button class="danger" onclick="remove(${{r.bill_id}})">Remove</button></td>
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


LOBBYING_PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lobbying search — LegiScan Bill Lookup</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <div class="top-nav">{nav_links("/lobbying")}</div>
  <h1>Lobbying search</h1>
  <p class="sub">California lobbying firms, employers, and quarterly disclosures from CAL-ACCESS.</p>

  <form id="f">
    <input id="q" placeholder="Firm, employer, or client name" autocomplete="off" required style="flex:1">
    <button type="submit">Search</button>
  </form>

  <div id="loading">Searching…</div>
  <div id="error"></div>
  <div id="results"></div>
  <div id="detail"></div>
</div>

<script>
const form = document.getElementById('f');
const resultsEl = document.getElementById('results');
const detailEl = document.getElementById('detail');
const errorEl = document.getElementById('error');
const loadingEl = document.getElementById('loading');

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const q = document.getElementById('q').value.trim();
  if (!q) return;

  errorEl.className = ''; detailEl.innerHTML = ''; loadingEl.className = 'show';
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

function renderResults(rows) {{
  if (!rows.length) {{
    resultsEl.innerHTML = '<p class="empty">No firms, employers, or named clients match that.</p>';
    return;
  }}
  resultsEl.innerHTML = `
    <table>
      <tr><th>Name</th><th>Type</th><th>Location</th><th>Status</th></tr>
      ${{rows.map(r => `
        <tr class="row-link" onclick='loadDetail(${{JSON.stringify(r).replace(/'/g, "&#39;")}})'>
          <td>${{r.name}}</td>
          <td>${{r.entity_type ? `<span class="tag">${{r.entity_type}}</span>` : `<span class="tag">named as client only</span>`}}</td>
          <td>${{[r.city, r.state].filter(Boolean).join(', ')}}</td>
          <td>${{r.registration_status || ''}}</td>
        </tr>
      `).join('')}}
    </table>
  `;
}}

async function loadDetail(r) {{
  detailEl.innerHTML = '<p class="empty">Loading…</p>';
  try {{
    const params = r.id ? `id=${{encodeURIComponent(r.id)}}` : `name=${{encodeURIComponent(r.name)}}`;
    const res = await fetch(`/api/lobbying/detail?${{params}}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not load detail');
    renderDetail(data);
  }} catch (err) {{
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  }}
}}

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
        <div class="bill-desc" style="margin-top:0.5rem">${{[e.city, e.state].filter(Boolean).join(', ')}}</div>
      ` : '<div class="bill-desc" style="margin-top:0.3rem">Named as a client in a disclosure — no independent registration on file.</div>'}}
    </div>
    <h2 class="section">Lobbying relationships</h2>
    ${{relationshipRows(d.relationships, d.name)}}
  `;
}}
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
        results.append({
            "kind": "client", "id": None, "name": name, "entity_type": None,
            "city": None, "state": None, "registration_status": None,
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
            "SELECT id, name, entity_type, city, state, registration_status, source_form "
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
            target_fn()
        except Exception as e:
            # Each job's own log() already records its own failures in
            # detail (see refresh_one/sync_disclosures etc.) — this is
            # just a backstop for anything that escapes those, e.g. a
            # crash before that job's own logging even starts.
            print(f"[{job_name} refresh] crashed: {e}")
        finally:
            with _refresh_lock:
                _refresh_running[job_name] = False

    threading.Thread(target=run, daemon=True, name=f"refresh-{job_name}").start()
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def _authorized(self):
        if not AUTH_USER or not AUTH_PASSWORD:
            return True  # no shared login configured — open access (local use)
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            return False
        return user == AUTH_USER and password == AUTH_PASSWORD

    def _require_auth(self):
        body = b"Login required."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="LegiScan Bill Lookup"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status, html):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

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
            self._send_json(200, {"counts": counts, "refresh_running": dict(_refresh_running)})
            return

        if not self._authorized():
            self._require_auth()
            return

        qs = parse_qs(parsed.query)

        if parsed.path == "/":
            self._send_html(200, PAGE)
            return

        if parsed.path == "/watchlist":
            self._send_html(200, WATCHLIST_PAGE)
            return

        if parsed.path == "/lobbying":
            self._send_html(200, LOBBYING_PAGE)
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
            state = (qs.get("state") or [""])[0]
            bill = (qs.get("bill") or [""])[0]
            if not state or not bill:
                self._send_json(400, {"error": "Missing state or bill parameter."})
                return
            try:
                data = lookup_bill(state, bill)
                self._send_json(200, data)
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        if parsed.path == "/api/watchlist":
            conn = db.get_connection()
            try:
                self._send_json(200, db.list_watchlist(conn))
            finally:
                conn.close()
            return

        self.send_response(404)
        self.end_headers()

    def _authorized_for_refresh(self):
        """Separate from _authorized(): the shared LOOKUP_USER/PASSWORD
        login is for humans in a browser. These routes are hit by a cron
        job with no browser, gated on their own secret instead — and if
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

        if not self._authorized():
            self._require_auth()
            return

        if parsed.path != "/api/watchlist":
            self.send_response(404)
            self.end_headers()
            return

        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "Invalid JSON body."})
            return

        bill_id = body.get("bill_id")
        if not bill_id:
            self._send_json(400, {"error": "Missing bill_id."})
            return

        try:
            # Re-fetch fresh from LegiScan rather than trusting whatever
            # the client already had lying around, so what gets stored is
            # accurate at the moment it's added.
            bill = get_bill_detail(bill_id)
        except Exception as e:
            self._send_json(502, {"error": str(e)})
            return

        conn = db.get_connection()
        try:
            db.upsert_bill(conn, bill)
            db.add_to_watchlist(conn, bill_id)
            conn.commit()
            self._send_json(200, db.list_watchlist(conn))
        finally:
            conn.close()

    def do_DELETE(self):
        if not self._authorized():
            self._require_auth()
            return

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path != "/api/watchlist":
            self.send_response(404)
            self.end_headers()
            return

        bill_id = (qs.get("bill_id") or [""])[0]
        if not bill_id:
            self._send_json(400, {"error": "Missing bill_id parameter."})
            return

        conn = db.get_connection()
        try:
            db.remove_from_watchlist(conn, bill_id)
            conn.commit()
            self._send_json(200, db.list_watchlist(conn))
        finally:
            conn.close()


def main():
    db.init_db()

    if not get_api_key():
        print("⚠️  No LEGISCAN_API_KEY found in your environment or ~/.zshrc.")
        print("    Set it with: export LEGISCAN_API_KEY=your_key_here")
        print("    (the app will still start, but lookups will fail until it's set)\n")

    is_hosted = bool(os.environ.get("RENDER") or os.environ.get("PORT_ASSIGNED_BY_HOST"))
    if (AUTH_USER or AUTH_PASSWORD) and not (AUTH_USER and AUTH_PASSWORD):
        print("⚠️  Only one of LOOKUP_USER / LOOKUP_PASSWORD is set — both are")
        print("    required for login protection to take effect. Running open.\n")

    # ThreadingHTTPServer, not HTTPServer — a plain HTTPServer handles one
    # request at a time, so an /internal/refresh-calaccess trigger firing
    # off a multi-minute background job wouldn't itself block (that part
    # runs in its own thread already), but every OTHER visitor hitting the
    # site while that request is even being accepted would queue behind
    # it. Threading it costs nothing for the low request volume this app
    # actually sees.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}" if not is_hosted else f"port {PORT}"
    print(f"LegiScan Bill Lookup running on {url}  (Ctrl+C to stop)")
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
