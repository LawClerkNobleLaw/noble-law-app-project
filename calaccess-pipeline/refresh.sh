#!/bin/zsh
# Runs the daily CAL-ACCESS refresh job once, then exits.
# No API key needed — the raw export is free, no login required — but this
# still matches legiscan-lookup/refresh.sh's shape for consistency.
#
# `caffeinate -i` keeps the Mac from idle-sleeping while this runs — the
# 1.5GB download takes a few minutes, long enough for an idle laptop to
# doze off mid-transfer and drop the connection. It can't stop an actual
# lid-close, only idle/display sleep, but that covers the ordinary
# "left the Mac sitting overnight" case this job runs unattended in.

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
caffeinate -i python3 refresh_calaccess.py
