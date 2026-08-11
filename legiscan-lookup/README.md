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
the API or credentials involved. Running this as a plain local script
sidesteps that entirely, at the cost of it only being reachable from this
Mac (not a shareable link).
