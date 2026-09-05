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
    the account-menu dropdown on every page (see account_widget()) —
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

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import accounts
import build_bill_corpus
import code_sections
import config
import db
import directory
import disclosure_fields
import letter_drafts
import mailer
import pdf_forms
import refresh_watchlist
import legiscan_client
from legiscan_client import lookup_bill, get_bill_detail, search_bills, smart_search

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calaccess-pipeline"))
import refresh_calaccess  # noqa: E402 — must follow the sys.path insert above

PORT = config.PORT

# Where the CSS/JS extracted out of this module live. Resolved off
# __file__ rather than the working directory so `python3 app.py` from
# anywhere (and Render's own start command) finds them the same way.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# How many bills one "flag selected" can take. Each is its own getBill
# call against LegiScan (see /api/flag-bulk), so this is a quota and
# latency bound, not a UI one — a request for fifty would sit there
# for the better part of a minute.
MAX_BULK_FLAG = 25

JS_CONTENT_TYPE = "application/javascript; charset=utf-8"


def _read_static_text(name):
    """Read one static asset into a string, at import time.

    Deliberately not re-read per request: these change when the app is
    deployed, not while it runs, and a per-request read would put a disk
    hit in front of every page load to save an edit-time restart — the
    same tradeoff the rest of this module already makes by holding its
    pages in module-level constants."""
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as fh:
        return fh.read()


TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

_TEMPLATE_SLOT = re.compile(r"\{\{(\w+)\}\}")


def _render_template(name, **slots):
    """Read templates/<name> and fill in its {{slot}} placeholders.

    Not a templating engine, and deliberately not an f-string any more:
    these pages are full of CSS rules and of JS template literals, so
    both `{...}` and `${...}` have to reach the browser untouched. That
    rules out str.format() (which reads every CSS brace as a field) and
    string.Template (whose $name would collide with `${...}`); `{{name}}`
    is the one shape neither CSS nor JS produces on its own, which is
    why it's the marker here. Nothing in these pages renders a literal
    `{{...}}`, so there's nothing for it to shadow.

    Called once per page at import, exactly as these constants were
    built before -- not per request (see app_shell()'s own docstring on
    why every page constant here is built once).

    A slot with no value, or a value with no slot, raises instead of
    quietly rendering a half-filled page: a typo then fails at boot
    rather than on somebody's screen.
    """
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as fh:
        text = fh.read()

    used = set()

    def fill(match):
        key = match.group(1)
        if key not in slots:
            raise KeyError(f"{name}: template has no value for slot {key}")
        used.add(key)
        return str(slots[key])

    filled = _TEMPLATE_SLOT.sub(fill, text)
    unfilled = set(slots) - used
    if unfilled:
        raise KeyError(f"{name}: passed slot(s) the template never uses: {sorted(unfilled)}")
    return filled


def _asset_url(name):
    """/static/<name> plus a short content hash, e.g. style.css?v=1a2b3c4d.

    The hash is what makes _send_static's year-long Cache-Control safe:
    the URL changes whenever the bytes do, so nobody gets served a stale
    stylesheet after a deploy, and nobody re-downloads an unchanged one."""
    body = STATIC_ASSETS[name][0]
    return f"/static/{name}?v={hashlib.sha256(body).hexdigest()[:8]}"

# Gates the two /internal/refresh-* routes. Unset locally on purpose —
# see the module docstring above.
REFRESH_SECRET = config.REFRESH_SECRET

# Guards against a cron firing twice before the first run finishes —
# maps job name -> bool. Not persisted; a restart just clears it, which is
# fine, since the worst case is one extra run, not a corrupted one (every
# refresh is upsert-based already).
# How much CSV one import may carry. A whole-Legislature staff sheet is
# ~120 offices by ~90 columns and lands well under a megabyte; eight is
# room for a much wider sheet and still small enough that the whole
# thing can sit in memory in a request thread without anyone having to
# think about it. The cap exists because the text arrives as a JSON
# string, so nothing is streamed and the ceiling should be stated rather
# than discovered.
DIRECTORY_MAX_BYTES = 8 * 1024 * 1024

_refresh_running = {"watchlist": False, "calaccess": False, "corpus": False}

# What /internal/status reports back for "did the last refresh actually
# work" — filled in by _trigger_refresh's run() below. Not persisted,
# same tradeoff as _refresh_running: a restart just means this is empty
# until the next refresh runs, not a corrupted answer.
_last_refresh = {"watchlist": None, "calaccess": None, "corpus": None}
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


def _login_lockout_remaining_minutes(email):
    """How many whole minutes are actually left on this email's lockout
    — separate from _login_locked_out() (which just returns a bool, and
    has direct `is True`/`is False` test coverage that a changed return
    type would break) so the message shown at lockout time can state
    the real wait instead of a generic "a few minutes" regardless of
    whether it's minute 1 or minute 14 of the window."""
    with _login_failures_lock:
        entry = _login_failures.get(email)
        if not entry:
            return 0
        _, first_failure = entry
        remaining = LOGIN_LOCKOUT_WINDOW - (datetime.datetime.now() - first_failure)
        return max(1, int(remaining.total_seconds() // 60) + 1)


# The app's one stylesheet, now static/style.css rather than ~865 lines
# of CSS inside this module. Read once at import (not per request) and
# served at /static/style.css by _send_static below, so a browser fetches
# it once for a whole session instead of re-reading an identical copy
# inlined into every single page response.
#
# Still a module-level constant under the name STYLE because comments
# throughout this file cross-reference "STYLE" as the place a given rule
# lives — those now mean static/style.css — and because the route above
# serves this exact string.
STYLE = _read_static_text("style.css")


# Deliberately NOT HttpOnly (unlike accounts.SESSION_COOKIE) — see
# Handler._signed_in_hint_cookie_header()'s own comment for why this
# needs to be readable by THEME_INIT_SCRIPT's inline script and why
# that's safe (it grants nothing; it's a UI hint, not an auth token).
SIGNED_IN_HINT_COOKIE = "signed_in_hint"

# Sets data-theme from localStorage BEFORE first paint, so a page load
# doesn't flash the wrong theme for a moment before JS gets around to
# correcting it. Every page's <head> includes this, right after <style>
# (see STYLE's own :not([data-theme="light"]) media query and
# :root[data-theme="dark"] block, which is what this attribute actually
# controls).
#
# Priority order: (1) an explicit choice from the toggle in
# account_widget() (localStorage['theme'], set for anyone — signed in
# or not — who's ever used it) always wins. (2) Failing that, a
# signed-OUT visitor defaults to dark rather than following the OS's
# light/dark preference — product decision, not a bug: this app is
# meant to look like the mockup's always-dark marketing/auth
# experience for anyone who hasn't signed in yet. "Signed out" here
# means SIGNED_IN_HINT_COOKIE is absent — that cookie is set/cleared
# alongside the real session cookie (see _signed_in_hint_cookie_header)
# specifically because this script runs synchronously before any
# fetch could resolve, and the real session cookie is HttpOnly (opaque
# to JS by design). (3) Otherwise (signed in, no explicit choice),
# leave the attribute unset entirely, so the plain OS-preference media
# query keeps working exactly as it always has.
#
# One edge case worth naming: a session created before this change
# shipped won't have SIGNED_IN_HINT_COOKIE yet, so that visitor reads
# as "signed out" for this initial-paint guess only, until their next
# login/logout resets it — account_widget()'s own /api/me check still
# correctly shows them as signed in regardless; this only affects which
# theme they see before that check resolves.
THEME_INIT_SCRIPT = f"""
<script>
(function() {{
  var t = localStorage.getItem('theme');
  if (t === 'dark' || t === 'light') {{ document.documentElement.setAttribute('data-theme', t); return; }}
  var signedIn = document.cookie.indexOf('{SIGNED_IN_HINT_COOKIE}=1') !== -1;
  if (!signedIn) document.documentElement.setAttribute('data-theme', 'dark');
}})();
</script>
"""

# Two Google Fonts (free, no licensing decision needed): Poppins is the
# body/UI typeface (--font-sans in STYLE); Instrument Serif is the one
# deliberate display exception the mockup uses for an editorial-style
# headline (--font-serif). Garet, the mockup's actual primary typeface,
# is a paid font whose web-embedding license isn't confirmed — omitted
# here on purpose (see STYLE's own comment on --font-sans); the stack
# already falls through to Poppins without it. Included in every page's
# <head> alongside THEME_INIT_SCRIPT so both are one edit, not five.
FONT_LINKS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
"""


def nav_links(current):
    """Links to whichever OTHER content pages exist — computed once per
    page constant below (these are built at import time, not
    per-request, so this only ever runs a handful of times total).
    Flagged bills isn't listed here on purpose — it's personal and tied
    to login, so it lives in the account menu next to "View profile"
    rather than in this always-visible row (see account_widget())."""
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
# the static HTML. account_widget() below ships both the guest state
# and the signed-in state up front (each hidden via style="display:none"
# until /api/me's own client-side fetch reveals whichever applies) —
# the same "server ships a shell, JS fetches JSON and renders" pattern
# already used everywhere else in this app, just applied to login state.
def account_widget(extra_links="", menu_class="", guest_plain=False):
    """The one "who's logged in" component — an avatar circle, an
    email that truncates with an ellipsis instead of wrapping/
    overflowing, and a click-to-toggle dropdown (which also holds the
    dark-mode switch, right above Sign out — see THEME_INIT_SCRIPT and
    STYLE's :root[data-theme="dark"] for the other half of how that
    actually changes anything). Used by both app_shell() (the sidebar
    footer) and top_nav() (every public page) instead of two
    separately-implemented, differently-behaved ones: the sidebar's
    original version already truncated a long email correctly;
    top_nav()'s original version (a native <details>/<summary>)
    didn't, and wrapped its own dropdown caret onto its own line once
    the email got long. This is that one, not a third
    implementation — call sites differ only in the three params below.

    extra_links: top_nav()'s call passes the real navigation links
    (Profile, Flagged bills, Clients, Disclosures) the dropdown needs
    on public pages, which have no sidebar to put them in otherwise;
    app_shell()'s call leaves this blank since sidebar pages already
    show those as persistent nav items elsewhere on the page.

    menu_class: positions the dropdown. Blank (app_shell()'s call)
    keeps the base .app-account-menu rule in STYLE, which opens
    upward from a bottom-anchored sidebar footer. top_nav()'s call
    passes "top-anchored" (see that modifier in STYLE) since a public
    page's account button sits in a top bar, not a sidebar footer, and
    needs the dropdown to open downward instead.

    guest_plain: the logged-out Sign in/Sign up pair. False (the
    sidebar's call) renders them as the .secondary/.primary button
    pair the sidebar footer's always shown — a real call-to-action
    area with room for two boxed buttons. True (top_nav()'s call)
    renders plain, unclassed <a> tags instead, which pick up the
    existing .top-nav a rule in STYLE for free — matching how "Sign
    in"/"Sign up" always looked on public pages before this component
    existed, since two solid pill buttons read as too heavy sitting
    next to the rest of the top bar's plain text links."""
    if guest_plain:
        guest_links = '<a href="/login">Sign in</a>\n  <a href="/signup">Sign up</a>'
    else:
        guest_links = '<a href="/login" class="secondary">Sign in</a>\n  <a href="/signup" class="primary">Sign up</a>'
    return f"""
<div class="app-account-guest{' plain-links' if guest_plain else ''}" id="shell-guest">
  {guest_links}
</div>
<button type="button" class="app-account" id="shell-account-btn" style="display:none" aria-haspopup="true" aria-expanded="false">
  <span class="app-avatar" id="shell-avatar">&nbsp;</span>
  <span class="app-account-email" id="shell-email">&nbsp;</span>
  <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" style="color:var(--slate);flex:none">
    <path d="M3 4.5L6 7.5L9 4.5" stroke-linecap="round"/>
  </svg>
</button>
<div class="app-account-menu {menu_class}" id="shell-account-menu">
  {extra_links}
  <button type="button" class="theme-toggle-row" id="theme-toggle-btn" role="switch" aria-checked="false">
    <span>Dark mode</span>
    <span class="theme-toggle-track"><span class="theme-toggle-thumb"></span></span>
  </button>
  <button type="button" id="shell-signout-btn">Sign out</button>
</div>
<script>
(function() {{
  fetch('/api/me').then(r => r.json()).then(me => {{
    if (!me.logged_in) {{
      document.getElementById('shell-guest').style.display = 'flex';
      return;
    }}
    const email = me.email || '';
    document.getElementById('shell-email').textContent = email;
    document.getElementById('shell-avatar').textContent = email.slice(0, 2).toUpperCase();
    document.getElementById('shell-account-btn').style.display = '';
  }}).catch(() => {{
    document.getElementById('shell-guest').style.display = 'flex';
  }});

  const acctBtn = document.getElementById('shell-account-btn');
  const acctMenu = document.getElementById('shell-account-menu');
  // aria-expanded on the trigger mirrors the .show class so screen readers
  // know this is a disclosure control and whether it's currently open —
  // sighted-only before, since only the CSS class changed.
  const setAcctMenuOpen = (open) => {{
    acctMenu.classList.toggle('show', open);
    acctBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }};
  acctBtn.addEventListener('click', () => setAcctMenuOpen(!acctMenu.classList.contains('show')));
  document.addEventListener('click', (e) => {{
    if (!acctBtn.contains(e.target) && !acctMenu.contains(e.target)) setAcctMenuOpen(false);
  }});
  // Escape closes the menu and returns focus to the button that opened it,
  // matching the standard disclosure-menu keyboard pattern.
  document.addEventListener('keydown', (e) => {{
    if (e.key === 'Escape' && acctMenu.classList.contains('show')) {{
      setAcctMenuOpen(false);
      acctBtn.focus();
    }}
  }});
  document.getElementById('shell-signout-btn').addEventListener('click', async () => {{
    await fetch('/api/logout', {{ method: 'POST' }});
    window.location.href = '/';
  }});

  // Dark-mode toggle — data-theme on <html> is what STYLE's
  // :root[data-theme="dark"] / :not([data-theme="light"]) rules
  // actually key off; THEME_INIT_SCRIPT (in every page's own <head>)
  // is what applies a stored choice on the NEXT page load, before
  // this script (or anything else) even runs, so a click here only
  // has to handle updating the CURRENT page plus saving the choice.
  const themeToggle = document.getElementById('theme-toggle-btn');
  const isDarkNow = () => {{
    const explicit = document.documentElement.getAttribute('data-theme');
    if (explicit === 'dark') return true;
    if (explicit === 'light') return false;
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }};
  const syncThemeToggle = () => themeToggle.setAttribute('aria-checked', isDarkNow() ? 'true' : 'false');
  syncThemeToggle();
  themeToggle.addEventListener('click', () => {{
    const next = isDarkNow() ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    syncThemeToggle();
  }});
}})();
</script>
"""


# The real Rotunda logo — a stylized capitol/rotunda silhouette (dome,
# entablature bar, four columns, base) built as inline SVG rather than
# an <img>, so it recolors via CSS the same way every other icon in
# this app already does instead of needing separate light/dark raster
# exports (the mockup's own logo is a flat white-on-black PNG, so this
# keeps that same shape but as a theme-aware mask instead of adopting
# the PNG and the static-asset-serving it would need). .brand-mark is
# a sized, empty element that masks this shape and fills it with --ink
# (see .brand-mark's own CSS) — --ink already flips per theme on its
# own, so unlike a two-PNG version this needs only ONE definition here,
# not three (:root, the dark media query, and [data-theme="dark"]).
# Four columns (was three) to match the mockup's own mark.
BRAND_MARK_SVG = """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <path d="M20 42 A30 30 0 0 1 80 42" fill="none" stroke="#000" stroke-width="10"/>
  <rect x="8" y="47" width="84" height="7"/>
  <rect x="18" y="59" width="9" height="27"/>
  <rect x="35" y="59" width="9" height="27"/>
  <rect x="52" y="59" width="9" height="27"/>
  <rect x="69" y="59" width="9" height="27"/>
  <rect x="14" y="91" width="72" height="7"/>
</svg>"""
BRAND_MARK_SVG_B64 = base64.b64encode(BRAND_MARK_SVG.encode("utf-8")).decode("ascii")


# STYLE is a plain (non-f) string -- see its own definition above for why
# -- so this swaps in via .replace() on the placeholder token rather than
# f-string interpolation. Has to happen down here, after the constant
# above exists, not right after STYLE's own definition.
STYLE = STYLE.replace("__BRAND_MARK_SVG_B64__", BRAND_MARK_SVG_B64)

# The shared page JS, extracted (2026-09) out of this module into
# static/js/ — it was ~500 lines of JavaScript, plus its explanatory
# comments, sitting in Python strings here. Each file's own header
# comment says what it does and which pages share it.
#
# Each one is served as a real script file and pulled in with a
# <script src="..."> tag ahead of the page's own inline <script> (see
# the *_SRC constants below), rather than being pasted into the page
# body the way it used to be. Two reasons: a browser now fetches each
# of these once for a whole session instead of re-reading an identical
# copy embedded into every page that shares it, and those explanatory
# comments stay a developer-facing thing on disk instead of shipping to
# the browser inside every page response.
#
# Safe as separate classic scripts because each one is self-contained:
# top-level function/const declarations plus a couple of document-level
# listeners, nothing that reads a page's own variables at load time. A
# classic <script src> runs before the inline <script> that follows it,
# and top-level declarations land in the same shared global scope they
# used to share by being pasted into that block — so the page code
# calling them sees exactly what it saw before.
#
# Reading them from files also retires a hazard the old form had: each
# constant had to stay a plain, non-f string, because these are full of
# JS template literals whose ${...} would otherwise collide with
# Python's own interpolation. A file has no such rule.
BILL_TABLES_JS = _read_static_text("js/bill_tables.js")
HEARING_TIME_JS = _read_static_text("js/hearing_time.js")
BILL_STATUS_JS = _read_static_text("js/bill_status.js")
POSITION_HISTORY_JS = _read_static_text("js/position_history.js")
TOAST_JS = _read_static_text("js/toast.js")
BILL_CLIENTS_JS = _read_static_text("js/bill_clients.js")
CLIENT_QUICKADD_JS = _read_static_text("js/client_quickadd.js")
CONFIRM_DELETE_JS = _read_static_text("js/confirm_delete.js")
TITLE_CASE_JS = _read_static_text("js/title_case.js")
ROW_MENU_JS = _read_static_text("js/row_menu.js")
PAGE_PROGRESS_JS = _read_static_text("js/page_progress.js")


# Every file the /static/ route will serve, name -> (bytes, content type),
# built once at import rather than read per request. STYLE has to be in
# here below the .replace() above, not next to its own load, so the bytes
# served are the ones with the brand mark already substituted in.
STATIC_ASSETS = {
    "style.css": (STYLE.encode("utf-8"), "text/css; charset=utf-8"),
    "js/bill_tables.js": (BILL_TABLES_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/hearing_time.js": (HEARING_TIME_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/bill_status.js": (BILL_STATUS_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/position_history.js": (POSITION_HISTORY_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/toast.js": (TOAST_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/bill_clients.js": (BILL_CLIENTS_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/client_quickadd.js": (CLIENT_QUICKADD_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/confirm_delete.js": (CONFIRM_DELETE_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/title_case.js": (TITLE_CASE_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/row_menu.js": (ROW_MENU_JS.encode("utf-8"), JS_CONTENT_TYPE),
    "js/page_progress.js": (PAGE_PROGRESS_JS.encode("utf-8"), JS_CONTENT_TYPE),
}

STYLE_HREF = _asset_url("style.css")
BILL_TABLES_SRC = _asset_url("js/bill_tables.js")
HEARING_TIME_SRC = _asset_url("js/hearing_time.js")
BILL_STATUS_SRC = _asset_url("js/bill_status.js")
POSITION_HISTORY_SRC = _asset_url("js/position_history.js")
TOAST_SRC = _asset_url("js/toast.js")
BILL_CLIENTS_SRC = _asset_url("js/bill_clients.js")
CLIENT_QUICKADD_SRC = _asset_url("js/client_quickadd.js")
CONFIRM_DELETE_SRC = _asset_url("js/confirm_delete.js")
TITLE_CASE_SRC = _asset_url("js/title_case.js")
ROW_MENU_SRC = _asset_url("js/row_menu.js")
PAGE_PROGRESS_SRC = _asset_url("js/page_progress.js")

TOP_BRAND = """<a href="/" class="top-brand">
  <span class="brand-mark" style="width:17px;height:17px"></span>
  Rotunda
</a>"""


# The extra links account_widget()'s dropdown needs on a public page
# (which has no sidebar to put them in otherwise) — see top_nav()
# below and account_widget()'s own docstring for why the sidebar's
# call doesn't pass these.
TOP_NAV_ACCOUNT_LINKS = """
  <a href="/profile">View profile</a>
  <a href="/flagged">My flagged bills</a>
  <a href="/clients">Clients</a>
  <a href="/disclosures">Disclosure forms</a>
"""


def top_nav(current, left_extra="", show_account_menu=True):
    """The full top-nav bar: the brand mark, the 3-page links (or a
    custom left_extra, e.g. signup's "Skip for now"), plus the account
    widget (see account_widget()) pushed to the right via its own
    wrapper's margin-left:auto. Meant to sit directly in <body>,
    outside .wrap — it's a full-width bar, not part of the centered
    content column.

    show_account_menu=False drops the account widget entirely — used
    by /login and /signup, whose own left_extra already gives a
    logged-out visitor the one way to switch between them ("Log in →"
    / "Sign up →"). Without this, those two pages would show that link
    on the left AND a second, JS-filled "Sign in / Sign up" pair on
    the right — including, on /login itself, a "Sign in" link back to
    the page you're already on."""
    left = left_extra if left_extra else nav_links(current)
    # max-width, not just margin-left:auto — .app-account's own
    # width:100% (see STYLE) needs something bounded to be 100% OF;
    # the sidebar gets that for free from its fixed-width aside, but
    # a top-nav bar has no such constraint on its own, which is
    # exactly why the email never actually truncated here before —
    # it just kept growing and pushed the bar's layout instead.
    account = (
        f'<div style="margin-left:auto;position:relative;max-width:14rem">'
        f'{account_widget(TOP_NAV_ACCOUNT_LINKS, "top-anchored", guest_plain=True)}</div>'
        if show_account_menu else ""
    )
    # <nav>, not <div> — public pages (landing/signup/login/profile, the
    # only callers of top_nav()) otherwise had no navigation landmark at
    # all for screen-reader users to jump to; the .top-nav class and its
    # styling are unaffected since STYLE targets the class, not the tag.
    #
    # {left} and {account} used to sit directly in .top-nav-inner's own
    # row — fine at desktop widths, but with no wrap/collapse logic at
    # all, that row (brand + nav_links()/left_extra + the account
    # widget) simply ran wider than a phone screen and dragged the
    # *whole page* into horizontal scroll (confirmed: 521px of content
    # in a 375px viewport). .top-nav-links below wraps them together —
    # `display: contents` at desktop widths so it's invisible to layout
    # (children still lay out directly in .top-nav-inner's flex row,
    # unchanged from before), collapsing into a hamburger-triggered
    # dropdown panel only below STYLE's mobile breakpoint (see that
    # media query). The toggle script mirrors account_widget()'s own
    # open/close/Escape/click-outside dropdown, plus returning focus to
    # the button on Escape.
    return f"""<nav class="top-nav" aria-label="Main"><div class="top-nav-inner">{TOP_BRAND}
  <button type="button" class="icon-btn top-nav-menu-btn" id="top-nav-menu-btn" aria-label="Open menu" aria-haspopup="true" aria-expanded="false" aria-controls="top-nav-links">
    <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h10M2 7h10M2 10h10" stroke-linecap="round"/></svg>
  </button>
  <div class="top-nav-links" id="top-nav-links">{left}{account}</div>
</div></nav>
<script>
(function() {{
  const btn = document.getElementById('top-nav-menu-btn');
  const links = document.getElementById('top-nav-links');
  const setOpen = (open) => {{
    links.classList.toggle('show', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }};
  btn.addEventListener('click', () => setOpen(!links.classList.contains('show')));
  document.addEventListener('click', (e) => {{
    if (!btn.contains(e.target) && !links.contains(e.target)) setOpen(false);
  }});
  document.addEventListener('keydown', (e) => {{
    if (e.key === 'Escape' && links.classList.contains('show')) {{
      setOpen(false);
      btn.focus();
    }}
  }});
}})();
</script>"""


# ── Sidebar app shell — for signed-in pages only, rolled out one page
# at a time rather than all 13 templates at once. Public pages (lookup,
# login, signup, lobbying search) keep top_nav() + .wrap above; a
# sidebar pointing at pages you can't use yet doesn't make sense before
# you're signed in. /flagged is the first page moved over — /clients,
# /disclosures, /profile, /report, and /clients/detail follow later.
#
# Grouped/accordion structure (Bills / Lobbying Activity / Draft),
# matching the mockup's own nav grouping — was a flat 5-item list
# before this redesign. Each group is (label, icon, [(href, label,
# icon), ...]); app_shell() below expands whichever group contains
# `current` by default. The mockup also shows a top-level "home" item
# and a "draft > position letters" child — both omitted here since
# neither has a real page behind it yet (no dashboard/home route, no
# position-letter drafting feature); "draft" only has one real child
# (Disclosures/Form 601 prep) until position letters becomes a real
# feature.
SHELL_NAV_ITEMS = [
    # Each entry is (label, icon, target). A target that's a *list* is a
    # collapsible group of children, which is what every entry was until
    # now; a target that's a plain *string* is a flat top-level link with
    # no children (see app_shell's render_item). Dashboard is the only
    # flat one — it has nothing to group under it, and burying the app's
    # landing page one click inside an accordion would defeat the point
    # of making it the landing page.
    ("Dashboard",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<rect x="1.75" y="1.75" width="4.5" height="5.5" rx="1"/><rect x="7.75" y="1.75" width="4.5" height="3.5" rx="1"/>'
     '<rect x="1.75" y="8.75" width="4.5" height="3.5" rx="1"/><rect x="7.75" y="6.75" width="4.5" height="5.5" rx="1"/></svg>',
     "/dashboard"),
    ("Bills",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<rect x="3" y="1.5" width="8" height="11" rx="1"/><path d="M5 4.5h4M5 7h4M5 9.5h2.5" stroke-linecap="round"/></svg>',
     [
         # Was two separate items ("Lookup" + "Discover") until the two
         # pages merged into one search experience — see LOOKUP_BODY.
         ("/lookup", "Bill lookup",
          '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
          '<circle cx="6" cy="6" r="4"/><path d="M9.5 9.5L12.5 12.5" stroke-linecap="round"/></svg>'),
         ("/flagged", "Flagged bills",
          '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
          '<path d="M2 1v12M2 2h8l-2 2.5L10 7H2" stroke-linejoin="round"/></svg>'),
     ]),
    ("Lobbying Activity",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<path d="M2 13V6l5-4 5 4v7" stroke-linejoin="round"/><path d="M5.5 13V8h3v5"/></svg>',
     [
         ("/lobbying", "Search",
          '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
          '<circle cx="6" cy="6" r="4"/><path d="M9.5 9.5L12.5 12.5" stroke-linecap="round"/></svg>'),
         ("/clients", "Clients",
          '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
          '<circle cx="5.5" cy="4.5" r="2.5"/><path d="M1 12c0-2.5 2-4.2 4.5-4.2S10 9.5 10 12" stroke-linecap="round"/></svg>'),
         ("/directory", "Capitol directory",
          '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
          '<rect x="2.5" y="1.5" width="9" height="11" rx="1"/><path d="M2.5 4.5h-1M2.5 7h-1M2.5 9.5h-1" stroke-linecap="round"/>'
          '<circle cx="7" cy="6" r="1.4"/><path d="M4.8 10.2c0-1.2 1-2 2.2-2s2.2.8 2.2 2" stroke-linecap="round"/></svg>'),
     ]),
    ("Draft",
     '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
     '<path d="M2.3 11.7l1.8-.4L11 4.4a1 1 0 000-1.4l-.9-.9a1 1 0 00-1.4 0L1.8 9l-.4 1.8z"/></svg>',
     [
         ("/draft/letters", "Letters",
          '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
          '<rect x="1.5" y="3" width="11" height="8" rx="1"/>'
          '<path d="M1.9 3.6L7 7.8l5.1-4.2" stroke-linejoin="round"/></svg>'),
         ("/disclosures", "Disclosures",
          '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">'
          '<rect x="3" y="1.5" width="8" height="11" rx="1"/>'
          '<path d="M5.2 6l1 1 2.2-2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>'),
     ]),
]


def app_shell(current, body):
    """Sidebar + topbar chrome shared by every real page in the product.
    `body` is that page's own already-built inner HTML — its own
    heading, controls, table, script, whatever it needs — just wrapped
    in the shell. The topbar itself shows only today's date — real,
    page-agnostic content worth keeping — rather than a page title;
    it used to also show a static "Overview" label above that date on
    every single page regardless of what page it was, which just
    duplicated (and, everywhere but the actual overview, contradicted)
    the page's own real heading a few pixels below. The page's actual
    title/description lives inside `body` as a .page-head, paired with
    that page's own controls (see FLAGGED_BODY).

    Most pages that call this have already 302'd to /login server-side
    if there's no session, so for them the /api/me fetch below isn't an
    access check — it only learns which email to show in the sidebar
    footer. /lookup and /lobbying are the exceptions: they render this
    same shell without requiring a session at all (see the module
    docstring's "free to look up a bill, no account needed" promise),
    so /api/me can genuinely come back logged_in: false here — in which
    case the sidebar footer swaps to Sign in/Sign up (#shell-guest)
    instead of an avatar and a "Sign out" button that wouldn't do
    anything. Sidebar links to account-gated pages (Flagged bills,
    Clients, Profile, ...) still just 302 a logged-out visitor to
    /login if they click one — same as always."""
    # data-nav carries the plain href per item (same value `current` gets
    # compared against below) so a page like /report — whose real nav
    # context depends on where the visitor came from, not on the fixed
    # `current` this whole shell was built with at import time — can find
    # and re-target the right <li> client-side instead of parsing link
    # text. See REPORT_BODY's own script for the one place that happens
    # (and this function's own script below, which expands a group when
    # that re-targeting lands on one of its children).
    def render_group(label, icon, children):
        has_current = any(href == current for href, _, _ in children)
        child_html = "".join(
            f'<li><a href="{href}" data-nav="{href}" class="side-nav-item child{" active" if href == current else ""}">{c_icon}{c_label}</a></li>'
            for href, c_label, c_icon in children
        )
        caret = ('<span class="nav-caret"><svg width="9" height="9" viewBox="0 0 24 24" fill="none" '
                 'stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M8 5l8 7-8 7"/></svg></span>')
        return (
            f'<li class="nav-group{" open" if has_current else ""}">'
            f'<button type="button" class="side-nav-item side-nav-parent" aria-expanded="{"true" if has_current else "false"}">'
            f'{icon}<span>{label}</span>{caret}</button>'
            f'<ul class="nav-subitems">{child_html}</ul></li>'
        )
    def render_item(label, icon, target):
        """A flat link (string target) or a collapsible group (list of
        children) — see SHELL_NAV_ITEMS. The flat branch needs no CSS of
        its own: .side-nav-item without .child is already the top-level
        row shape .side-nav-parent borrows, minus the caret."""
        if isinstance(target, str):
            active = " active" if target == current else ""
            return (f'<li><a href="{target}" data-nav="{target}" class="side-nav-item{active}">'
                    f'{icon}<span>{label}</span></a></li>')
        return render_group(label, icon, target)

    nav_html = "".join(render_item(label, icon, target) for label, icon, target in SHELL_NAV_ITEMS)
    profile_active = " active" if current == "/profile" else ""
    return f"""
<div class="app-shell">
  <div class="app-sidebar-backdrop" id="shell-sidebar-backdrop"></div>
  <aside class="app-sidebar" id="shell-sidebar">
    <div class="app-brand">
      <span class="app-brand-mark">
        <span class="brand-mark" style="width:20px;height:20px"></span>
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
      {account_widget()}
    </div>
  </aside>
  <div class="app-body">
    <header class="app-topbar">
      <div style="display:flex;align-items:center;gap:1rem">
        <button type="button" class="icon-btn app-topbar-menu-btn" id="shell-menu-btn" aria-label="Open navigation" aria-haspopup="true" aria-expanded="false" aria-controls="shell-sidebar">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h10M2 7h10M2 10h10" stroke-linecap="round"/></svg>
        </button>
        <div class="app-topbar-sub" id="shell-date"></div>
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

  // Each Bills/Lobbying Activity/Draft group starts open only if it
  // holds `current` (see render_group() above); clicking its parent
  // button just toggles its own .nav-group, independent of the others.
  document.querySelectorAll('.side-nav-parent').forEach((btn) => {{
    btn.addEventListener('click', () => {{
      const group = btn.closest('.nav-group');
      const open = !group.classList.contains('open');
      group.classList.toggle('open', open);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }});
  }});

  // Sidebar footer's account button/menu (shell-guest/shell-account-btn/
  // shell-account-menu/shell-signout-btn) are wired by account_widget()'s
  // own <script>, not here — see the {{account_widget()}} call above.

  // Below 900px the sidebar becomes an off-canvas drawer (see STYLE's
  // .app-sidebar media query) opened by this hamburger button — same
  // open/close/Escape/click-outside shape as account_widget()'s own
  // dropdown, plus a backdrop (since this covers the whole screen, not
  // a small anchored menu) and a basic focus trap/return-focus, since
  // this one hides real navigation behind it rather than a few extra
  // links.
  const sidebar = document.getElementById('shell-sidebar');
  const backdrop = document.getElementById('shell-sidebar-backdrop');
  const menuBtn = document.getElementById('shell-menu-btn');
  const setSidebarOpen = (open) => {{
    sidebar.classList.toggle('show', open);
    backdrop.classList.toggle('show', open);
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.body.style.overflow = open ? 'hidden' : '';
    if (open) {{
      const firstFocusable = sidebar.querySelector('a, button');
      if (firstFocusable) firstFocusable.focus();
    }}
  }};
  menuBtn.addEventListener('click', () => setSidebarOpen(!sidebar.classList.contains('show')));
  backdrop.addEventListener('click', () => setSidebarOpen(false));
  document.addEventListener('keydown', (e) => {{
    if (!sidebar.classList.contains('show')) return;
    if (e.key === 'Escape') {{
      setSidebarOpen(false);
      menuBtn.focus();
      return;
    }}
    if (e.key === 'Tab') {{
      const focusable = sidebar.querySelectorAll('a, button');
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {{ e.preventDefault(); last.focus(); }}
      else if (!e.shiftKey && document.activeElement === last) {{ e.preventDefault(); first.focus(); }}
    }}
  }});
}})();
</script>
"""


def page(title, path, body):
    """The 13-line <!doctype html>...<link rel="stylesheet" href="{STYLE_HREF}">...<body>
    {app_shell(path, body)}...</html> skeleton every signed-in-shell
    page below used to repeat verbatim — only title/path/body ever
    differed between them. One place to edit the skeleton itself (a
    new meta tag, a different STYLE variable) instead of the same edit
    copied into 13 constants by hand.

    Deliberately NOT used by the handful of pages that build their
    <body> some other way — SIGNUP_PAGE/LOGIN_PAGE/PROFILE_PAGE call
    top_nav() with page-specific left_extra/show_account_menu args, not
    app_shell() (collapsing those would mean restructuring how each one
    builds its body, not just deduping this wrapper); LANDING_PAGE
    doesn't use either helper at all — it's a standalone full-bleed
    splash with no shared nav chrome (see LANDING_STYLE's own
    comment). Those four are also the ones page_progress.js's bar skips
    — one-time or low-frequency visits, not the daily-workflow page
    transitions P2-32 was about."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{STYLE_HREF}">
{FONT_LINKS}
{THEME_INIT_SCRIPT}
</head>
<body>
<div id="page-progress"></div>
<script src="{PAGE_PROGRESS_SRC}"></script>
{app_shell(path, body)}
</body>
</html>
"""


# The marketing homepage at "/" — replaced (2026-08 redesign) with the
# mockup's own minimal splash screen (Rotunda Landing.dc.html): just
# the wordmark, brand mark, tagline, and a "get started" link, in place
# of the previous full hero/feature-grid/workflow/trust/footer page —
# a deliberate content cut the user confirmed, not an oversight.
# Unlike every other page in this file, LANDING_STYLE hardcodes its own
# colors rather than reading STYLE's --bg/--ink tokens: the mockup
# itself has no light-mode variant for this screen (compare Rotunda
# Dashboard.dc.html, which explicitly supports both) — it's always
# this one dark look, regardless of the visitor's system/toggle
# preference. Reuses the masked brand-mark shape (--brand-mark, see
# BRAND_MARK_SVG above) instead of the mockup's own raster PNG logo —
# same reasoning as the sidebar's app-brand-mark: recolors for free, no
# static-asset route needed for a redesign this size. "get started"
# goes to /signup, which already offers "← Lookup"/"Log in →" links of
# its own (see SIGNUP_PAGE) — this splash doesn't need its own chrome
# to keep those reachable.
LANDING_STYLE = """
  .splash {
    position: relative; overflow: hidden; min-height: 100svh; background: #000; color: #fff;
    display: flex; flex-direction: column;
  }
  .splash a { color: #fff; }
  .splash a:hover { color: #F4EFE4; }
  .splash ::selection { background: #F4EFE4; color: #000; }
  /* Four faint decorative layers, all pointer-events:none and purely
     cosmetic — a dotted-grid swatch in two corners, three concentric
     ring outlines bleeding off the top-left/bottom-right, and one soft
     radial glow low-center. Pixel values ported directly from the
     mockup rather than converted to rem, since these are one-off
     decorative shapes tied to this exact screen, not reused anywhere
     else that would benefit from rem's user-font-size scaling. */
  .splash-dots {
    position: absolute; pointer-events: none;
    background-image: radial-gradient(rgba(255,255,255,0.2) 2px, transparent 2.2px);
    background-size: 33px 33px;
  }
  .splash-dots.tl { top: -8px; left: 0; width: 264px; height: 194px; background-position: 22px 22px; }
  .splash-dots.br { bottom: -10px; right: -10px; width: 372px; height: 190px; }
  .splash-ring { position: absolute; border-radius: 50%; pointer-events: none; }
  .splash-ring.r1 { top: -17vmax; left: -15vmax; width: 40vmax; height: 40vmax; max-width: 560px; max-height: 560px; border: 1px solid rgba(255,255,255,0.28); }
  .splash-ring.r2 { top: -10vmax; left: -26vmax; width: 32vmax; height: 32vmax; max-width: 450px; max-height: 450px; border: 1px solid rgba(255,255,255,0.22); }
  .splash-ring.r3 { bottom: -22vmax; right: -10vmax; width: 47vmax; height: 47vmax; max-width: 660px; max-height: 660px; border: 1px solid rgba(255,255,255,0.26); }
  .splash-glow {
    position: absolute; bottom: -190px; left: 50%; transform: translateX(-50%); width: 460px; height: 330px;
    border-radius: 50%; background: radial-gradient(closest-side, rgba(255,255,255,0.2), rgba(255,255,255,0)); pointer-events: none;
  }
  .splash-topline { position: relative; padding: clamp(28px, 5vh, 56px) clamp(28px, 4vw, 56px) 0; flex: none; }
  .splash-topline div { height: 1px; background: rgba(255,255,255,0.6); }
  .splash-main {
    position: relative; flex: 1; display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: clamp(16px, 4vh, 40px) 24px 0; text-align: center;
  }
  .splash-word {
    margin: 0; font-size: clamp(34px, 4.6vw, 62px); font-weight: 700; letter-spacing: 0.3em;
    line-height: 1; text-indent: 0.3em;
  }
  .splash-mark {
    display: inline-block; width: clamp(140px, 18vw, 250px); height: clamp(140px, 18vw, 250px);
    margin: clamp(14px, 2.6vh, 24px) 0 0; background-color: #fff;
    -webkit-mask-image: var(--brand-mark); mask-image: var(--brand-mark);
    -webkit-mask-size: contain; mask-size: contain;
    -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat;
    -webkit-mask-position: center; mask-position: center;
  }
  .splash-tagline {
    margin: clamp(12px, 2.2vh, 20px) 0 0; font-size: clamp(11px, 1.05vw, 14px); font-weight: 400;
    letter-spacing: 0.22em; text-transform: uppercase; color: rgba(255,255,255,0.9);
  }
  .splash-cta {
    position: relative; flex: none; display: flex; flex-direction: column; align-items: center; gap: 10px;
    padding: clamp(20px, 4vh, 40px) 24px clamp(32px, 8vh, 72px);
  }
  .splash-cta a { font-size: 19px; font-weight: 400; letter-spacing: 0.01em; padding: 4px 8px; }
  .splash-chevrons { display: flex; flex-direction: column; align-items: center; animation: splash-chevron 1.9s ease-in-out infinite; }
  .splash-chevrons svg:last-child { margin-top: -3px; }
  @keyframes splash-chevron { 0%, 100% { transform: translateY(0); opacity: 0.75; } 50% { transform: translateY(5px); opacity: 1; } }
  @media (prefers-reduced-motion: reduce) { .splash-chevrons { animation: none; } }
"""

LANDING_PAGE = _render_template(
    "landing_page.html",
    STYLE_HREF=STYLE_HREF,
    LANDING_STYLE=LANDING_STYLE,
    FONT_LINKS=FONT_LINKS,
)


# The body for /lookup, wrapped in app_shell() below rather than
# top_nav()+.wrap — this is one of the two pages (with /lobbying) that
# render the full signed-in shell without requiring a session, so it
# feels like part of the same product whether you found it from the
# marketing homepage or clicked "Lookup" in your own sidebar. See
# app_shell()'s docstring for how the sidebar footer handles being
# logged out here.
#
# Merged with what used to be the separate /discover page — one search
# box now handles both "I know the exact bill number" and "I don't,
# just what it's about" (see smart_search() in legiscan_client.py,
# behind the new /api/search), and results always render as a list —
# even a single exact-number hit — instead of this page rendering full
# bill detail inline the way it used to for a bill-number match.
# Clicking any result goes to /report instead (see REPORT_BODY, which
# now also carries the flag-confirmation modal this page used to have,
# since /report is the one place bill detail — and now flagging too —
# actually lives). /discover itself now just redirects here.
#
# Deliberately no .stat-grid/.stat-card row here, unlike FLAGGED_BODY/
# CLIENTS_BODY/DISCLOSURES_BODY. Every existing stat-card pulls
# per-account numbers (flagged bill count, client count, drafts
# awaiting sign-off) from session-gated /api/... endpoints that 401 a
# logged-out caller. /lookup is one of exactly two pages in the app
# that render without a session at all (see the comment above and
# app_shell()'s own docstring) — it's the "free to look up any bill,
# no account needed" front door, not just a page a logged-in user
# happens to also use. A stats row here would either 401-redirect a
# guest straight to /login the moment its JS ran (breaking that
# promise) or have to hide itself for guests, which is the majority of
# this page's actual traffic — a tile row that's invisible for the
# primary audience isn't a real feature, it's dead weight for everyone
# else. There's also no honest per-account number to show a guest in
# the first place: "bills flagged" is meaningless before they have an
# account, and a client-side "bills looked up this session" counter
# would be fake, unpersisted state, not a real server-backed metric.
# The one number this page actually surfaces — how many bills matched
# a given search — is already shown inline in renderResults() below
# ("N bills match — showing page X of Y"); a real "bills tracked"
# total belongs to /flagged, where it's already one of that page's
# stat-cards.
LOOKUP_BODY = _render_template(
    "lookup_body.html",
    BILL_STATUS_SRC=BILL_STATUS_SRC,
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
    POSITION_HISTORY_SRC=POSITION_HISTORY_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
    TOAST_SRC=TOAST_SRC,
    top_nav=''.join(('<div class="skeleton-row">\n      <div class="skeleton-bar" style="width:10%"></div>\n      <div class="skeleton-bar" style="width:45%"></div>\n      <div class="skeleton-bar" style="width:14%"></div>\n      <div class="skeleton-bar" style="width:12%"></div>\n    </div>' for _ in range(3))),
)

PAGE = page("Look up a bill — Rotunda", "/lookup", LOOKUP_BODY)


# Same reasoning as LOOKUP_BODY above — /lobbying is the other page that
# renders the shell without a session.
#
# Same reasoning as LOOKUP_BODY above for the missing .stat-grid too,
# plus one more reason specific to this page: the natural-seeming
# metric a stat row would want here — "your clients cross-referenced
# against CAL-ACCESS" — is per-account (it means *your firm's* client
# roster matched against entities), fetched via /api/clients, which is
# just as session-gated as /api/flagged and just as unavailable to the
# guests this public search is built for. That number also isn't
# genuinely homeless without this page: it's the client roster
# CLIENTS_BODY already owns and displays, so restating it here would
# be either a broken tile for guests or a redundant one for logged-in
# users looking at a page whose actual job is anonymous org lookup,
# not account state. The result-count messaging renderResults() below
# already prints (including the truncated-at-50 note) is this page's
# honest equivalent of a "total" — there's no second, more meaningful
# number sitting unused behind it.
def _skeleton_rows(count, widths=(32, 14, 18, 10)):
    """`count` rows of content-shaped grey bars — the loading-state
    building block LOBBYING_BODY introduced first (below), rolled out
    further as P2-32's fix for "no skeleton... on every list and detail
    view": the layout the real content will occupy is visible the
    instant the page's own HTML arrives, rather than a spinner that
    gives no hint of what's coming or how much of it."""
    row = "".join(f'<div class="skeleton-bar" style="width:{w}%"></div>' for w in widths)
    return "".join(f'<div class="skeleton-row">{row}</div>' for _ in range(count))


def _skeleton_panel(rows=3, row_widths=(60, 35, 45)):
    """One .panel-shaped placeholder: a title-width bar in the head, then
    `rows` skeleton rows — what DASHBOARD_BODY's four panels use below."""
    return (
        '<div class="panel"><div class="panel-head">'
        '<div class="skeleton-bar" style="width:40%;height:1rem;margin:0"></div>'
        f'</div>{_skeleton_rows(rows, row_widths)}</div>'
    )


LOBBYING_BODY = _render_template(
    "lobbying_body.html",
    top_nav=_skeleton_rows(3),
    TITLE_CASE_SRC=TITLE_CASE_SRC,
)

LOBBYING_PAGE = page("Organization Search — Rotunda", "/lobbying", LOBBYING_BODY)


# A real destination for one organization's detail, rather than an
# in-page div appended below the whole results list — that older layout
# meant scrolling past every result to find what you clicked. Reached
# via ?id=... (a registered entity) or ?name=... (a client only ever
# named in someone else's filing, never independently registered).
LOBBYING_DETAIL_BODY = _render_template("lobbying_detail_body.html")

LOBBYING_DETAIL_PAGE = page("Organization Detail — Rotunda", "/lobbying", LOBBYING_DETAIL_BODY)


SIGNUP_PAGE = _render_template(
    "signup_page.html",
    STYLE_HREF=STYLE_HREF,
    FONT_LINKS=FONT_LINKS,
    THEME_INIT_SCRIPT=THEME_INIT_SCRIPT,
    top_nav=top_nav('/signup', left_extra='<a href="/lookup">← Lookup</a><a href="/login">Log in →</a>', show_account_menu=False),
)


LOGIN_PAGE = _render_template(
    "login_page.html",
    STYLE_HREF=STYLE_HREF,
    FONT_LINKS=FONT_LINKS,
    THEME_INIT_SCRIPT=THEME_INIT_SCRIPT,
    top_nav=top_nav('/login', left_extra='<a href="/lookup">← Lookup</a><a href="/signup">Sign up →</a>', show_account_menu=False),
)


PROFILE_PAGE = _render_template(
    "profile_page.html",
    STYLE_HREF=STYLE_HREF,
    FONT_LINKS=FONT_LINKS,
    THEME_INIT_SCRIPT=THEME_INIT_SCRIPT,
    top_nav=top_nav('/signup/profile', left_extra='<a href="/dashboard">Skip for now →</a>'),
)


PROFILE_BODY = _render_template(
    "profile_body.html",
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
)

PROFILE_VIEW_PAGE = page("Your profile — Rotunda", "/profile", PROFILE_BODY)


# The signed-in landing page (see the "/" route, which sends a logged-in
# visitor here). Everything on it is a view onto data other pages already
# own — flagged bills, hearings, prepared filings, clients — pulled
# together so the three separate deadlines this app tracks can finally be
# compared against each other in one place. One fetch, /api/dashboard;
# see db.dashboard_summary for why the aggregation is server-side.
DASHBOARD_BODY = _render_template(
    "dashboard_body.html",
    HEARING_TIME_SRC=HEARING_TIME_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
    skeleton_panels="".join(_skeleton_panel() for _ in range(4)),
)

DASHBOARD_PAGE = page("Dashboard — Rotunda", "/dashboard", DASHBOARD_BODY)


FLAGGED_BODY = _render_template(
    "flagged_body.html",
    # Bill / Next action / Status / Last change / Clients — roughly the
    # real table's own column proportions (see TABLE_HEAD in
    # flagged_body.html's own script), not just an arbitrary set of bars.
    skeleton_rows=_skeleton_rows(6, (25, 12, 10, 15, 28)),
    HEARING_TIME_SRC=HEARING_TIME_SRC,
    BILL_STATUS_SRC=BILL_STATUS_SRC,
    POSITION_HISTORY_SRC=POSITION_HISTORY_SRC,
    TOAST_SRC=TOAST_SRC,
    BILL_CLIENTS_SRC=BILL_CLIENTS_SRC,
    CLIENT_QUICKADD_SRC=CLIENT_QUICKADD_SRC,
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
    ROW_MENU_SRC=ROW_MENU_SRC,
)

FLAGGED_PAGE = page("My Flagged Bills — Rotunda", "/flagged", FLAGGED_BODY)


# Unflagging used to DELETE the flagged_bills row (and every
# bill_client_links row hanging off it); P1-16 asked for archive over
# delete, so here is the somewhere-to-see-it-and-get-it-back that fix
# needs — otherwise "archived" is invisible and indistinguishable from
# gone. Kept as current="/flagged" for the same reason CALENDAR_PAGE and
# SPONSOR_ROLLUP_PAGE are: a view onto the same set of bills, not a
# separate nav section.
ARCHIVED_BODY = _render_template(
    "archived_body.html",
    BILL_STATUS_SRC=BILL_STATUS_SRC,
    TOAST_SRC=TOAST_SRC,
    POSITION_HISTORY_SRC=POSITION_HISTORY_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
)

ARCHIVED_PAGE = page("Archived Bills — Rotunda", "/flagged", ARCHIVED_BODY)


# Every scheduled hearing across every flagged bill, in one place,
# grouped by day. Aggregation only — db.list_hearings_for_flagged_bills
# just joins tables the daily refresh job already fills in
# (refresh_watchlist.py); nothing here calls LegiScan directly. Kept as
# current="/flagged" for app_shell() (same convention REPORT_BODY
# already uses) so the sidebar still highlights Flagged Bills, since
# this is a view onto that same set of bills, not a separate section.
CALENDAR_BODY = _render_template(
    "calendar_body.html",
    HEARING_TIME_SRC=HEARING_TIME_SRC,
)

CALENDAR_PAGE = page("Hearing Calendar — Rotunda", "/flagged", CALENDAR_BODY)


# For every sponsor across a user's flagged bills: which of those
# bills they sponsored, and how each bill's own votes turned out. See
# db.list_sponsor_vote_rollup()'s docstring for why this is a rollup of
# each BILL's chamber-level vote tally, not any individual legislator's
# personal ballot — LegiScan's votes data is the former, not the
# latter, and getting the latter would mean calling LegiScan's separate
# getRollCall operation, a new integration this deliberately doesn't
# add. "Sponsor" also isn't always a person — a committee can sponsor
# a bill too (see bill_sponsors), and this list doesn't filter those
# out, matching how every other Sponsors listing in this app (e.g.
# LOOKUP_BODY's own sponsor chips) already treats them.
SPONSOR_ROLLUP_BODY = _render_template(
    "sponsor_rollup_body.html",
    BILL_STATUS_SRC=BILL_STATUS_SRC,
    POSITION_HISTORY_SRC=POSITION_HISTORY_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
)

SPONSOR_ROLLUP_PAGE = page("Sponsors &amp; Votes — Rotunda", "/flagged", SPONSOR_ROLLUP_BODY)


CLIENTS_BODY = _render_template(
    "clients_body.html",
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
    ROW_MENU_SRC=ROW_MENU_SRC,
)

CLIENTS_PAGE = page("Clients — Rotunda", "/clients", CLIENTS_BODY)


# The Capitol directory (see directory.py). Sits beside Clients rather
# than under Bills because it answers the same shape of question those
# pages do — who are the people, and how do I reach them — where the
# bill pages answer what is moving.
DIRECTORY_BODY = _render_template(
    "directory_body.html",
    TOAST_SRC=TOAST_SRC,
)

DIRECTORY_PAGE = page("Capitol directory — Rotunda", "/directory", DIRECTORY_BODY)


# One client's own page: org info, every bill assigned to them with its
# position, and a way to add a new bill starting from here rather than
# only from /flagged — the reverse direction of the existing
# flag-then-assign flow. Reached via ?id=..., e.g. from the Clients list
# or Organization Search's "+ Add as client" link.
CLIENT_DETAIL_BODY = _render_template(
    "client_detail_body.html",
    HEARING_TIME_SRC=HEARING_TIME_SRC,
    BILL_STATUS_SRC=BILL_STATUS_SRC,
    POSITION_HISTORY_SRC=POSITION_HISTORY_SRC,
    TOAST_SRC=TOAST_SRC,
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
    ROW_MENU_SRC=ROW_MENU_SRC,
)

CLIENT_DETAIL_PAGE = page("Client — Rotunda", "/clients", CLIENT_DETAIL_BODY)


# Bill report — everything about one bill in one place: current
# status, full status history, amendment history, upcoming hearings,
# and (if this signed-in user has assigned it to one of their own
# clients) that client's name and current position. Reached via
# ?bill_id=... — e.g. linked from the "Bill report" link on /flagged —
# rather than being a page anyone navigates to on its own.
#
# Called "Bill report", not "Action report": the latter is already the
# name of the generated one-page client deliverable in the product
# spec, and spending it on this screen would leave the real feature
# without a name when it ships.
REPORT_BODY = _render_template(
    "report_body.html",
    BILL_TABLES_SRC=BILL_TABLES_SRC,
    HEARING_TIME_SRC=HEARING_TIME_SRC,
    BILL_STATUS_SRC=BILL_STATUS_SRC,
    POSITION_HISTORY_SRC=POSITION_HISTORY_SRC,
    TOAST_SRC=TOAST_SRC,
    BILL_CLIENTS_SRC=BILL_CLIENTS_SRC,
    CLIENT_QUICKADD_SRC=CLIENT_QUICKADD_SRC,
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
)

REPORT_PAGE = page("Bill report — Rotunda", "/flagged", REPORT_BODY)


# Draft > Letters — the position letter a lobbyist actually hands to a
# member's office. The Draft section used to contain no drafting: its
# only child was Disclosures, so the one deliverable that justifies
# keeping position data in this app had to be written somewhere else,
# from data this app was already holding. See letter_drafts.py for what
# a new one starts out saying, and the letters table in schema.sql for
# the two boundaries: nothing is regenerated over what the user wrote,
# and nothing is sent.
LETTERS_BODY = _render_template(
    "letters_body.html",
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
    POSITION_HISTORY_SRC=POSITION_HISTORY_SRC,
    TITLE_CASE_SRC=TITLE_CASE_SRC,
)

LETTERS_PAGE = page("Letters — Rotunda", "/draft/letters", LETTERS_BODY)

LETTER_EDIT_BODY = _render_template("letter_edit_body.html")

LETTER_EDIT_PAGE = page("Letter — Rotunda", "/draft/letters", LETTER_EDIT_BODY)


# "Prepare my disclosure form" — /disclosures (pick a form, generate a
# draft, see everything you've prepared before) and /disclosures/review
# (one draft: the actual filled PDF, known-gap notes, and the sign-off
# step). This app never files anything itself — see pdf_forms.py and
# db.sign_off_prepared_filing for where that boundary is enforced.
DISCLOSURES_BODY = _render_template(
    "disclosures_body.html",
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
    ROW_MENU_SRC=ROW_MENU_SRC,
)

DISCLOSURES_PAGE = page("Disclosure Forms — Rotunda", "/disclosures", DISCLOSURES_BODY)


DISCLOSURE_REVIEW_BODY = _render_template(
    "disclosure_review_body.html",
    CONFIRM_DELETE_SRC=CONFIRM_DELETE_SRC,
)

DISCLOSURE_REVIEW_PAGE = page("Review Disclosure Form — Rotunda", "/disclosures", DISCLOSURE_REVIEW_BODY)


# Multi-word corporate-suffix phrases stripped by normalize_org_name()
# below — checked (and replaced) before punctuation is stripped, since
# some of them contain punctuation of their own ("&"). Order matters:
# the longer/more-specific phrases are listed first so e.g. "and its
# affiliates" doesn't get partially eaten by a shorter, unintended
# match first.
_ORG_SUFFIX_PHRASES = [
    "and its subsidiaries", "& its subsidiaries", "and subsidiaries",
    "and its affiliates", "& its affiliates", "and affiliates",
]
# Single-word corporate-suffix tokens dropped after splitting. Kept
# deliberately short and specific (exactly the tokens a real filing
# suffix would use) rather than any generic-sounding business word —
# stripping something broader like "company" or "group" risks folding
# two real, differently-named organizations into one canonical row.
_ORG_SUFFIX_WORDS = {"inc", "incorporated", "corp", "corporation", "llc", "lp"}


def normalize_org_name(name):
    """Canonical key for clustering near-duplicate spellings of the
    same organization (see search_lobbying()) — e.g. 'Chevron Corp &
    its subsidiaries' and 'CHEVRON CORPORATION AND ITS AFFILIATES' both
    normalize to 'chevron'. Lowercases, strips punctuation, and strips
    the specific corporate-suffix noise listed above — deliberately
    conservative, since this key decides which rows get merged into one
    display row (see search_lobbying()'s clustering, which additionally
    never merges two independently-registered entities into each other
    no matter what this returns — only unregistered "named as client
    only" mentions get grouped this way)."""
    if not name:
        return ""
    key = name.lower().replace("’", "'")
    key = re.sub(r"\bit's\b", "its", key)  # possessive typo seen in real CAL-ACCESS free text
    key = re.sub(r"\bsubsidaries\b", "subsidiaries", key)  # missing-"i" misspelling, same real CAL-ACCESS free text
    for phrase in _ORG_SUFFIX_PHRASES:
        key = key.replace(phrase, " ")
    key = re.sub(r"[^a-z0-9\s]", " ", key)
    words = [w for w in key.split() if w not in _ORG_SUFFIX_WORDS]
    return " ".join(words)


def search_lobbying(conn, q):
    """Matches BOTH the registered-entity name and the free-text
    client_name on disclosures — see the module docstring for why the
    second half matters (a client doesn't need its own registration to
    be named in someone else's filing).

    Near-duplicate spellings of the same unregistered "named as client
    only" mention (several "Amazon" variants, 20+ "Chevron..." spellings
    seen in testing — see normalize_org_name()) are clustered into one
    canonical row with the rest attached as `variants`, so the results
    list doesn't spend 20 rows on one organization. Registered entities
    (their own filer_id on file) are never merged into another row —
    each keeps its own, even if two of them happen to normalize to the
    same key — because collapsing two independently-registered
    organizations into one would misrepresent one's real filings as the
    other's, not just look tidier. Same-key registered entities are
    still visually grouped, just without merging — see
    _group_duplicate_entities()."""
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

    # One GROUP BY pass over every client_name matching this same LIKE
    # filter, instead of a separate COUNT(*)/MAX(filed_date) round trip
    # per row below (up to 40 extra queries — measured ~2.87s on this
    # table; the two LIKE scans alone take ~19ms). Same result set,
    # just computed together instead of one row at a time.
    context_by_name = {
        r["client_name"]: (r["n"], r["latest"])
        for r in conn.execute(
            """SELECT client_name, COUNT(*) AS n, MAX(filed_date) AS latest
               FROM lobbying_disclosures WHERE client_name LIKE ?
               GROUP BY client_name""",
            (like,),
        ).fetchall()
    }
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
        n, latest = context_by_name.get(name, (0, None))
        results.append({
            "kind": "client", "id": None, "name": name, "entity_type": None,
            "city": None, "state": None, "registration_status": None,
            "mention_count": n, "latest_filed": latest,
        })

    results = _cluster_client_mentions(results)
    results = _group_duplicate_entities(results)
    results.sort(key=lambda r: (r["name"] or "").lower())
    return results[:50]


def _cluster_client_mentions(results):
    """Groups unregistered `kind == "client"` rows by normalize_org_name()
    into one canonical row + a `variants` list, attaching to a same-key
    registered entity when there's exactly one unambiguous match.
    `kind == "entity"` rows always pass through unchanged and are never
    merged with each other — see search_lobbying()'s docstring for why."""
    entities_by_key = {}
    for r in results:
        if r["kind"] == "entity":
            entities_by_key.setdefault(normalize_org_name(r["name"]), []).append(r)

    client_groups = {}
    for r in results:
        if r["kind"] == "client":
            client_groups.setdefault(normalize_org_name(r["name"]), []).append(r)

    out = [r for r in results if r["kind"] == "entity"]
    for key, group in client_groups.items():
        matches = entities_by_key.get(key, [])
        if len(matches) == 1:
            # Exactly one registered entity shares this normalized name —
            # fold every mention in as that entity's variants rather than
            # showing them as their own rows.
            matches[0].setdefault("variants", []).extend(group)
        elif len(matches) > 1:
            # More than one distinct registered entity shares this
            # normalized name — no way to tell which one each mention
            # actually belongs to, so don't guess; leave them as their
            # own separate rows instead of attaching to the wrong one.
            out.extend(group)
        elif len(group) == 1:
            out.append(group[0])
        else:
            original = max(group, key=lambda r: r.get("mention_count") or 0)
            canonical = dict(original)
            canonical["variants"] = [r for r in group if r is not original]
            out.append(canonical)
    return out


def _group_duplicate_entities(results):
    """A second, separate grouping pass from _cluster_client_mentions
    above — that one only ever folds *unregistered* "named as client
    only" mentions together. This one visually clusters *registered*
    entities (each with its own real filer_id) whose names normalize to
    the same thing — e.g. the "Chevron Corporation & its subsidaries" /
    "Chevron Corporation and It's Subsidaries" / "CHEVRON CORPORATION
    AND ITS SUBSIDIARIES" trio seen in testing: three real,
    independently registered filers. Still never merged into one row
    (see search_lobbying()'s own docstring on why collapsing two real
    registrations would misrepresent one's filings as the other's) —
    this only wraps them in a `kind: "entity_group"` row carrying the
    real entities in `entities`, same "group, don't merge" shape
    _cluster_client_mentions already uses for name variants. A lone
    entity with no same-key sibling passes through unchanged."""
    entity_groups = {}
    passthrough = []
    for r in results:
        if r["kind"] == "entity":
            entity_groups.setdefault(normalize_org_name(r["name"]), []).append(r)
        else:
            passthrough.append(r)

    out = passthrough
    for group in entity_groups.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        # Alphabetically first stands in as the group row's own display
        # name — arbitrary but stable, and it (like every other real
        # name in the group) still shows up as its own entry underneath.
        group = sorted(group, key=lambda r: (r["name"] or "").lower())
        out.append({
            "kind": "entity_group", "id": None, "name": group[0]["name"],
            "entity_type": None, "city": None, "state": None,
            "registration_status": None, "entities": group,
        })
    return out


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


def _current_source_values(conn, user_id, form_type):
    """What pdf_forms would fill this form with if the draft were being
    created right now, from the firm's data as it stands today.

    Only used to answer "does this value still match where it came
    from" (see disclosure_fields.provenance_for). Nothing is written
    from it — a draft is deliberately a snapshot, and a business address
    changing in Profile must not silently rewrite a filing somebody is
    part-way through reviewing. It should say so on the field instead,
    which is exactly what this makes possible."""
    if form_type != "601":
        return {}
    profile = accounts.get_profile(conn, user_id)
    if not profile:
        return {}
    user_row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    return pdf_forms.values_for_form_601(
        profile,
        db.list_clients(conn, user_id),
        user_row["email"] if user_row else "",
        sign_off=None,
        today=datetime.date.today(),
        lobbyists=db.list_org_lobbyists(conn, user_id),
    )


def _with_disclosure_editor_meta(filing, conn, user_id):
    """Every /api/prepared-filings* response that hands back a filing
    also needs to hand back what the disclosure editor renders it with:
    the field schema (labels/kind/required), the real client-row
    AcroForm field names (pdf_forms.CLIENT_ROW_FIELDS — the frontend
    needs the exact field_data keys to bind row inputs to), and the row
    count. One helper so the six call sites can't drift out of sync
    with each other."""
    form_type = filing["form_type"]
    filing["field_schema"] = disclosure_fields.sections_for_form_type(form_type)
    filing["client_row_fields"] = pdf_forms.CLIENT_ROW_FIELDS
    filing["max_client_rows"] = pdf_forms.max_client_rows()
    # None for a form with no deadline rule yet — the editor then just
    # asks for a due date instead of naming what it's counted from.
    filing["deadline_rule"] = disclosure_fields.deadline_rule(form_type)
    # What is still wrong with this draft, structured (a key per row) so
    # the banner can offer a jump link rather than only a sentence. The
    # same list a rejected generate/sign already returns as prose — one
    # source, so the banner and the rejection can't disagree.
    filing["issues"] = disclosure_fields.field_issues(form_type, filing["field_data"])
    # Where each pre-filled value came from, and whether it still agrees
    # with that source. See disclosure_fields.provenance_for.
    filing["provenance"] = disclosure_fields.provenance_for(
        form_type, filing["field_data"], _current_source_values(conn, user_id, form_type),
    )
    return filing


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def _send_json(self, status, payload, set_cookie=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._write_set_cookie_headers(set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status, html, set_cookie=None):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._write_set_cookie_headers(set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _write_set_cookie_headers(self, set_cookie):
        """set_cookie may be a single cookie header string, a list of
        them (login/logout now set two: the real HttpOnly session
        cookie plus SIGNED_IN_HINT_COOKIE — see that constant's own
        comment), or None. HTTP allows repeating the Set-Cookie header
        once per cookie; send_header() called twice does exactly that,
        it does not overwrite."""
        if not set_cookie:
            return
        cookies = [set_cookie] if isinstance(set_cookie, str) else set_cookie
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)

    def _send_static(self, name):
        """Serve one file out of static/ — the app's own CSS and JS,
        extracted (2026-09) out of the page constants in this module.

        `name` comes straight off the URL, so it's looked up in an
        explicit dict rather than joined onto a directory path: there's
        no "../" handling to get subtly wrong, and an asset that isn't
        one of ours 404s here without ever reaching the filesystem.
        """
        asset = STATIC_ASSETS.get(name)
        if asset is None:
            self.send_response(404)
            self.end_headers()
            return
        body, content_type = asset
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Safe to cache this hard because every reference to these URLs
        # carries ?v=<content hash> (see _asset_url): the URL changes the
        # moment the file does, so a stale copy can't outlive an edit.
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
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
        else:
            # Without this the cookie defaults to a session cookie —
            # gone as soon as the browser closes — even though the
            # session row itself (see accounts.py) is good for
            # SESSION_TTL_DAYS. Matching the two means "stay signed in"
            # actually means 30 days, not "until you close the tab."
            parts.append(f"Max-Age={accounts.SESSION_TTL_DAYS * 86400}")
        if is_https:
            parts.append("Secure")
        return "; ".join(parts)

    def _signed_in_hint_cookie_header(self, clear=False):
        """A second, deliberately non-HttpOnly cookie set/cleared
        alongside the real session cookie (see _session_cookie_header
        just above) — set on signup/login, cleared on logout. Holds no
        session token and grants nothing; it exists only so
        THEME_INIT_SCRIPT's inline pre-paint <script> (which runs
        synchronously, before any fetch could resolve, to avoid a
        flash of the wrong theme) can tell "there's probably a session"
        from "there's probably not" via plain document.cookie, which
        the real session cookie's HttpOnly flag deliberately blocks JS
        from reading. Anyone can forge or strip this cookie — that's
        fine, since nothing security-sensitive reads it; the real
        /api/me check (see account_widget()) still governs actual
        access. SIGNED_IN_HINT_COOKIE is defined near THEME_INIT_SCRIPT,
        not accounts.py, since it's a UI/theme concern, not an auth
        one."""
        parts = [f"{SIGNED_IN_HINT_COOKIE}={'' if clear else '1'}", "Path=/", "SameSite=Lax"]
        if clear:
            parts.append("Max-Age=0")
        else:
            parts.append(f"Max-Age={accounts.SESSION_TTL_DAYS * 86400}")
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        return "; ".join(parts)

    def _redirect_to_login(self):
        """302 to /login, carrying the page the visitor was actually
        trying to reach (path + query string) as ?next=... so a
        logged-out click on e.g. "+ Add as client" or a bookmarked
        /flagged link doesn't just dead-end at a blank login form —
        LOGIN_PAGE's own JS reads this back and returns them there
        after a successful sign-in (falling back to /dashboard if it's
        missing or doesn't look like a same-site path)."""
        self.send_response(302)
        self.send_header("Location", "/login?next=" + quote(self.path, safe=""))
        self.end_headers()

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

    # ── The ~30 route handlers below all start with "is anyone logged
    # in," and only differ in what happens if not: a page route 302s to
    # /login, an API route sends a 401 JSON body (message varies by
    # route — "Sign in to flag bills." reads better than a generic
    # "Not logged in." on that specific action). Both wrap the same
    # core check, _current_user_id, above. ──

    def _require_user_for_page(self):
        """Page routes: opens its own connection just long enough to
        check login status, then closes it — the page itself is a
        pre-built HTML string (any per-request data is fetched by the
        page's own client-side JS), so there's nothing else for the
        caller to do with conn afterward. Returns user_id, or None
        after already sending a 302 to /login (see
        _redirect_to_login); `if not self._require_user_for_page():
        return` is then sufficient at the call site.

        Not used by every page route that checks login — a few (e.g.
        /signup/profile, which redirects to /signup instead of /login
        when logged out) genuinely need a different failure path, not
        just a different page on success."""
        conn = db.get_connection()
        try:
            user_id = self._current_user_id(conn)
        finally:
            conn.close()
        if not user_id:
            self._redirect_to_login()
        return user_id

    def _require_user_for_api(self, conn, message="Not logged in."):
        """API routes: returns user_id, or None after already sending
        a 401 {"error": message} — `if not user_id: return` right
        after is then sufficient. Takes an already-open conn, since
        every one of these callers needs it again right afterward for
        their own query/write; unlike the page-route variant above,
        this can't own the connection's lifecycle itself."""
        user_id = self._current_user_id(conn)
        if not user_id:
            self._send_json(401, {"error": message})
        return user_id

    def _handle_unexpected_error(self):
        """Last resort for do_GET/do_POST/do_DELETE below — logs the
        full traceback (still visible in Render's log stream or local
        stdout) and returns a clean 500 JSON body instead of letting
        BaseHTTPRequestHandler's own default error handling take over,
        which would send a raw traceback back to the client. Matters
        more as real traffic grows — today's low request volume means
        an unhandled exception here has been rare enough to not have
        surfaced yet, not that it can't happen."""
        traceback.print_exc()
        try:
            self._send_json(500, {"error": "Something went wrong on our end. Try again in a moment."})
        except Exception:
            # The original error already happened partway through
            # writing a response (e.g. mid chunked send) — nothing
            # more to do at that point beyond not raising a second
            # exception on top of the first.
            pass

    def do_GET(self):
        try:
            self._do_GET()
        except Exception:
            self._handle_unexpected_error()

    def do_POST(self):
        try:
            self._do_POST()
        except Exception:
            self._handle_unexpected_error()

    def do_DELETE(self):
        try:
            self._do_DELETE()
        except Exception:
            self._handle_unexpected_error()

    def _do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path.startswith("/static/"):
            # Ahead of every other route and of any session check on
            # purpose: these are the app's own CSS/JS, identical bytes
            # for signed-in and signed-out visitors, and they'd otherwise
            # sit behind the session lookup every page does.
            self._send_static(parsed.path[len("/static/"):])
            return

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
            # Unlike every other route, this one has no session check
            # at all by default — a signed-in visitor landing on "/"
            # (e.g. clicking the logo, see TOP_BRAND/TOP_NAV_ACCOUNT_LINKS,
            # both of which keep pointing here on purpose) would just
            # see the marketing page again instead of their own app.
            # Same short-lived-connection pattern as
            # _require_user_for_page(). /dashboard is the shared
            # "somewhere real, now that you're signed in" destination —
            # LOGIN_PAGE's post-sign-in fallback and the signup flow's
            # "Skip for now" land there too. (The retired /watchlist
            # route below is the one exception: it redirects to /flagged
            # specifically, because that page is what it was retired
            # *into*, not just wherever a signed-in visitor belongs.)
            conn = db.get_connection()
            try:
                logged_in = bool(self._current_user_id(conn))
            finally:
                conn.close()
            if logged_in:
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
                return
            self._send_html(200, LANDING_PAGE)
            return

        if parsed.path == "/lookup":
            self._send_html(200, PAGE)
            return

        if parsed.path == "/discover":
            # Retired — merged into /lookup, which now handles both
            # exact bill-number lookups and free-text search in one
            # page (see smart_search() in legiscan_client.py). A
            # redirect rather than a 404 so an old bookmark or external
            # link still lands somewhere useful, carrying over ?q=...
            # if it had one.
            location = "/lookup" + (f"?{parsed.query}" if parsed.query else "")
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
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
            if logged_in:
                self.send_response(302)
                self.send_header("Location", "/flagged")
                self.end_headers()
            else:
                self._redirect_to_login()
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
            if not self._require_user_for_page():
                return
            self._send_html(200, PROFILE_VIEW_PAGE)
            return

        if parsed.path == "/dashboard":
            if not self._require_user_for_page():
                return
            self._send_html(200, DASHBOARD_PAGE)
            return

        if parsed.path == "/api/dashboard":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view your dashboard.")
                if not user_id:
                    return
                self._send_json(200, db.dashboard_summary(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/flagged":
            if not self._require_user_for_page():
                return
            self._send_html(200, FLAGGED_PAGE)
            return

        if parsed.path == "/api/flagged":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view your flagged bills.")
                if not user_id:
                    return
                self._send_json(200, db.list_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        # P1-16: archived flags, listed separately rather than folded into
        # /api/flagged with a status field — an archived bill is off every
        # other list this app builds (the dashboard, the calendar, the
        # sponsor rollup, the digest), so its own list is where "did we
        # actually lose anything" gets answered.
        if parsed.path == "/api/flagged/archived":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view archived bills.")
                if not user_id:
                    return
                self._send_json(200, db.list_archived_bills(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/flagged/archived":
            if not self._require_user_for_page():
                return
            self._send_html(200, ARCHIVED_PAGE)
            return

        if parsed.path == "/flagged/calendar":
            if not self._require_user_for_page():
                return
            self._send_html(200, CALENDAR_PAGE)
            return

        if parsed.path == "/api/flagged/calendar":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view your hearing calendar.")
                if not user_id:
                    return
                self._send_json(200, db.list_hearings_for_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/flagged/sponsors":
            if not self._require_user_for_page():
                return
            self._send_html(200, SPONSOR_ROLLUP_PAGE)
            return

        if parsed.path == "/api/flagged/sponsors":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view sponsors and votes.")
                if not user_id:
                    return
                self._send_json(200, db.list_sponsor_vote_rollup(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/clients":
            if not self._require_user_for_page():
                return
            self._send_html(200, CLIENTS_PAGE)
            return

        if parsed.path == "/directory":
            if not self._require_user_for_page():
                return
            self._send_html(200, DIRECTORY_PAGE)
            return

        if parsed.path == "/api/directory":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view the directory.")
                if not user_id:
                    return
                chamber = (qs.get("chamber") or [""])[0]
                self._send_json(200, {
                    "legislators": db.search_directory(
                        conn, user_id,
                        query=(qs.get("q") or [""])[0],
                        chamber=chamber if chamber in ("Assembly", "Senate") else None,
                    ),
                    "stats": db.directory_stats(conn, user_id),
                    "import": db.latest_directory_import(conn, user_id),
                })
            finally:
                conn.close()
            return

        if parsed.path == "/api/clients":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view your clients.")
                if not user_id:
                    return
                self._send_json(200, db.list_clients(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/clients/detail":
            if not self._require_user_for_page():
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
                user_id = self._require_user_for_api(conn, "Sign in to view this client.")
                if not user_id:
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
                self._send_json(200, {
                    "client": client, "bills": bills, "entity_id": entity_id,
                    # Every stance this user has taken for this client, on
                    # any bill — including bills they've since been taken
                    # off, which is exactly what makes the record worth
                    # keeping (see db.list_position_history).
                    "position_history": db.list_position_history(conn, user_id, client_id=client_id),
                    # The people at this client, and what is coming up for
                    # them. Both were things the record couldn't answer:
                    # every real question about a bill is a question for a
                    # person, and "what's next for them" meant reading the
                    # bill table row by row.
                    "contacts": db.list_client_contacts(conn, user_id, client_id),
                    "hearings": db.list_hearings_for_client(conn, user_id, client_id),
                })
            finally:
                conn.close()
            return

        if parsed.path == "/report":
            if not self._require_user_for_page():
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
                user_id = self._require_user_for_api(conn, "Sign in to view this report.")
                if not user_id:
                    return
                report = db.get_bill_report(conn, user_id, bill_id)
                if not report:
                    # Not stored locally yet — used to only happen for a
                    # bad bill_id, but now that the merged /lookup search
                    # sends you straight here for ANY result (see
                    # LOOKUP_BODY), including one nobody's ever flagged,
                    # this is the normal first-view case too. Fetch it
                    # fresh from LegiScan and store it the same way
                    # /api/flag already does, then serve the report —
                    # same "re-fetch fresh rather than trust the client"
                    # pattern as /api/watchlist.
                    try:
                        bill = get_bill_detail(bill_id)
                    except Exception:
                        traceback.print_exc()
                        self._send_json(502, {"error": "Couldn't reach LegiScan right now. Try again in a moment."})
                        return
                    db.upsert_bill(conn, bill)
                    conn.commit()
                    report = db.get_bill_report(conn, user_id, bill_id)
                if not report:
                    self._send_json(404, {"error": "No bill found with that ID."})
                    return
                self._send_json(200, report)
            finally:
                conn.close()
            return

        if parsed.path == "/draft/letters":
            if not self._require_user_for_page():
                return
            self._send_html(200, LETTERS_PAGE)
            return

        if parsed.path == "/draft/letters/edit":
            if not self._require_user_for_page():
                return
            self._send_html(200, LETTER_EDIT_PAGE)
            return

        if parsed.path == "/api/letters":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to see your letters.")
                if not user_id:
                    return
                bill_id = (qs.get("bill_id") or [""])[0]
                client_id = (qs.get("client_id") or [""])[0]
                self._send_json(200, db.list_letters(
                    conn, user_id,
                    bill_id=int(bill_id) if bill_id.isdigit() else None,
                    client_id=int(client_id) if client_id.isdigit() else None,
                ))
            finally:
                conn.close()
            return

        if parsed.path == "/api/letters/one":
            letter_id = (qs.get("id") or [""])[0]
            if not letter_id.isdigit():
                self._send_json(400, {"error": "Missing or invalid id parameter."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to read your letters.")
                if not user_id:
                    return
                letter = db.get_letter(conn, user_id, int(letter_id))
                if not letter:
                    self._send_json(404, {"error": "No letter with that ID."})
                    return
                self._send_json(200, letter)
            finally:
                conn.close()
            return

        if parsed.path == "/disclosures":
            if not self._require_user_for_page():
                return
            self._send_html(200, DISCLOSURES_PAGE)
            return

        if parsed.path == "/disclosures/review":
            if not self._require_user_for_page():
                return
            self._send_html(200, DISCLOSURE_REVIEW_PAGE)
            return

        if parsed.path == "/api/prepared-filings":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view your disclosure filings.")
                if not user_id:
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
                    # The editor needs to know what's editable and what's
                    # required to render itself — sent alongside the
                    # filing rather than a separate round trip.
                    self._send_json(200, _with_disclosure_editor_meta(filing, conn, user_id))
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
                user_id = self._require_user_for_api(conn, "Sign in to download this filing.")
                if not user_id:
                    return
                filing = db.get_prepared_filing(conn, user_id, filing_id)
            finally:
                conn.close()
            if not filing:
                self._send_json(404, {"error": "No prepared filing found with that id."})
                return
            try:
                pdf_bytes = pdf_forms.render_prepared_filing(filing)
            except Exception:
                # Log the real exception server-side; the user gets a
                # stable, plain-language message instead of whatever
                # pypdf/formatting internals happened to raise (see
                # _handle_unexpected_error for the same pattern elsewhere).
                traceback.print_exc()
                self._send_json(500, {"error": "Couldn't generate the PDF. Try again, or contact support if this keeps happening."})
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
                user_id = self._require_user_for_api(conn, "Sign in to view your profile.")
                if not user_id:
                    return
                self._send_json(200, {"profile": accounts.get_profile(conn, user_id)})
            finally:
                conn.close()
            return

        # The digest's own settings. Separate from /api/profile because
        # the profile is the firm's CAL-ACCESS registration (and is
        # edited through the multi-step /signup/profile form), while this
        # is one person's mail preferences — different owner, different
        # lifecycle, different form.
        if parsed.path == "/api/notification-prefs":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to view your digest settings.")
                if not user_id:
                    return
                self._send_json(200, {
                    "prefs": db.get_notification_prefs(conn, user_id),
                    "muted_bills": db.list_digest_mutes(conn, user_id),
                })
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
                results = search_lobbying(conn, q)
                # search_lobbying() itself caps at 50 (see its own
                # docstring) — a full 50 back almost certainly means more
                # exist, so the frontend can flag that rather than the
                # list silently looking complete in a compliance search.
                self._send_json(200, {"results": results, "truncated": len(results) >= 50})
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
            # Superseded by /api/search below (the merged /lookup+
            # /discover page uses that one) — left in place in case
            # anything else still calls it directly rather than ripped
            # out along with the page that used to be its only caller.
            bill = (qs.get("bill") or [""])[0]
            if not bill:
                self._send_json(400, {"error": "Missing bill parameter."})
                return
            try:
                data = lookup_bill(bill)
                self._send_json(200, data)
            except Exception:
                # LegiScan network hiccups / bad JSON otherwise surfaced
                # their raw text (e.g. "Expecting value: line 1 column 1")
                # straight into the lookup UI — log it, tell the user
                # something they can act on instead.
                traceback.print_exc()
                self._send_json(502, {"error": "Couldn't reach LegiScan right now. Try again in a moment."})
            return

        if parsed.path == "/api/bills/search":
            # Same as /api/bill above — superseded by /api/search, kept
            # alive for any other caller.
            q = (qs.get("q") or [""])[0]
            if not q:
                self._send_json(400, {"error": "Missing q parameter."})
                return
            page_raw = (qs.get("page") or ["1"])[0]
            try:
                page = int(page_raw)
            except ValueError:
                self._send_json(400, {"error": "page must be a number."})
                return
            try:
                data = search_bills(q, page=page)
                self._send_json(200, data)
            except Exception:
                traceback.print_exc()
                self._send_json(502, {"error": "Couldn't reach LegiScan right now. Try again in a moment."})
            return

        if parsed.path == "/api/org-lobbyists":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to see your firm's lobbyists.")
                if not user_id:
                    return
                self._send_json(200, db.list_org_lobbyists(conn, user_id))
            finally:
                conn.close()
            return

        # What has moved lately among the bills the firm watches — the
        # search page's start state (P2-28). Deliberately scoped that way
        # and labelled that way on screen: the refresh job only visits
        # flagged bills, so this can never be a claim about the
        # Legislature at large.
        if parsed.path == "/api/recent-changes":
            try:
                days = min(max(int((qs.get("days") or ["7"])[0]), 1), 90)
            except ValueError:
                days = 7
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to see recent changes.")
                if not user_id:
                    return
                self._send_json(200, {
                    "days": days,
                    "changes": db.recent_bill_changes(
                        conn, user_id, limit=12, since=db.days_ago_in_california(days)),
                })
            finally:
                conn.close()
            return

        if parsed.path == "/api/saved-searches":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to see your saved searches.")
                if not user_id:
                    return
                self._send_json(200, db.list_saved_searches(conn, user_id))
            finally:
                conn.close()
            return

        # Saved VIEWS are the flagged list's, not the search page's: a
        # named filter composition over bills the firm already tracks,
        # where a saved SEARCH is a standing query against all of
        # LegiScan that the daily job re-runs. Different table, different
        # page, deliberately similar wording on screen.
        if parsed.path == "/api/saved-views":
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to see your saved views.")
                if not user_id:
                    return
                self._send_json(200, db.list_saved_views(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/search":
            # The merged /lookup page's one search endpoint — routes to
            # a bill-number search or a free-text one depending on the
            # query itself (see smart_search()'s own docstring), so the
            # page doesn't have to guess which of the two old endpoints
            # (/api/bill, /api/bills/search) to call.
            q = (qs.get("q") or [""])[0]
            if not q:
                self._send_json(400, {"error": "Missing q parameter."})
                return
            page_raw = (qs.get("page") or ["1"])[0]
            try:
                page = int(page_raw)
            except ValueError:
                self._send_json(400, {"error": "page must be a number."})
                return
            # The search page filters and sorts client-side across the
            # whole result set, so it asks for the whole result set (up
            # to SEARCH_PAGE_CAP pages, fetched concurrently) rather than
            # one page at a time. `page` is still honoured for anything
            # asking the old way.
            deep = (qs.get("all") or ["1"])[0] != "0"
            # Only two session choices are offered, and only these two
            # values are ever sent to LegiScan — a stray ?year= can't
            # reach the API. Current session is the default because a
            # lobbyist's question is almost always about live bills; the
            # opt-out exists because bill history is a real question too.
            scope = (qs.get("session") or ["current"])[0]
            year = (legiscan_client.YEAR_ALL_SESSIONS if scope == "all"
                    else legiscan_client.YEAR_CURRENT_SESSION)
            # Two searches behind one endpoint. The default asks
            # LegiScan, which indexes titles and summaries; mode=text
            # asks the local corpus, which holds the bills' actual words
            # (see bill_text.py). They are different questions — "which
            # bill is SB 122" versus "which bills say anything about
            # local control" — and the second one has no answer at
            # LegiScan, at any parameter.
            mode = (qs.get("mode") or ["summary"])[0]
            if mode == "section":
                # A citation, not a phrase. "17053.5" as words also
                # matches every bill that merely cross-references it and
                # every 17053.55 besides; as a citation it matches the
                # bills that actually edit that section.
                code, section = code_sections.parse_query(q)
                conn = db.get_connection()
                try:
                    results = db.search_code_sections(conn, code=code, section=section)
                    stats = db.corpus_stats(conn)
                    stats.update(db.code_section_stats(conn))
                finally:
                    conn.close()
                data = {
                    "results": results,
                    "count": len(results),
                    "complete": True,
                    "corpus": stats,
                    # What the query was understood to mean. Echoed back
                    # because "rev and tax 17053.5" is interpreted, and a
                    # search that quietly reads a query differently than
                    # the user meant should say so on screen.
                    "citation": {"code": code, "section": section},
                }
            elif mode == "text":
                conn = db.get_connection()
                try:
                    results = db.search_bill_text(conn, q)
                    stats = db.corpus_stats(conn)
                finally:
                    conn.close()
                data = {
                    "results": results,
                    "count": len(results),
                    "complete": True,
                    # What the corpus actually holds, passed through so
                    # the page can say "searched 40 bills" rather than
                    # letting an empty result read as "no such bill"
                    # when it really means "not indexed yet."
                    "corpus": stats,
                }
            else:
                try:
                    data = smart_search(
                        q, page=page, year=year,
                        pages=legiscan_client.SEARCH_PAGE_CAP if deep else 1,
                    )
                except Exception:
                    traceback.print_exc()
                    self._send_json(502, {"error": "Couldn't reach LegiScan right now. Try again in a moment."})
                    return
            data["mode"] = mode
            data["session_scope"] = scope
            # Annotate each row with what this user already tracks. A
            # search of 119 results across three pages otherwise asks
            # them to re-evaluate bills they settled last week — the flag
            # state is one indexed read away, and the row can carry it.
            # Signed-out visitors get the results unannotated rather than
            # an error; search doesn't require an account.
            conn = db.get_connection()
            try:
                user_id = self._current_user_id(conn)
                if user_id:
                    tracking = db.tracking_for_bills(
                        conn, user_id, [r["bill_id"] for r in data.get("results", []) if r.get("bill_id")]
                    )
                    for row in data.get("results", []):
                        tracked = tracking.get(row.get("bill_id"))
                        row["flagged"] = bool(tracked)
                        row["clients"] = tracked["clients"] if tracked else []
                    data["signed_in"] = True
                else:
                    data["signed_in"] = False
            finally:
                conn.close()
            self._send_json(200, data)
            return

        self._send_json(404, {"error": "Not found."})

    def _authorized_for_refresh(self):
        """These routes are hit by a cron job with no browser and no
        individual account, gated on their own secret instead — and if
        REFRESH_SECRET was never set (the local/default case), the routes
        don't exist at all, same as any other unrecognized path."""
        if not REFRESH_SECRET:
            return False
        supplied = self.headers.get("X-Refresh-Secret", "")
        return hmac.compare_digest(supplied, REFRESH_SECRET)

    def _do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/internal/refresh-watchlist", "/internal/refresh-calaccess",
                           "/internal/build-corpus"):
            if not self._authorized_for_refresh():
                self.send_response(404)  # not 401 — don't reveal the route exists
                self.end_headers()
                return
            job = {
                "/internal/refresh-watchlist": "watchlist",
                "/internal/refresh-calaccess": "calaccess",
                "/internal/build-corpus": "corpus",
            }[parsed.path]
            target = {
                "watchlist": refresh_watchlist.main,
                "calaccess": refresh_calaccess.main,
                # Budget left at the module default rather than taken
                # from the request: the cap exists to make a runaway
                # impossible, and a cap the caller sets is not a cap.
                "corpus": build_bill_corpus.main,
            }[job]
            if _trigger_refresh(job, target):
                self._send_json(202, {"status": f"{job} refresh started"})
            else:
                self._send_json(409, {"status": f"{job} refresh already running"})
            return

        if parsed.path in ("/api/directory/inspect", "/api/directory/import",
                           "/api/directory/stale", "/api/directory/staff"):
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage the directory.")
                if not user_id:
                    return

                if parsed.path == "/api/directory/inspect":
                    # Read-only: parses the header row and hands back a
                    # proposed role per column. Nothing is written until
                    # the user has seen the guess and said go.
                    text = body.get("text") or ""
                    if len(text) > DIRECTORY_MAX_BYTES:
                        self._send_json(413, {"error": "That file is too large to import."})
                        return
                    self._send_json(200, directory.inspect(text))
                    return

                if parsed.path == "/api/directory/import":
                    text = body.get("text") or ""
                    if len(text) > DIRECTORY_MAX_BYTES:
                        self._send_json(413, {"error": "That file is too large to import."})
                        return
                    records = directory.build_records(text, body.get("mapping"))
                    if not records["legislators"]:
                        self._send_json(400, {
                            "error": " ".join(records["warnings"])
                                     or "Nothing in that file could be imported.",
                        })
                        return
                    saved = db.save_directory_import(
                        conn, user_id,
                        source_name=(body.get("source_name") or "")[:200],
                        as_of=body.get("as_of"),
                        legislators=records["legislators"],
                    )
                    conn.commit()
                    saved["warnings"] = records["warnings"]
                    self._send_json(200, saved)
                    return

                if parsed.path == "/api/directory/stale":
                    db.set_staff_stale(conn, user_id, body.get("staff_id"),
                                       bool(body.get("is_stale")))
                    conn.commit()
                    self._send_json(200, {"status": "ok"})
                    return

                db.update_staff(conn, user_id, body.get("staff_id"), body.get("fields"))
                conn.commit()
                self._send_json(200, {"status": "ok"})
            finally:
                conn.close()
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
                self._send_json(200, {"status": "created"},
                                 set_cookie=[self._session_cookie_header(token), self._signed_in_hint_cookie_header()])
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
                minutes = _login_lockout_remaining_minutes(email)
                self._send_json(
                    429,
                    {"error": f"Too many failed attempts. Try again in about {minutes} minute{'s' if minutes != 1 else ''}."},
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
                self._send_json(200, {"status": "logged in"},
                                 set_cookie=[self._session_cookie_header(token), self._signed_in_hint_cookie_header()])
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
            self._send_json(200, {"status": "logged out"},
                             set_cookie=[self._session_cookie_header(None, clear=True), self._signed_in_hint_cookie_header(clear=True)])
            return

        if parsed.path == "/api/profile":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to save your profile.")
                if not user_id:
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

        if parsed.path == "/api/notification-prefs":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to change your digest settings.")
                if not user_id:
                    return
                # A whole-row write, so the response is the row as
                # stored — the panel renders from that rather than from
                # what it just sent, which is how a rejected or
                # normalised value (a duplicate cc, a dropped unknown
                # change type) shows up in the UI instead of only in the
                # database.
                try:
                    prefs = db.save_notification_prefs(conn, user_id, body)
                except ValueError as err:
                    self._send_json(400, {"error": str(err)})
                    return
                self._send_json(200, {"prefs": prefs})
            finally:
                conn.close()
            return

        if parsed.path == "/api/digest-mute":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            try:
                bill_id = int(body.get("bill_id"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Missing or invalid bill_id."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to mute a bill.")
                if not user_id:
                    return
                # Only a bill the firm actually tracks can be muted —
                # otherwise this endpoint would accept (and store) a row
                # for any bill id at all, and the settings panel would
                # list bills nobody here has ever flagged.
                if bill_id not in db.list_flagged_bill_ids_for_user(conn, user_id):
                    self._send_json(404, {"error": "That bill isn't flagged."})
                    return
                muted = db.set_digest_muted(conn, user_id, bill_id, bool(body.get("muted")))
                self._send_json(200, {"bill_id": bill_id, "muted": muted})
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
                user_id = self._require_user_for_api(conn, "Sign in to flag bills.")
                if not user_id:
                    return
                try:
                    # Same "re-fetch fresh rather than trust the client"
                    # pattern as /api/watchlist — see that route.
                    bill = get_bill_detail(bill_id)
                except Exception:
                    traceback.print_exc()
                    self._send_json(502, {"error": "Couldn't reach LegiScan right now. Try again in a moment."})
                    return
                db.upsert_bill(conn, bill)
                db.flag_bill(conn, user_id, bill_id)
                conn.commit()
                self._send_json(200, {"status": "flagged"})
            finally:
                conn.close()
            return

        # Restoring an archived bill (P1-16) is NOT the same request as
        # flagging a new one above: there's no fresh LegiScan detail to
        # fetch (the bill's already in `bills`, refreshed daily right up
        # until it was archived) and no client to pick (bill_client_links
        # never left). Reusing /api/flag POST would mean every restore
        # waits on a LegiScan round trip and fails outright if that round
        # trip does — for an action that's otherwise pure local bookkeeping.
        if parsed.path == "/api/flag/restore":
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
                user_id = self._require_user_for_api(conn, "Sign in to restore flagged bills.")
                if not user_id:
                    return
                db.flag_bill(conn, user_id, bill_id)  # clears archived_at — see its ON CONFLICT clause
                conn.commit()
                self._send_json(200, {"status": "restored"})
            finally:
                conn.close()
            return

        if parsed.path == "/api/org-lobbyists":
            # The firm's roster, which is what Form 601's Part I is a list
            # of. Kept on Profile rather than inside the disclosure flow:
            # it's a fact about the firm, not about one filing, and every
            # 601 from here on reads it.
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage your firm's lobbyists.")
                if not user_id:
                    return
                try:
                    db.add_org_lobbyist(conn, user_id, body.get("name"), body.get("cert_id"))
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, db.list_org_lobbyists(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/letters":
            # Start a letter. The seed is built server-side from the bill
            # report this user can already see (see letter_drafts) rather
            # than assembled in the browser — the wording of the thing
            # this app puts a lobbyist's name under belongs in one place
            # with the rest of the document domain, not in a template.
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            bill_id = body.get("bill_id")
            if not bill_id:
                self._send_json(400, {"error": "Pick a bill to write about."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to draft a letter.")
                if not user_id:
                    return
                report = db.get_bill_report(conn, user_id, int(bill_id))
                if not report:
                    self._send_json(404, {"error": "No bill found with that ID."})
                    return
                client_id = body.get("client_id")
                client = None
                if client_id:
                    client = next(
                        (c for c in report.get("assigned_clients", []) if c["id"] == int(client_id)),
                        None,
                    )
                    if not client:
                        self._send_json(400, {"error": "That client isn't assigned to this bill."})
                        return
                # The soonest hearing, which is what a letter written
                # ahead of a hearing names. get_bill_report already
                # filters these to date >= today and orders them.
                hearing = (report.get("upcoming_hearings") or [None])[0]
                seed = letter_drafts.build_seed(
                    report, client,
                    position=(client or {}).get("position"),
                    hearing=hearing,
                    profile=accounts.get_profile(conn, user_id),
                )
                letter_id = db.create_letter(conn, user_id, {
                    "bill_id": report["bill_id"],
                    "bill_label": f"{report.get('state') or ''} {report.get('bill_number') or ''}".strip(),
                    "client_id": (client or {}).get("id"),
                    "client_name": (client or {}).get("name"),
                    "position": (client or {}).get("position"),
                    "subject": seed["subject"],
                    "body": seed["body"],
                })
                conn.commit()
                self._send_json(200, {"id": letter_id})
            finally:
                conn.close()
            return

        if parsed.path == "/api/letters/save":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            letter_id = body.get("id")
            if not letter_id:
                self._send_json(400, {"error": "Missing id."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to edit your letters.")
                if not user_id:
                    return
                if not db.update_letter(conn, user_id, int(letter_id),
                                        body.get("subject"), body.get("body")):
                    self._send_json(404, {"error": "No letter with that ID."})
                    return
                conn.commit()
                self._send_json(200, db.get_letter(conn, user_id, int(letter_id)))
            finally:
                conn.close()
            return

        if parsed.path == "/api/saved-searches":
            # Saving a query so the daily job re-runs it (see
            # saved_searches in schema.sql). The optional client is what a
            # new match gets assigned to when the user flags it — one
            # saved search per client covers most of a firm's needs.
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to save a search.")
                if not user_id:
                    return
                client_id = body.get("client_id")
                try:
                    db.create_saved_search(
                        conn, user_id, body.get("name"), body.get("query"),
                        int(client_id) if client_id else None,
                    )
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, db.list_saved_searches(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/saved-views":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to save a view.")
                if not user_id:
                    return
                try:
                    views = db.create_saved_view(conn, user_id, body.get("name"), body.get("query"))
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                self._send_json(200, views)
            finally:
                conn.close()
            return

        if parsed.path == "/api/saved-searches/seen":
            # Opening a saved search is seeing its new matches, same as a
            # digest going out — the count clears either way, so the two
            # can't disagree about what the user has been told.
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            saved_search_id = body.get("id")
            if not saved_search_id:
                self._send_json(400, {"error": "Missing id."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage saved searches.")
                if not user_id:
                    return
                db.mark_search_seen(conn, user_id, int(saved_search_id))
                conn.commit()
                self._send_json(200, db.list_saved_searches(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/flag-bulk":
            # Triage of a new-bill sweep is a bulk activity: a session is
            # thirty bills skimmed and four flagged. Doing that one at a
            # time through /api/flag meant four page round trips plus
            # three re-searches, so this takes the whole selection at
            # once — and optionally assigns every one of them to a client
            # in the same request, since "flag these four for UCSA" is
            # the actual thought behind the selection.
            #
            # Still one getBill per bill (see /api/flag on why the detail
            # is re-fetched rather than trusted from the browser), which
            # is the real cost and why the selection is capped. What this
            # saves is the navigation, not the API calls.
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            raw_ids = body.get("bill_ids")
            if not isinstance(raw_ids, list) or not raw_ids:
                self._send_json(400, {"error": "Pick at least one bill to flag."})
                return
            try:
                bill_ids = [int(b) for b in raw_ids]
            except (ValueError, TypeError):
                self._send_json(400, {"error": "bill_ids must be numbers."})
                return
            if len(bill_ids) > MAX_BULK_FLAG:
                self._send_json(400, {
                    "error": f"Flag up to {MAX_BULK_FLAG} bills at a time. "
                             "Each one is a separate lookup against LegiScan."
                })
                return

            client_id = body.get("client_id")
            position = body.get("position") or "watch"

            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to flag bills.")
                if not user_id:
                    return
                flagged, failed = [], []
                for bill_id in bill_ids:
                    try:
                        bill = get_bill_detail(bill_id)
                    except Exception:
                        # One unreachable bill shouldn't lose the other
                        # three. Reported per-bill below rather than
                        # failing the whole request.
                        traceback.print_exc()
                        failed.append(bill_id)
                        continue
                    db.upsert_bill(conn, bill)
                    db.flag_bill(conn, user_id, bill_id)
                    if client_id:
                        try:
                            db.link_bill_to_client(conn, user_id, bill_id, int(client_id), position)
                        except ValueError as e:
                            self._send_json(400, {"error": str(e)})
                            return
                    flagged.append(bill_id)
                conn.commit()
                self._send_json(200, {"flagged": flagged, "failed": failed})
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
                user_id = self._require_user_for_api(conn, "Sign in to add clients.")
                if not user_id:
                    return
                client_id = body.get("id")
                try:
                    if client_id:
                        db.update_client(conn, user_id, client_id, body)
                    else:
                        db.create_client(conn, user_id, body)
                except ValueError as err:
                    # Compensation is normalized in db._client_values, not
                    # here, so a bad amount is refused the same way
                    # whether it arrives from this form or from a future
                    # importer — see db.normalize_compensation.
                    self._send_json(400, {"error": str(err)})
                    return
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
                user_id = self._require_user_for_api(conn, "Sign in to assign clients.")
                if not user_id:
                    return
                position = body.get("position") or "watch"
                # None (not "") means "leave the effective date alone" —
                # see db.link_bill_to_client, which only moves it when the
                # position itself changed. An empty string from a cleared
                # date input is normalized to that same None rather than
                # being written as a blank date.
                effective_date = (body.get("effective_date") or "").strip() or None
                try:
                    db.link_bill_to_client(
                        conn, user_id, bill_id, client_id, position,
                        effective_date=effective_date,
                    )
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, db.list_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/bill-notes":
            # The lobbyist's own note on a bill — per user, stored against
            # their flag (see flagged_bills.notes), so it survives the
            # daily refresh overwriting everything on the shared `bills`
            # row and disappears with the flag rather than outliving it.
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
                bill_id = int(bill_id)
            except (ValueError, TypeError):
                self._send_json(400, {"error": "bill_id must be a number."})
                return

            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to add notes to a bill.")
                if not user_id:
                    return
                try:
                    notes = db.set_bill_notes(conn, user_id, bill_id, (body.get("notes") or "").strip())
                except ValueError as e:
                    # Not flagged, so there's no per-user row to hang the
                    # note on. A 400 saying so beats accepting the text and
                    # dropping it.
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, {"notes": notes})
            finally:
                conn.close()
            return

        if parsed.path == "/api/bill-viewed":
            # "I have now looked at this bill" — clears its unread dot on
            # the flagged list (see db.mark_bill_viewed). Sent by the bill
            # report once it has actually rendered, not on the way in, so
            # a request that errored out doesn't count as having been read.
            #
            # A POST rather than a side effect of GET /api/report: reading
            # a report is also how the digest email's links work and how a
            # search result opens, and a GET that quietly mutates state is
            # the kind of thing a link prefetcher fires for free.
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
                bill_id = int(bill_id)
            except (ValueError, TypeError):
                self._send_json(400, {"error": "bill_id must be a number."})
                return

            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to track what you've read.")
                if not user_id:
                    return
                # False for a bill this user hasn't flagged — reported as
                # such rather than as an error; see mark_bill_viewed.
                marked = db.mark_bill_viewed(conn, user_id, bill_id)
                conn.commit()
                self._send_json(200, {"marked": marked})
            finally:
                conn.close()
            return

        if parsed.path == "/api/bill-amend-by-date":
            # "When does this need to be amended by?" — manually entered,
            # not synced from LegiScan (checked its raw getBill payload
            # directly; no such field exists there). See the column
            # comment on bills.amend_by_date in schema.sql.
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
                bill_id = int(bill_id)
            except (ValueError, TypeError):
                self._send_json(400, {"error": "bill_id must be a number."})
                return

            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to set an amendment deadline.")
                if not user_id:
                    return
                report = db.get_bill_report(conn, user_id, bill_id)
                if not report:
                    self._send_json(404, {"error": "No bill found with that ID."})
                    return
                db.set_bill_amend_by_date(conn, bill_id, (body.get("amend_by_date") or "").strip())
                conn.commit()
                self._send_json(200, db.get_bill_report(conn, user_id, bill_id))
            finally:
                conn.close()
            return

        # The people at a client, and the firm's running notes on the
        # relationship. Both hang off one client, so both take a
        # client_id and hand back the fresh list the page re-renders from.
        if parsed.path == "/api/client-contacts":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            try:
                client_id = int(body.get("client_id"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Missing or invalid client_id."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage contacts.")
                if not user_id:
                    return
                try:
                    if body.get("contact_id"):
                        # The only edit this endpoint makes to an existing
                        # contact: which one to call first.
                        contacts = db.set_primary_contact(
                            conn, user_id, client_id, int(body["contact_id"]))
                    else:
                        contacts = db.add_client_contact(conn, user_id, client_id, body)
                except (ValueError, TypeError) as err:
                    self._send_json(400, {"error": str(err) or "Invalid contact."})
                    return
                self._send_json(200, contacts)
            finally:
                conn.close()
            return

        if parsed.path == "/api/client-notes":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            try:
                client_id = int(body.get("client_id"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Missing or invalid client_id."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to save notes.")
                if not user_id:
                    return
                if not db.set_client_notes(conn, user_id, client_id, body.get("notes")):
                    self._send_json(404, {"error": "No client found with that ID."})
                    return
                self._send_json(200, {"status": "saved"})
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
                user_id = self._require_user_for_api(conn, "Sign in to add bills to a client.")
                if not user_id:
                    return
                if not db.get_client(conn, user_id, client_id):
                    self._send_json(404, {"error": "No client found with that ID."})
                    return
                try:
                    bill = lookup_bill(bill_number)
                except Exception:
                    traceback.print_exc()
                    self._send_json(502, {"error": "Couldn't reach LegiScan right now. Try again in a moment."})
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
                user_id = self._require_user_for_api(conn, "Sign in to prepare a disclosure form.")
                if not user_id:
                    return
                profile = accounts.get_profile(conn, user_id)
                if not profile:
                    self._send_json(400, {
                        "error": "Complete your registration profile before preparing a disclosure form.",
                    })
                    return
                user_row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
                clients = db.list_clients(conn, user_id)

                client_row_ids = None
                if form_type == "601":
                    field_data = pdf_forms.values_for_form_601(
                        profile, clients, user_row["email"], sign_off=None,
                        today=datetime.date.today(),
                        # Form 601 exists to register a firm's lobbyists.
                        # Until an organization sat above the account
                        # there was only ever one name to put here; an
                        # empty roster still falls back to the
                        # registrant's own, which is right for a firm of
                        # one (see pdf_forms.values_for_form_601).
                        lobbyists=db.list_org_lobbyists(conn, user_id),
                    )
                    client_row_ids = [c["id"] for c in clients[:pdf_forms.max_client_rows()]]

                filing_id = db.create_prepared_filing(
                    conn, user_id, form_type, body.get("period_label"), field_data, client_row_ids=client_row_ids,
                )
                conn.commit()
                self._send_json(200, _with_disclosure_editor_meta(db.get_prepared_filing(conn, user_id, filing_id), conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/prepared-filings/field":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            filing_id = body.get("id")
            field_key = body.get("field_key")
            value = body.get("value") or ""
            if not filing_id or not field_key:
                self._send_json(400, {"error": "Missing id or field_key."})
                return

            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to edit a disclosure filing.")
                if not user_id:
                    return
                filing = db.get_prepared_filing(conn, user_id, filing_id)
                if not filing:
                    self._send_json(404, {"error": "No prepared filing found with that id."})
                    return
                if not disclosure_fields.is_editable_field_key(filing["form_type"], field_key):
                    self._send_json(400, {"error": "That field isn't editable on this form."})
                    return
                problem = None
                for f in disclosure_fields.sections_for_form_type(filing["form_type"]):
                    match = next((x for x in f.get("fields", []) if x["key"] == field_key), None)
                    if match:
                        problem = disclosure_fields.validate_field(match["kind"], value.strip())
                        break
                if problem:
                    self._send_json(400, {"error": f"Invalid value: {problem}."})
                    return
                try:
                    filing = db.update_prepared_filing_field(conn, user_id, filing_id, field_key, value)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, _with_disclosure_editor_meta(filing, conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/prepared-filings/deadline":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            filing_id = body.get("id")
            if not filing_id:
                self._send_json(400, {"error": "Missing id."})
                return
            trigger_date = (body.get("trigger_date") or "").strip()
            # An explicit due_date in the body is an override and is taken
            # as-is; otherwise it's derived from the trigger. The lobbyist's
            # own reading of their deadline beats this app's arithmetic —
            # see disclosure_fields.FORM_DEADLINES on why nothing here is
            # inferred from a draft's created_at.
            due_date = (body.get("due_date") or "").strip()

            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to edit a disclosure filing.")
                if not user_id:
                    return
                filing = db.get_prepared_filing(conn, user_id, filing_id)
                if not filing:
                    self._send_json(404, {"error": "No prepared filing found with that id."})
                    return
                for label, value in (("Qualifying date", trigger_date), ("Due date", due_date)):
                    if value and not disclosure_fields.valid_iso_date(value):
                        self._send_json(400, {"error": f"{label} must be a real calendar date."})
                        return
                if not due_date:
                    due_date = disclosure_fields.due_date_for(filing["form_type"], trigger_date)
                try:
                    filing = db.set_prepared_filing_deadline(conn, user_id, filing_id, trigger_date, due_date)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, _with_disclosure_editor_meta(filing, conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/prepared-filings/select-clients":
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body."})
                return
            filing_id = body.get("id")
            client_ids = body.get("client_ids")
            if not filing_id or not isinstance(client_ids, list):
                self._send_json(400, {"error": "Missing id or client_ids."})
                return
            if len(client_ids) > pdf_forms.max_client_rows():
                self._send_json(400, {"error": f"This form only has {pdf_forms.max_client_rows()} client rows."})
                return
            try:
                client_ids = [int(cid) for cid in client_ids]
            except (ValueError, TypeError):
                self._send_json(400, {"error": "client_ids must all be numbers."})
                return

            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to edit a disclosure filing.")
                if not user_id:
                    return
                filing = db.get_prepared_filing(conn, user_id, filing_id)
                if not filing:
                    self._send_json(404, {"error": "No prepared filing found with that id."})
                    return
                clients = []
                for cid in client_ids:
                    client = db.get_client(conn, user_id, cid)
                    if not client:
                        self._send_json(400, {"error": f"No client found with id {cid}."})
                        return
                    clients.append(client)
                # Adding or removing one client must not wipe what was
                # typed into the other rows — four of the five row fields
                # are things the client record doesn't hold, so they are
                # typed here. Pass the rows as they stand so each
                # retained client's own edits move with it.
                row_values = pdf_forms.client_row_values(
                    clients,
                    previous_clients=filing.get("client_row_ids") or [],
                    previous_field_data=filing["field_data"],
                )
                try:
                    filing = db.set_prepared_filing_client_rows(conn, user_id, filing_id, client_ids, row_values)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, _with_disclosure_editor_meta(filing, conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/prepared-filings/generate-pdf":
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
                user_id = self._require_user_for_api(conn, "Sign in to generate a disclosure PDF.")
                if not user_id:
                    return
                filing = db.get_prepared_filing(conn, user_id, filing_id)
                if not filing:
                    self._send_json(404, {"error": "No prepared filing found with that id."})
                    return
                errors = disclosure_fields.validate_field_data(filing["form_type"], filing["field_data"])
                if errors:
                    self._send_json(400, {"error": "Fix these before generating a PDF.", "field_errors": errors})
                    return
                try:
                    filing = db.mark_prepared_filing_pdf_generated(conn, user_id, filing_id)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                conn.commit()
                self._send_json(200, _with_disclosure_editor_meta(filing, conn, user_id))
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
                user_id = self._require_user_for_api(conn, "Sign in to sign off on a filing.")
                if not user_id:
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

        self._send_json(404, {"error": "Not found."})

    def _do_DELETE(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/api/org-lobbyists":
            lobbyist_id = (qs.get("id") or [""])[0]
            if not lobbyist_id.isdigit():
                self._send_json(400, {"error": "Missing or invalid id parameter."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage your firm's lobbyists.")
                if not user_id:
                    return
                if not db.delete_org_lobbyist(conn, user_id, int(lobbyist_id)):
                    self._send_json(404, {"error": "No lobbyist with that ID."})
                    return
                conn.commit()
                self._send_json(200, db.list_org_lobbyists(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/letters":
            letter_id = (qs.get("id") or [""])[0]
            if not letter_id.isdigit():
                self._send_json(400, {"error": "Missing or invalid id parameter."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage your letters.")
                if not user_id:
                    return
                if not db.delete_letter(conn, user_id, int(letter_id)):
                    self._send_json(404, {"error": "No letter with that ID."})
                    return
                conn.commit()
                self._send_json(200, db.list_letters(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/saved-searches":
            saved_search_id = (qs.get("id") or [""])[0]
            if not saved_search_id:
                self._send_json(400, {"error": "Missing id parameter."})
                return
            try:
                saved_search_id = int(saved_search_id)
            except ValueError:
                self._send_json(400, {"error": "id must be a number."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage saved searches.")
                if not user_id:
                    return
                if not db.delete_saved_search(conn, user_id, saved_search_id):
                    self._send_json(404, {"error": "No saved search with that ID."})
                    return
                conn.commit()
                self._send_json(200, db.list_saved_searches(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/saved-views":
            view_id = (qs.get("id") or [""])[0]
            try:
                view_id = int(view_id)
            except ValueError:
                self._send_json(400, {"error": "id must be a number."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage saved views.")
                if not user_id:
                    return
                if not db.delete_saved_view(conn, user_id, view_id):
                    self._send_json(404, {"error": "No saved view with that ID."})
                    return
                self._send_json(200, db.list_saved_views(conn, user_id))
            finally:
                conn.close()
            return

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
                user_id = self._require_user_for_api(conn, "Sign in to manage flagged bills.")
                if not user_id:
                    return
                # Archives, doesn't delete (P1-16) — see archive_flagged_bill.
                # Restoring is /api/flag POST re-flagging the same bill_id.
                db.archive_flagged_bill(conn, user_id, bill_id)
                conn.commit()
                self._send_json(200, db.list_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/client-contacts":
            try:
                client_id = int((qs.get("client_id") or [""])[0])
                contact_id = int((qs.get("id") or [""])[0])
            except ValueError:
                self._send_json(400, {"error": "client_id and id must be numbers."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage contacts.")
                if not user_id:
                    return
                if not db.delete_client_contact(conn, user_id, client_id, contact_id):
                    self._send_json(404, {"error": "No contact with that ID."})
                    return
                self._send_json(200, db.list_client_contacts(conn, user_id, client_id))
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
                user_id = self._require_user_for_api(conn, "Sign in to manage clients.")
                if not user_id:
                    return
                db.delete_client(conn, user_id, client_id)
                conn.commit()
                self._send_json(200, db.list_clients(conn, user_id))
            finally:
                conn.close()
            return

        if parsed.path == "/api/prepared-filings":
            filing_id = (qs.get("id") or [""])[0]
            if not filing_id:
                self._send_json(400, {"error": "Missing id parameter."})
                return
            try:
                filing_id = int(filing_id)
            except ValueError:
                self._send_json(400, {"error": "id must be a number."})
                return
            conn = db.get_connection()
            try:
                user_id = self._require_user_for_api(conn, "Sign in to manage disclosure filings.")
                if not user_id:
                    return
                db.delete_prepared_filing(conn, user_id, filing_id)
                conn.commit()
                self._send_json(200, db.list_prepared_filings(conn, user_id))
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
                user_id = self._require_user_for_api(conn, "Sign in to manage client assignments.")
                if not user_id:
                    return
                db.unlink_bill_from_client(conn, user_id, bill_id, client_id)
                conn.commit()
                self._send_json(200, db.list_flagged_bills(conn, user_id))
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "Not found."})


def main():
    # Was previously a printed warning ("the app will still start, but
    # lookups will fail until it's set") — now refuses to start at all.
    # A missing LEGISCAN_API_KEY used to surface as a 502 on someone's
    # first real lookup, hours after the process actually started;
    # failing here means it can't boot into that half-working state.
    config.validate()

    db.init_db()

    is_hosted = config.IS_HOSTED

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
