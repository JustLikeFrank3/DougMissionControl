#!/usr/bin/env bash
# Exercises linux/boot-agent.py's auth matrix against a real listener on a
# throwaway port, with FLIGHTSIM_BOOT_CMD pointed at a stub so a passing
# test never actually reboots the machine.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmpdir="$(mktemp -d)"
port=19108
agent_pid=""
trap 'kill "$agent_pid" 2>/dev/null || true; rm -rf "$tmpdir"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

printf 'test-token-abc123\n' > "$tmpdir/token"
cat > "$tmpdir/fake-boot" <<EOF
#!/bin/bash
echo called >> "$tmpdir/boot-calls"
EOF
chmod 755 "$tmpdir/fake-boot"

FLIGHTSIM_BOOT_PORT="$port" \
FLIGHTSIM_TOKEN_FILE="$tmpdir/token" \
FLIGHTSIM_BOOT_CMD="$tmpdir/fake-boot" \
    python3 "$repo_dir/linux/boot-agent.py" >"$tmpdir/agent.log" 2>&1 &
agent_pid=$!

for _ in $(seq 50); do
    curl -sf --max-time 1 "http://127.0.0.1:$port/status" >/dev/null 2>&1 && break
    sleep 0.1
done

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$port$1"; }

[ "$(code /status)" = 200 ] || fail "/status should be 200"
[ "$(code /)" = 200 ]       || fail "/ should be 200"
[ "$(code /nope)" = 404 ]   || fail "unknown path should be 404"

[ "$(code /reboot)" = 403 ]                || fail "/reboot with no token must be 403"
[ "$(code '/reboot?token=wrong')" = 403 ]  || fail "/reboot with a wrong token must be 403"
[ "$(code '/reboot?token=')" = 403 ]       || fail "/reboot with an empty token must be 403"
# The pre-token behaviour: a bare /reboot rebooted the box. Guard against
# any regression that lets a rejected request reach the boot command.
[ ! -f "$tmpdir/boot-calls" ] || fail "a rejected request ran the boot command"

[ "$(code '/reboot?token=test-token-abc123')" = 200 ] || fail "a good token must be 200"
for _ in $(seq 30); do
    [ -f "$tmpdir/boot-calls" ] && break
    sleep 0.1
done
[ -f "$tmpdir/boot-calls" ] || fail "an accepted request did not run the boot command"

# Fail closed: an unreadable or empty token file must refuse every request
# rather than degrading to "no token required".
: > "$tmpdir/token"
[ "$(code '/reboot?token=test-token-abc123')" = 403 ] || fail "empty token file must fail closed"
rm -f "$tmpdir/token"
[ "$(code '/reboot?token=test-token-abc123')" = 403 ] || fail "missing token file must fail closed"

echo "boot agent auth tests passed"
