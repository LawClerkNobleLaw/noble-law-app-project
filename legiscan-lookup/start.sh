#!/bin/zsh
# Launches the LegiScan Bill Lookup app.
# Sources ~/.zshrc first so LEGISCAN_API_KEY is available even if this
# script is run from a shell that hasn't loaded your profile.

DIR="$(cd "$(dirname "$0")" && pwd)"
source ~/.zshrc 2>/dev/null
cd "$DIR"
python3 app.py
