#!/usr/bin/env bash
# douglas/update.sh: what it does to a settings file, and what it refuses.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
up="$repo_dir/douglas/update.sh"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

bash -n "$up" || fail "update.sh does not parse"

# A machine provisioned before conversations were kept: the old defaults.
cat > "$tmpdir/old.env" <<'CONF'
# douglas-voice
VOICE_PTT_TOKEN=not-a-real-token
LLM_MAX_TOKENS=400
VOICE_MAX_LISTEN_S=20
VOICE_HISTORY_TURNS=6
VOICE_HISTORY_IDLE_S=300
VOICE_STATE_FILE=/run/douglas/voice.json
CONF
out="$("$up" --settings "$tmpdir/old.env")"
want='# douglas-voice
VOICE_PTT_TOKEN=not-a-real-token
LLM_MAX_TOKENS=1500
VOICE_MAX_LISTEN_S=20
VOICE_HISTORY_CHARS=24000
VOICE_HISTORY_IDLE_S=0
VOICE_STATE_FILE=/run/douglas/voice.json'
[ "$(cat "$tmpdir/old.env")" = "$want" ] || fail "the old defaults were not brought up to date: $(cat "$tmpdir/old.env")"
[ "$(printf '%s\n' "$out" | wc -l)" -eq 3 ] || fail "each of the three changes is said, once: $out"
grep -q 'not-a-real-token' <<<"$out" && fail "a token was printed"

# Run again, nothing changes and nothing is said.
out="$("$up" --settings "$tmpdir/old.env")"
[ -z "$out" ] || fail "a second run said something: $out"
[ "$(cat "$tmpdir/old.env")" = "$want" ] || fail "a second run changed the file"

# What the owner set is the owner's: only an old default is changed.
cat > "$tmpdir/own.env" <<'CONF'
LLM_MAX_TOKENS=800
VOICE_HISTORY_IDLE_S=900
VOICE_HISTORY_CHARS=40000
VOICE_HISTORY_TURNS=6
CONF
"$up" --settings "$tmpdir/own.env" >/dev/null
[ "$(cat "$tmpdir/own.env")" = 'LLM_MAX_TOKENS=800
VOICE_HISTORY_IDLE_S=900
VOICE_HISTORY_CHARS=40000' ] || fail "the owner's own values were changed: $(cat "$tmpdir/own.env")"

# The hour that one version wrote is an old default too.
printf 'VOICE_HISTORY_IDLE_S=3600\n' > "$tmpdir/hour.env"
"$up" --settings "$tmpdir/hour.env" >/dev/null
[ "$(cat "$tmpdir/hour.env")" = 'VOICE_HISTORY_IDLE_S=0' ] || fail "an hour was left as it was"

# The file keeps its mode: every key he holds is in the real one.
chmod 600 "$tmpdir/hour.env"
printf 'LLM_MAX_TOKENS=400\n' >> "$tmpdir/hour.env"
"$up" --settings "$tmpdir/hour.env" >/dev/null
[ "$(stat -c %a "$tmpdir/hour.env")" = 600 ] || fail "the file's mode changed"

"$up" --settings "$tmpdir/none.env" >/dev/null 2>&1 && fail "a file that is not there was taken"
"$up" --settings >/dev/null 2>&1 && fail "no file named was taken"
"$up" everything >/dev/null 2>&1 && fail "an argument it does not know was taken"

# The update itself is root's, and only for a machine that is set up.
if [ "$(id -u)" -ne 0 ]; then
    "$up" >/dev/null 2>&1 && fail "it ran without being root"
elif [ ! -f /etc/douglas/voice.env ]; then
    out="$("$up" 2>&1)" && fail "it ran on a machine Douglas is not set up on"
    grep -q 'provision.sh' <<<"$out" || fail "it did not say what sets a machine up: $out"
fi

# It installs each file it names from the checkout, and runs only installers that are there.
for f in douglas-voice.py douglas-tts.py douglas-voice.service work-setup.sh setup.sh memory-setup.sh switch-setup.sh; do
    [ -f "$repo_dir/douglas/$f" ] || fail "update.sh names douglas/$f, which is not in the checkout"
    grep -q "$f" "$up" || fail "update.sh no longer names $f"
done
grep -Eq 'provision\.sh"|llama\.cpp/build|pip install|apt-get' "$up" && fail "it reaches for the whole build"

# work-setup.sh: which gh it takes for too old (Ubuntu 24.04's cannot print a pull request any more).
setup="$repo_dir/douglas/work-setup.sh"
bash -n "$setup" || fail "work-setup.sh does not parse"
mkdir "$tmpdir/bin"
old_or_not() {      # the version a stand-in gh says it is -> "old" or "new"
    printf '#!/bin/sh\necho "%s"\n' "$1" > "$tmpdir/bin/gh"; chmod +x "$tmpdir/bin/gh"
    ( PATH="$tmpdir/bin:$PATH"; eval "$(sed -n '/^GH_WANT=/p; /^gh_version()/p; /^gh_old()/p' "$setup")"
      if gh_old; then echo old; else echo new; fi )
}
[ "$(old_or_not 'gh version 2.45.0 (2025-07-18 Ubuntu 2.45.0-1ubuntu0.3)')" = old ] || fail "Ubuntu's gh 2.45 was not taken for old"
[ "$(old_or_not 'gh version 2.9.0 (2022-04-01)')" = old ] || fail "2.9 was read as newer than 2.70"
[ "$(old_or_not 'gh version 2.83.1 (2026-10-01)')" = new ] || fail "a current gh was taken for old"
[ "$(old_or_not 'something else entirely')" = old ] || fail "a gh that does not say its version was taken for current"
rm "$tmpdir/bin/gh"
for tool in sed dpkg; do ln -s "$(command -v "$tool")" "$tmpdir/bin/$tool"; done     # a PATH with those and no gh
[ "$( ( PATH="$tmpdir/bin"; eval "$(sed -n '/^GH_WANT=/p; /^gh_version()/p; /^gh_old()/p' "$setup")"
        if gh_old; then echo old; else echo new; fi ) )" = old ] || fail "no gh at all was not taken as one to install"
grep -q 'cli.github.com/packages' "$setup" || fail "work-setup.sh no longer gets gh from GitHub's own source"

echo "douglas update tests passed"
