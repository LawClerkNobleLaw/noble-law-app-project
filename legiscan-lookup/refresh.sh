#!/bin/zsh
# Runs the daily watch-list refresh job once, then exits.
# Sources ~/.zshrc first so LEGISCAN_API_KEY is available — launchd (which
# runs this on a schedule) does not read shell profiles on its own, the
# same reason start.sh does this for the live app.

DIR="$(cd "$(dirname "$0")" && pwd)"
source ~/.zshrc 2>/dev/null
cd "$DIR"
python3 refresh_watchlist.py
