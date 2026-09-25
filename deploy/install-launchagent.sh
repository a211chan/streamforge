#!/usr/bin/env bash
# StreamForge を常駐させる（macOS / launchd）。
# 常駐させておけば、遠隔からは「起動」ではなくトンネルを張るだけで済む。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="tv.streamforge.api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/StreamForge/logs"

mkdir -p "$(dirname "$PLIST")" "$LOGDIR"

if [ ! -x "$REPO/run.sh" ]; then
  echo "run.sh が見つからないか実行権限がありません: $REPO/run.sh" >&2
  exit 1
fi
if [ ! -d "$REPO/.venv" ]; then
  echo ".venv がありません。先にセットアップしてください:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

sed -e "s|@@REPO@@|$REPO|g" -e "s|@@LOGDIR@@|$LOGDIR|g" \
    "$REPO/deploy/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

# 入れ直しても二重起動しないよう、いったん外してから入れる
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "登録しました: $PLIST"
echo "状態:  launchctl print gui/$(id -u)/$LABEL | head -20"
echo "停止:  launchctl bootout gui/$(id -u)/$LABEL"
echo "ログ:  $LOGDIR/streamforge.{out,err}.log"
