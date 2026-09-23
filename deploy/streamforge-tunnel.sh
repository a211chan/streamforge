#!/usr/bin/env bash
# MacBook 側で実行する。Mac mini の StreamForge へトンネルを張ってブラウザを開く。
#
# StreamForge は 127.0.0.1 にしか待ち受けない（認証が無いため）。
# SSH のポート転送で手元の 127.0.0.1:8080 に繋ぐので、tailnet 上の他の端末からは
# 見えないまま使える。
set -euo pipefail

HOST="${STREAMFORGE_HOST:-macmini}"   # Tailscale のマシン名。IPでも可
PORT="${STREAMFORGE_PORT:-8080}"
ERRLOG="$(mktemp -t streamforge-ssh)"
trap 'rm -f "$ERRLOG"' EXIT

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "127.0.0.1:$PORT は既に使われています。別のトンネルが生きているかもしれません" >&2
  echo "  確認: lsof -nP -iTCP:$PORT -sTCP:LISTEN" >&2
  exit 1
fi

echo "$HOST へトンネルを張ります（Ctrl-C で切断）"
# -N: コマンドを実行せず転送だけ / ServerAlive*: 切れたら気づけるように
ssh -N \
    -L "127.0.0.1:$PORT:127.0.0.1:$PORT" \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    "$HOST" 2>"$ERRLOG" &
SSH_PID=$!
trap 'kill "$SSH_PID" 2>/dev/null || true; rm -f "$ERRLOG"' EXIT

for _ in $(seq 20); do
  sleep 0.5

  # SSH が死んでいたら、それは StreamForge ではなく接続の問題。
  # 原因を取り違えると確認する場所を間違えるので、ここで分ける
  if ! kill -0 "$SSH_PID" 2>/dev/null; then
    echo "SSH で $HOST に接続できませんでした" >&2
    sed 's/^/  /' "$ERRLOG" >&2
    echo "  ・Mac mini でリモートログインが有効か: sudo systemsetup -getremotelogin" >&2
    echo "  ・Tailscale で見えているか:            tailscale status | grep $HOST" >&2
    exit 1
  fi

  if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/api/system/capabilities"; then
    echo "接続できました → http://127.0.0.1:$PORT"
    open "http://127.0.0.1:$PORT" 2>/dev/null || true
    wait "$SSH_PID"
    exit 0
  fi
done

# SSH は生きている。つまり届いてはいるので、向こう側の StreamForge を疑う
echo "トンネルは張れましたが、StreamForge が応答しません" >&2
echo "  確認: ssh $HOST 'launchctl print gui/\$(id -u)/tv.streamforge.api | head -5'" >&2
echo "  ログ: ssh $HOST 'tail -20 ~/StreamForge/logs/streamforge.err.log'" >&2
exit 1
