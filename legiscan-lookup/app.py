#!/usr/bin/env python3
"""
LegiScan Bill Lookup — a small local web app.

Runs entirely on your machine (not hosted anywhere) so it has normal
internet access and can call the LegiScan API live, on demand, when you
search. Start it with `python3 app.py` (or `./start.sh`), then open
http://localhost:8420 in your browser.

The API key is read from the LEGISCAN_API_KEY environment variable. If
that's not set (e.g. you're running this from a non-login shell), it falls
back to reading the `export LEGISCAN_API_KEY=...` line out of ~/.zshrc.
"""

import base64
import json
import os
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

PORT = int(os.environ.get("PORT", 8420))
LEGISCAN_BASE = "https://api.legiscan.com/"

# Optional shared-login protection. Leave LOOKUP_USER / LOOKUP_PASSWORD
# unset for frictionless local use; set both when hosting this somewhere
# reachable by other people.
AUTH_USER = os.environ.get("LOOKUP_USER")
AUTH_PASSWORD = os.environ.get("LOOKUP_PASSWORD")


def get_api_key():
    key = os.environ.get("LEGISCAN_API_KEY")
    if key:
        return key
    # Fall back to parsing it out of ~/.zshrc, in case this was launched
    # from a shell that never sourced the profile (e.g. double-clicked).
    zshrc = os.path.expanduser("~/.zshrc")
    try:
        with open(zshrc) as f:
            for line in f:
                m = re.search(r'export\s+LEGISCAN_API_KEY\s*=\s*"?([^"\s]+)"?', line)
                if m:
                    return m.group(1)
    except FileNotFoundError:
        pass
    return None


def legiscan_call(op, **params):
    key = get_api_key()
    if not key:
        raise RuntimeError(
            "No LegiScan API key found. Set LEGISCAN_API_KEY in your "
            "environment (or ~/.zshrc) and restart this app."
        )
    query = {"key": key, "op": op, **params}
    url = LEGISCAN_BASE + "?" + urlencode(query)
    with urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def lookup_bill(state, bill_number):
    state = state.strip().upper()
    bill_number = bill_number.strip().upper()

    search = legiscan_call("getSearch", state=state, bill=bill_number)
    if search.get("status") != "OK":
        raise RuntimeError(f"LegiScan search failed: {search}")

    results = search.get("searchresult", {})
    match = None
    for k, v in results.items():
        if k == "summary":
            continue
        match = v
        break
    if not match:
        raise RuntimeError(f"No bill found for {state} {bill_number}.")

    bill_id = match["bill_id"]
    detail = legiscan_call("getBill", id=bill_id)
    if detail.get("status") != "OK":
        raise RuntimeError(f"LegiScan getBill failed: {detail}")

    bill = detail["bill"]
    return {
        "state": bill.get("state"),
        "bill_number": bill.get("bill_number"),
        "title": bill.get("title"),
        "description": bill.get("description"),
        "status_date": bill.get("status_date"),
        "url": bill.get("url"),
        "sponsors": [
            {"name": s.get("name"), "party": s.get("party"), "role": s.get("role")}
            for s in bill.get("sponsors", [])
        ],
        "history": [
            {"date": h.get("date"), "chamber": h.get("chamber"), "action": h.get("action")}
            for h in bill.get("history", [])
        ],
    }


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LegiScan Bill Lookup</title>
<style>
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
  h2.section { font-size: 0.95rem; margin: 1.6rem 0 0.6rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.87rem; }
  td { padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--rule); vertical-align: top; }
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
</style>
</head>
<body>
<div class="wrap">
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

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const state = document.getElementById('state').value.trim();
  const bill = document.getElementById('bill').value.trim();
  if (!state || !bill) return;

  errorEl.className = ''; resultEl.className = ''; loadingEl.className = 'show';
  form.querySelector('button').disabled = true;

  try {
    const res = await fetch(`/api/bill?state=${encodeURIComponent(state)}&bill=${encodeURIComponent(bill)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Lookup failed');
    render(data);
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  } finally {
    loadingEl.className = '';
    form.querySelector('button').disabled = false;
  }
});

function render(d) {
  const sponsors = (d.sponsors || []).map(s =>
    `<span class="sponsor">${s.name}${s.party ? ' (' + s.party + ')' : ''}</span>`
  ).join('');

  const history = (d.history || []).map(h =>
    `<tr><td class="date">${h.date || ''}</td><td class="chamber">${h.chamber || ''}</td><td>${h.action || ''}</td></tr>`
  ).join('');

  resultEl.innerHTML = `
    <div class="card">
      <div class="bill-id">${d.state} ${d.bill_number}</div>
      <div class="bill-title">${d.title || ''}</div>
      <div class="bill-desc">${d.description || ''}</div>
      ${d.url ? `<a class="bill-link" href="${d.url}" target="_blank" rel="noopener">View on LegiScan →</a>` : ''}
    </div>
    ${sponsors ? `<h2 class="section">Sponsors</h2><div class="sponsor-list">${sponsors}</div>` : ''}
    <h2 class="section">History</h2>
    <table>${history || '<tr><td>No history available.</td></tr>'}</table>
  `;
  resultEl.className = 'show';
}
</script>
</body>
</html>
"""


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

    def do_GET(self):
        if not self._authorized():
            self._require_auth()
            return

        parsed = urlparse(self.path)

        if parsed.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/bill":
            qs = parse_qs(parsed.query)
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

        self.send_response(404)
        self.end_headers()


def main():
    if not get_api_key():
        print("⚠️  No LEGISCAN_API_KEY found in your environment or ~/.zshrc.")
        print("    Set it with: export LEGISCAN_API_KEY=your_key_here")
        print("    (the app will still start, but lookups will fail until it's set)\n")

    is_hosted = bool(os.environ.get("RENDER") or os.environ.get("PORT_ASSIGNED_BY_HOST"))
    if (AUTH_USER or AUTH_PASSWORD) and not (AUTH_USER and AUTH_PASSWORD):
        print("⚠️  Only one of LOOKUP_USER / LOOKUP_PASSWORD is set — both are")
        print("    required for login protection to take effect. Running open.\n")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}" if not is_hosted else f"port {PORT}"
    print(f"LegiScan Bill Lookup running on {url}  (Ctrl+C to stop)")
    if not is_hosted:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
