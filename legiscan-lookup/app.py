#!/usr/bin/env python3
"""
LegiScan Bill Lookup — a small local web app.

Runs entirely on your machine (not hosted anywhere) so it has normal
internet access and can call the LegiScan API live, on demand, when you
search. Start it with `python3 app.py` (or `./start.sh`), then open
http://localhost:8420 in your browser.

Three capabilities live here:

  - Live lookup (the original feature): search a bill by state + number,
    call LegiScan on the spot, show the result. Nothing is stored.
  - Stored watch list (added in Phase 1, Session 5): add a bill to a
    watch list and its current status/sponsors/history get saved to
    the database. See /watchlist.
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
  .top-nav { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.25rem; }
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
"""


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
  <div class="top-nav"><span></span><a href="/watchlist">Watch list →</a></div>
  <h1>LegiScan Bill Lookup</h1>
  <p class="sub">Runs locally on this Mac — every search calls the LegiScan API live.</p>

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
  <div class="top-nav"><a href="/">← Lookup</a><span></span></div>
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
        if not self._authorized():
            self._require_auth()
            return

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/":
            self._send_html(200, PAGE)
            return

        if parsed.path == "/watchlist":
            self._send_html(200, WATCHLIST_PAGE)
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
