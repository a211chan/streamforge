#!/usr/bin/env bash
# 開発用の起動スクリプト。常駐は launchd から同じコマンドを叩く。
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m app.main
