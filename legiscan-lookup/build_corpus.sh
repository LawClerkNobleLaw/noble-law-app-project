#!/bin/zsh
# Tops up the searchable bill corpus once, then exits.
#
# Separate from refresh.sh for the reason build_bill_corpus.py's own
# docstring gives: that job is the nightly must-run behind the digest
# email and touches a handful of bills; this one walks the session and
# is measured in thousands of API calls. They should not queue behind
# each other.
#
# Sources ~/.zshrc first so LEGISCAN_API_KEY is available — launchd does
# not read shell profiles on its own, same reason refresh.sh does it.

DIR="$(cd "$(dirname "$0")" && pwd)"
source ~/.zshrc 2>/dev/null
cd "$DIR"
python3 build_bill_corpus.py
