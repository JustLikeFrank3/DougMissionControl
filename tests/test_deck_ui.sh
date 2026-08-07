#!/usr/bin/env bash
# Wrapper so `bash tests/test_*.sh` picks up the panel's unit tests alongside
# the shell ones. Skips rather than fails where node is absent — the Pi runs
# the panel in Chromium and has no reason to have node installed.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v node >/dev/null 2>&1; then
    echo "SKIP: deck-ui unit tests need node (not installed here)"
    exit 0
fi
exec node "$here/test_deck_ui.mjs"
