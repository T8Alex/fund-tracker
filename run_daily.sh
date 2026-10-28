#!/bin/zsh
set -u
cd "$(dirname "$0")"
export TZ="Asia/Shanghai"

# launchd 的 PATH 很精简，这里显式补上用户级 Python 和 Homebrew 路径
export PATH="/Users/t8alex/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PYTHON_BIN="/Users/t8alex/.local/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

mkdir -p data reports
"$PYTHON_BIN" -m fund_tracker run "$@" >> data/cron.log 2>&1
code=$?
"$PYTHON_BIN" -m shared.run_ledger append --path data/run_ledger.jsonl --job fund-run --event process --exit-code "$code" >> data/cron.log 2>&1
exit "$code"
