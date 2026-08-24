"""
config.py — every environment variable this app reads, in one place.

Before this existed, each of app.py, db.py, legiscan_client.py,
digest.py, and mailer.py called os.environ.get(...) for its own
settings independently — correct, but it meant there was no single
place to look to know "what does this app actually need to run," and
no one spot that could refuse to start with a missing required
setting. Those five files now import their settings from here instead
of reading os.environ directly.

Two different failure modes on purpose:

- REQUIRED settings (currently just LEGISCAN_API_KEY) make validate()
  raise ConfigError, listing everything missing at once. Call
  validate() explicitly at real startup (app.py's main() does) — NOT
  at import time, since plenty of things legitimately import app.py/
  db.py/etc. without ever booting the server (the test suite in
  tests/, a one-off script, a REPL) and shouldn't be forced to have
  every production env var set just to do that.

- Everything else already has a sensible default (BILLWATCH_DATA_DIR,
  APP_BASE_URL, PORT) or is intentionally optional — SMTP_*/EMAIL_FROM
  and REFRESH_SECRET degrade gracefully when unset (see mailer.py's and
  app.py's own docstrings for that reasoning); this file doesn't change
  that, it just centralizes where each value is read from.
"""

import os
import re


class ConfigError(RuntimeError):
    """Raised by validate() when a required setting is missing."""


def _get_legiscan_api_key():
    """Same resolution order this app has always used: the environment
    first, then (since launchd and a double-clicked app don't read
    ~/.zshrc the way an interactive shell does) parsed directly out of
    a `export LEGISCAN_API_KEY=...` line in it."""
    key = os.environ.get("LEGISCAN_API_KEY")
    if key:
        return key
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


# ── LegiScan ──────────────────────────────────────────────────────────
LEGISCAN_API_KEY = _get_legiscan_api_key()

# ── Where the SQLite file lives — see db.py's own module docstring for
#    why this differs between a local run and Render (persistent disk
#    survives redeploys; the code checkout doesn't). None here means
#    "use db.py's own repo-local default," not "no database." ──
BILLWATCH_DATA_DIR = os.environ.get("BILLWATCH_DATA_DIR")

# ── The daily digest email's own links back into the app (see
#    digest.py) — defaults to the local dev server so links still work
#    when testing by hand without setting anything. ──
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8420").rstrip("/")

# ── The web server's own port. ──
PORT = int(os.environ.get("PORT", "8420"))

# ── Gates /internal/refresh-* (see app.py's _authorized_for_refresh) —
#    None means those routes don't exist at all, same as any other
#    unrecognized path; this is intentionally optional, not required,
#    since a local dev run has no cron hitting those routes anyway. ──
REFRESH_SECRET = os.environ.get("REFRESH_SECRET")

# ── SMTP, for the daily digest email (see mailer.py). All four must be
#    set together for mailer.is_configured() to be true; intentionally
#    optional — send_email() degrades to logging what it would have
#    sent rather than raising when they're not. ──
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_FROM = os.environ.get("EMAIL_FROM")

# ── Is this process running on a real host (Render) rather than
#    someone's Mac? Just changes what main() prints/opens, nothing
#    behavioral. ──
IS_HOSTED = bool(os.environ.get("RENDER") or os.environ.get("PORT_ASSIGNED_BY_HOST"))


# Settings that MUST be present for the app to actually work — checked
# together so a fresh checkout missing more than one reports all of
# them at once, not just whichever was read first.
_REQUIRED = {
    "LEGISCAN_API_KEY": LEGISCAN_API_KEY,
}


def validate():
    """Raises ConfigError listing every missing required setting.
    Called once, explicitly, at real startup (app.py's main()) — not
    at import time. Previously, a missing LEGISCAN_API_KEY only printed
    a warning and let the server boot anyway, so the first real symptom
    was a bill lookup failing with a 502 partway through someone's
    workday instead of the process refusing to start at all."""
    missing = [name for name, value in _REQUIRED.items() if not value]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing) +
            ". Set " + missing[0] + " (and any others listed) and try again."
        )
