# LegiScan Bill Lookup

A small local web app for looking up bill history from LegiScan. Runs on
your Mac (not hosted anywhere), so it has normal internet access and calls
the LegiScan API live, on demand, every time you search.

## Run it

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

- `app.py` runs a tiny local HTTP server with no external dependencies
  (pure Python standard library).
- The page at `/` is a search form (state + bill number).
- Submitting it calls `/api/bill?state=..&bill=..` on the same local
  server, which in turn calls LegiScan's `getSearch` and `getBill`
  operations and returns the bill's title, sponsors, and full history.
- Nothing is cached — every search is a fresh live request to LegiScan.

## Why not a claude.ai artifact?

Published Artifact pages run behind a strict content-security policy that
blocks any outbound network request to an external host, regardless of
the API or credentials involved. Running this as a plain script — either
locally or on a normal host — sidesteps that entirely.

## Sharing it with coworkers (hosting on Render)

This app has no dependencies beyond the Python standard library, so it
deploys as-is to any host that runs Python. Render is the easiest option:

1. **Push this repo to GitHub** (already set up if you asked Claude to do
   it — otherwise: `gh repo create`, then `git push`).
2. **Create a free Render account** at render.com and connect your GitHub
   account.
3. In Render, choose **New → Blueprint**, pick this repo. Render will read
   `render.yaml` and prompt you for three secret values:
   - `LEGISCAN_API_KEY` — your LegiScan key
   - `LOOKUP_USER` — a shared username for coworkers
   - `LOOKUP_PASSWORD` — a shared password for coworkers
4. Deploy. Render gives you a permanent URL like
   `https://legiscan-lookup.onrender.com` — share that with coworkers,
   along with the username/password from step 3.

The `starter` plan in `render.yaml` (~$7/mo) is what keeps it always-on;
Render's free tier spins the app down after 15 minutes of inactivity and
takes ~30 seconds to wake back up on the next request.

Since `LOOKUP_USER`/`LOOKUP_PASSWORD` are unset for local runs, running it
locally with `./start.sh` stays exactly as frictionless as before — the
login prompt only appears once those two are set (which Render does for
you as part of step 3).
