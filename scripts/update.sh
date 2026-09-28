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
since="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl restart "$SERVICE_NAME"
sleep 3
systemctl is-active --quiet "$SERVICE_NAME" || {
    echo "✗ 起不来了。看 journalctl -u $SERVICE_NAME -n 50" >&2
    exit 1
}

# 等她连上 Discord、把记忆打开。数据库要是加了新列，就是这一步补上的；
# 不等的话体检会抢在前面，报一串"这一项没查成"，看着像更新坏了。
say "等她上线"
up=0
for _ in $(seq 60); do
    n="$(journalctl -u "$SERVICE_NAME" --since "$since" -o cat 2>/dev/null | grep -c '记忆在' || true)"
    if [ "${n:-0}" -gt 0 ]; then up=1; break; fi
    sleep 2
done
[ "$up" = 1 ] || say "!! 两分钟了还没看到她上线（多半是 Discord 连得慢），下面的体检可能不准"

# 顺手体检一遍。**故意不让它决定退出码**：她刚起来，
# 重启前那几条还没回的消息会让 doctor 判 BAD，而那不是这次更新的问题，
# 一个会误报的收尾只会让你以后不敢看它。有事它自己会说出来。
say "体检"
"$PYTHON_BIN" -m newperson doctor 2>&1 | sed 's/^/    /' || true

say "✓ 好了。看日志：journalctl -u $SERVICE_NAME -f"
