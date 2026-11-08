#!/bin/bash
# Starts the local auth bridge (if needed) and the 红果 desktop beta with the
# OAuth/playback environment it needs, then restores the saved account in the
# UI without asking for a scan. State lives in ~/Library/Application Support/HongguoBridge.
set -u
STATE="$HOME/Library/Application Support/HongguoBridge"
APP="/Applications/红果免费短剧.app"
BIN="$APP/Contents/MacOS/hongguo-desktop-companion"
PY="$STATE/venv/bin/python"
[ -x "$PY" ] || PY="/usr/bin/python3"

health() { curl -fsS --max-time 2 http://127.0.0.1:8787/health >/dev/null 2>&1; }

if ! health; then
  mkdir -p "$STATE"
  nohup "$PY" "$STATE/app/bridge.py" --port 8787 >>"$STATE/bridge.log" 2>&1 &
  for _ in $(seq 1 40); do
    health && break
    sleep 0.15
  done
fi

if pgrep -f "$BIN$" >/dev/null 2>&1; then
  /usr/bin/open -b cn.guoban.desktop-companion 2>/dev/null || true
else
  for _ in $(seq 1 40); do
    health && break
    sleep 0.15
  done
  export HONGGUO_DESKTOP_AUTH_BASE_URL=http://127.0.0.1:8787
  export HONGGUO_DESKTOP_AUTH_CLIENT_ID=test-client
  export HONGGUO_DESKTOP_PLAYBACK_PATH=/playback
  nohup "$BIN" >>"$STATE/app.log" 2>&1 &
  for _ in $(seq 1 40); do
    pgrep -f "$BIN$" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

nohup /usr/bin/osascript -l JavaScript "$STATE/bin/autologin.js" >>"$STATE/autologin.log" 2>&1 &
exit 0
