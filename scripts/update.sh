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

# 看的是"上次真正装好、重启成功的是哪个版本"，不是"这次拉没拉到新东西"。
# 原来按拉取前后比：上一次在装依赖或检查那一步失败了，代码已经拉下来，
# 再跑一次就会说"已经是最新的"然后退出 0——服务其实还在跑旧进程。
#
# （这个脚本自己也可能被上面那句 git pull 换掉。bash 是边读边跑的，
#  所以 git pull 那一行和它之前的内容一个字节都不要改。）
deployed="$(cat "$NP_DIR/.deployed_rev" 2>/dev/null || true)"
if [ "$before" = "$after" ] && [ "$deployed" = "$after" ]; then
    say "已经是最新的（$after），不用重启"
    exit 0
fi

if [ "$before" = "$after" ]; then
    say "代码已经是 $after，但上次没装完（或者头一回用这个脚本），这次补上"
else
    say "$before → $after"
    git --no-pager log --oneline "$before..$after" | sed 's/^/    /'
fi

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
systemctl is-active --quiet "$SERVICE_NAME" || {
    echo "✗ 起来之后又挂了。最近的日志：" >&2
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager >&2 || true
    exit 1
}
echo "$after" > "$NP_DIR/.deployed_rev"

# 顺手体检一遍。**故意不让它决定退出码**：她刚起来，
# 重启前那几条还没回的消息会让 doctor 判 BAD，而那不是这次更新的问题，
# 一个会误报的收尾只会让你以后不敢看它。有事它自己会说出来。
say "体检"
"$PYTHON_BIN" -m newperson doctor 2>&1 | sed 's/^/    /' || true

say "✓ 好了。看日志：journalctl -u $SERVICE_NAME -f"
