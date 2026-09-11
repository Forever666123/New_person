#!/usr/bin/env bash
#
# 拉最新代码、装依赖、重启她。
#
# 存在的理由很朴素：那串命令太长，在手机上或者剪贴板不好使的时候敲不动，
# 而敲错一半会让她停在一个装了新代码但没重启的状态。
#
#   /opt/New_person/scripts/update.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NP_DIR="$(dirname "$SCRIPT_DIR")"
cd "$NP_DIR"

PYTHON_BIN="${PYTHON_BIN:-$NP_DIR/.venv/bin/python}"
SERVICE_NAME="${SERVICE_NAME:-chloe}"

say() { echo "[$(date -u +%FT%TZ)] $*"; }

before="$(git rev-parse --short HEAD)"
say "拉代码"
git pull --ff-only
after="$(git rev-parse --short HEAD)"

if [ "$before" = "$after" ]; then
    say "已经是最新的（$after），不用重启"
    exit 0
fi

say "$before → $after"
git --no-pager log --oneline "$before..$after" | sed 's/^/    /'

say "装依赖"
"$PYTHON_BIN" -m pip install -q -e .

# 先确认新代码能跑起来，再去动正在服务的那个进程。
# 装坏了的话重启只会让她停在崩溃循环里，而那比晚几分钟更新糟得多。
say "检查配置和人设"
"$PYTHON_BIN" -m newperson check || {
    echo "✗ check 没过，**没有重启**。她还在用旧代码跑着，先修好再说。" >&2
    exit 1
}

say "重启"
systemctl restart "$SERVICE_NAME"
sleep 3
systemctl is-active --quiet "$SERVICE_NAME" || {
    echo "✗ 起不来了。看 journalctl -u $SERVICE_NAME -n 50" >&2
    exit 1
}

say "✓ 好了。看日志：journalctl -u $SERVICE_NAME -f"
