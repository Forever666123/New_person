#!/usr/bin/env bash
#
# 她出事的时候告诉你一声：往你 Discord 里一个私密频道的 webhook 发一句话。
#
# 为什么需要：她的正常状态就包含长时间不说话，所以"坏了"和"她这会儿不想聊"
# 从外面看一模一样。服务被 systemd 熔断停掉、备份失败、每周体检发现坏了——
# 原来这些只写进服务器上的日志，而那里没人去看。
#
# **不走她的私聊，也不走她的 bot 账号**：发到你自己服务器里的一个频道，
# 她的对话和补抓都看不到它。只发计数、时刻、错误类别，不带聊天内容。
#
# 装：Discord 服务器 -> 建一个只有你能看的频道 -> 频道设置 -> 整合 -> Webhooks
#     -> 新建 -> 复制 URL，然后
#       echo 'ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...' > scripts/alert.env
#       chmod 600 scripts/alert.env
# 试：scripts/alert.sh 测试一下
#
# 没配 URL 就只往 stderr 打一行，退出 0：它不能连累调它的那个脚本。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[ -f "$SCRIPT_DIR/alert.env" ] && . "$SCRIPT_DIR/alert.env"
URL="${ALERT_WEBHOOK_URL:-}"
TEXT="${*:-（没有内容）}"

if [ -z "$URL" ]; then
    echo "[alert] 没配 ALERT_WEBHOOK_URL，这条只写在这里：$TEXT" >&2
    exit 0
fi

# JSON 交给 python 拼：中文、引号、换行手拼一定会错。
# allowed_mentions 置空：报警里带个 @everyone 字样也不会真的去 @ 谁
PAYLOAD="$(printf '%s' "$TEXT" | python3 -c '
import json, sys
text = sys.stdin.read()[:1900]
print(json.dumps({"content": "[chloe] " + text, "allowed_mentions": {"parse": []}}))
')" || { echo "[alert] 拼不出消息" >&2; exit 0; }

curl -fsS --max-time 15 --retry 3 -H "Content-Type: application/json" \
    -d "$PAYLOAD" "$URL" >/dev/null \
    || echo "[alert] 发不出去（webhook 失效了？）：$TEXT" >&2
exit 0
