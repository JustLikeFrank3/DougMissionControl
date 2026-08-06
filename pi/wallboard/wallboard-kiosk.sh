#!/bin/bash
# Single-instance guard: relaunching without killing the old wrapper bred
# 4 loops x 68 chromium processes and starved the Pi (2026-07-20 incident).
exec 9>/run/user/1000/wallboard-kiosk.lock
flock -n 9 || { echo "wallboard-kiosk already running"; exit 0; }
# localhost (chromium runs on the Pi itself) — immune to DHCP lease
# changes, which stranded the kiosk on a hardcoded LAN IP before
# (2026-07-15 incident).
G="http://localhost:3000"
# The workstation dual-boots: the gpu-windows scrape job is up only when
# it is in Windows, so this one probe picks the playlist (Prometheus via
# the anonymous Grafana datasource proxy — no credentials on disk).
PROBE="$G/api/datasources/proxy/uid/prometheus/api/v1/query?query=max(up%7Bjob%3D%22gpu-windows%22%7D)"

mode() {
  local resp
  # Distinguish "probe unreachable" (unknown — do not flap the kiosk on a
  # network blip) from a real answer.
  resp=$(curl -sf --max-time 5 "$PROBE") || { echo unknown; return; }
  if echo "$resp" | grep -q ",\"1\"\]"; then echo windows; else echo linux; fi
}

playlist_url() {
  # Resolve uid by name at runtime — playlist uids are minted by Grafana,
  # so baking one here broke every time the playlist was recreated.
  local uid
  uid=$(curl -sf --max-time 5 "$G/api/playlists" | python3 -c "import json,sys; print(next((p[\"uid\"] for p in json.load(sys.stdin) if p[\"name\"]==sys.argv[1]), \"\"))" "jcmcp-wallboard-$1")
  [ -n "$uid" ] && echo "$G/playlists/play/$uid?kiosk"
}

while true; do
  until curl -sf -o /dev/null --max-time 3 "$G/api/health"; do
    sleep 5
  done
  MODE=$(mode)
  [ "$MODE" = unknown ] && MODE=linux
  URL=$(playlist_url "$MODE")
  if [ -z "$URL" ]; then
    echo "playlist jcmcp-wallboard-$MODE not found — run pi-deploy.sh wallboard"
    sleep 30
    continue
  fi
  chromium --password-store=basic --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble "$URL" &
  CHROME_PID=$!
  fails=0
  while kill -0 "$CHROME_PID" 2>/dev/null; do
    sleep 30
    # Cursor auto-hide: re-park the pointer offscreen (wlroots virtual
    # pointer via wlrctl); a bumped mouse gets ~30s of fame, then hides.
    wlrctl pointer move 9999 9999 2>/dev/null
    if curl -sf -o /dev/null --max-time 5 "$URL"; then
      fails=0
    else
      fails=$((fails + 1))
    fi
    NEW_MODE=$(mode)
    if [ "$NEW_MODE" != unknown ] && [ "$NEW_MODE" != "$MODE" ]; then
      # The booted OS changed — swap to the matching playlist.
      kill "$CHROME_PID" 2>/dev/null
      wait "$CHROME_PID" 2>/dev/null
      break
    fi
    if [ "$fails" -ge 3 ]; then
      kill "$CHROME_PID" 2>/dev/null
      wait "$CHROME_PID" 2>/dev/null
      break
    fi
  done
done
