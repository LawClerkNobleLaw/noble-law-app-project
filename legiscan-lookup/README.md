# LegiScan Bill Lookup

A small web app for looking up and tracking California bill history from
LegiScan, plus the CAL-ACCESS lobbying-disclosure pipeline that shares its
database. Runs on your Mac by default (`./start.sh`), and can also be
hosted on Render so it's reachable by coworkers — see below.

## Run it locally

```
./start.sh
```

or directly:

```
python3 app.py
```

Then open **http://localhost:8420** if it doesn't open automatically.

## Requirements

- Python 3 (already on macOS by default)
- `LEGISCAN_API_KEY` set in your environment — this is already configured
  in `~/.zshrc`. If you ever need to change it:
  ```
  export LEGISCAN_API_KEY=your_key_here
  ```

## How it works

Things that live in this app:

- **Live lookup** (the original feature) — search a bill by number at
  `/` (California only — the state was dropped from the search since
  every user is a CA lobbyist), calls LegiScan on the spot via
  `getSearch`/`getBill`, shows the result. Nothing is stored by the
  search itself.
- **Flagged bills** — sign in and click "Flag this bill" to add it to
  your own personal list at `/flagged`; its status/sponsors/history get
  saved to `db/billwatch.db` the same as before. This replaced an
  earlier single watch list shared by everyone with no owner — that
  made sense before individual accounts existed, but once they did, an
  ownerless shared list where anyone could silently remove a bill
  someone else was tracking was more liability than feature. A separate
  daily job, `refresh_watchlist.py`, re-checks only flagged bills once a
  day — not the whole session — to stay well under LegiScan's free-tier
  query cap (see the math in that file's docstring).
- **Individual accounts** — `/signup` (email + password, then a
  CAL-ACCESS Form 601-style profile step), `/login`, `/profile`. The
  site itself doesn't require signing in to visit — the live lookup and
  lobbying search are open to anyone with the URL — but the personal
  features (flagged bills, clients, action reports, profile) need it.
  Real password hashing lives in `accounts.py`.
- **Clients** — `/clients` lets each signed-in user keep their own list
  of clients (Form 602/603-style: name, business address, industry/
  interests, an optional CAL-ACCESS filer ID for future cross-checking).
  On `/flagged`, any flagged bill can be assigned to one or more of a
  user's clients, each with its own position — Support, Oppose, or
  Watch — changeable at any time from the same dropdown.
- **Action report** — `/report?bill_id=...` (linked from `/flagged`)
  rolls up everything about one bill in one place: current status, full
  status history, amendment history, upcoming hearings, and — if it's
  assigned to one of the signed-in user's clients — that client's name
  and position.
- **CAL-ACCESS lobbying data** — a separate pipeline in the sibling
  `calaccess-pipeline/` folder downloads California's daily lobbying
  disclosure export and loads it into the same database
  (`lobbying_entities`, `lobbying_disclosures`), so it can eventually be
  joined against bill data. See `calaccess-pipeline/refresh_calaccess.py`
  and `client_interest_tracking_framework.md`.

`app.py` itself has no dependencies beyond the Python standard library.
The actual "talk to LegiScan" logic lives in `legiscan_client.py`, and the
database logic lives in `db.py` (and `calaccess-pipeline/calaccess_db.py`)
— shared with the daily refresh scripts so nothing is duplicated between
the live app and the jobs.

**Locally**, both daily refreshes run via `launchd` (see `launchd/` in
this folder and in `calaccess-pipeline/`) — independent scripts that open
the database file directly, which only works because everything's on one
Mac sharing one local file.

**Hosted (see below)**, that local mechanism doesn't apply — Render's Cron
Job service type can't attach a persistent disk at all, so a cron job
can't touch the database directly. Instead, the web app exposes two
internal, secret-gated endpoints (`POST /internal/refresh-watchlist` and
`/internal/refresh-calaccess`) that run the exact same refresh code in a
background thread of the one always-on process that *does* hold the
disk. The two Render cron services are just thin triggers — each one
wakes up, makes one authenticated call, and exits.

## Why not a claude.ai artifact?

Published Artifact pages run behind a strict content-security policy that
blocks any outbound network request to an external host, regardless of
the API or credentials involved. Running this as a plain script — either
locally or on a normal host — sidesteps that entirely.

## Hosting on Render

This deploys as three Render services from one Blueprint (`render.yaml`
in this folder): the web app, and two thin cron triggers. Steps:

1. **Push this repo to GitHub** (already set up if you asked Claude to do
   it — otherwise: `gh repo create`, then `git push`).
2. **Create a free Render account** at render.com and connect your GitHub
   account.
3. In Render, choose **New → Blueprint**, pick this repo. Render reads
   `render.yaml` and creates all three services — the web app
   (`legiscan-lookup`) and two cron jobs (`billwatch-refresh-watchlist`,
   `billwatch-refresh-calaccess`).
4. You'll be prompted for secrets. Enter:
   - `LEGISCAN_API_KEY` — your LegiScan key (web service only)
   - `REFRESH_SECRET` — **make one up** (anything long and random works,
     e.g. `openssl rand -hex 32` in a terminal) and **enter the exact same
     value for all three services** — the two cron jobs use it to prove
     to the web app that a refresh trigger is legitimate, not a stranger
     hitting the endpoint.
5. Deploy. Render gives the web service a permanent URL like
   `https://legiscan-lookup.onrender.com` — share that with coworkers.
   The site itself is open to anyone with the link; each coworker signs
   up for their own individual account (see Individual accounts, above)
   to use the personal features.

**What you're paying for:** the `starter` plan on the web service
(~$7/mo, keeps it always-on instead of spinning down after 15 minutes
idle) plus a small persistent disk (a couple GB, cents/month) plus the
two cron jobs (billed per second of actual run time — each one is just a
single `curl` call, so this should be close to nothing; the real cost of
the refresh itself is absorbed by the already-running web service, not
the cron job).

**Two things worth checking once it's actually live**, since they
couldn't be verified without a real Render account:
- The cron jobs' `curl` call targets `http://$WEB_HOSTPORT/...` — Render's
  *private* network address for the web service, not its public URL. If
  that doesn't resolve on your plan tier, switch `render.yaml`'s
  `property: hostport` to `property: host` and change the cron
  `startCommand`s to `https://` instead of `http://`.
- The cron schedules (6am/7am Pacific) are written as fixed UTC times.
  Render doesn't adjust for daylight saving, so they'll drift an hour
  twice a year unless you nudge them by hand in `render.yaml`.

Since `REFRESH_SECRET` is unset for local runs, running it locally with
`./start.sh` stays exactly as frictionless as before, and the
`/internal/refresh-*` routes simply don't exist locally (404) —
`launchd` keeps doing the job on your Mac exactly as it does today.
