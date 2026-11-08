#!/bin/bash
# Install the local Mac beta patch:
#   - persistent local login bridge on 127.0.0.1:8787
#   - 红果搜片 web UI backed by the app's own Python backend
#   - backend search / playback compatibility files
#   - /Applications/红果搜片.app Dock helper
#
# Requires 红果免费短剧 1.0.9 beta.2 for Apple Silicon installed in /Applications.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${HONGGUO_APP:-/Applications/红果免费短剧.app}"
BACKEND="$APP/Contents/Resources/backend"
LAUNCHER="$APP/Contents/MacOS/hongguo-desktop-companion"
STATE="${HONGGUO_STATE:-$HOME/Library/Application Support/HongguoBridge}"
SEARCH_APP="${HONGGUO_SEARCH_APP:-/Applications/红果搜片.app}"
NO_START=0
if [ "${1:-}" = "--no-start" ]; then
  NO_START=1
fi

if [ ! -d "$BACKEND" ] || [ ! -x "$LAUNCHER" ]; then
  echo "未找到 /Applications/红果免费短剧.app（Mac 1.0.9 beta.2）。请先安装测试版。" >&2
  exit 1
fi

find_python() {
  local candidate
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python)" || {
  echo "需要 Python 3.10 或更新版本（桥接只使用标准库）。" >&2
  exit 1
}
echo "使用 Python: $PYTHON"

mkdir -p "$STATE/app" "$STATE/bin" "$STATE/backup"
if [ ! -x "$STATE/venv/bin/python" ]; then
  "$PYTHON" -m venv "$STATE/venv"
fi

cp "$ROOT/app/bridge.py" "$STATE/app/bridge.py"
cp "$ROOT/app/lab.html" "$STATE/app/lab.html"
cp "$ROOT/bin/launch.sh" "$STATE/bin/launch.sh"
cp "$ROOT/bin/autologin.js" "$STATE/bin/autologin.js"
chmod +x "$STATE/bin/launch.sh"

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$STATE/backup/$STAMP"
mkdir -p "$BACKUP"
for source in "$ROOT"/backend/*.py; do
  name="$(basename "$source")"
  if [ -f "$BACKEND/$name" ]; then
    cp "$BACKEND/$name" "$BACKUP/$name"
  fi
  cp "$source" "$BACKEND/$name"
done
if [ -d "$ROOT/third_party/phone-account-protocol" ] && [ ! -d "$BACKEND/phone-account-protocol" ]; then
  rsync -a "$ROOT/third_party/phone-account-protocol/" "$BACKEND/phone-account-protocol/"
fi
echo "后端补丁已写入（原文件备份在 $BACKUP）"

rm -rf "$SEARCH_APP"
mkdir -p "$SEARCH_APP/Contents/MacOS" "$SEARCH_APP/Contents/Resources"
cp "$ROOT/bin/search-app/Info.plist" "$SEARCH_APP/Contents/Info.plist"
cp "$ROOT/bin/search-app/launcher" "$SEARCH_APP/Contents/MacOS/launcher"
chmod +x "$SEARCH_APP/Contents/MacOS/launcher"
if [ -f "$APP/Contents/Resources/icon.icns" ]; then
  cp "$APP/Contents/Resources/icon.icns" "$SEARCH_APP/Contents/Resources/icon.icns"
fi
codesign --force --deep --sign - "$SEARCH_APP" >/dev/null 2>&1 || true
echo "已创建 $SEARCH_APP"

if [ "$NO_START" -eq 0 ]; then
  pkill -f "bridge.py --port 8787" >/dev/null 2>&1 || true
  sleep 0.5
  bash "$STATE/bin/launch.sh" >/dev/null 2>&1 || true
fi

echo
echo "安装完成。"
echo "  搜索页: http://127.0.0.1:8787/lab"
echo "  Dock:   $SEARCH_APP"
if [ "$NO_START" -eq 0 ] && pgrep -f "$LAUNCHER" >/dev/null 2>&1; then
  echo "红果 App 正在运行；后端补丁需要重启 App 后才会加载。"
fi
