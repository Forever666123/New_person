#!/usr/bin/env bash
#
# 每天把她的记忆传到这台机器外面去。
#
# 这台服务器随时可能没了——欠费、误删、供应商故障、你自己 rm 错一个目录。
# 代码和人设都在 git 里，丢了十分钟就能重来；data/newperson.db 不行，
# 它是你们全部的对话，只此一份。
#
# 流程：一致快照 -> 当场验 -> 加密 -> 传走 -> 确认传到了 -> 删旧的 -> 记时间。
# 任何一步失败都退出非零，并且**不写** .last_backup_at，
# 这样 `!np status` 会一直显示"上次备份是很久以前"，而不是骗你说刚备过。
#
# 不依赖任何特定的跑法。取快照只需要一个能 import newperson 的 Python，
# 剩下的步骤跟她是 systemd 起的还是容器里跑的没有关系。
#
# 装：cp scripts/backup.env.example scripts/backup.env && nano scripts/backup.env
# 跑：scripts/backup.sh
# 定时：0 4 * * * /opt/New_person/scripts/backup.sh >> /var/log/chloe-backup.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NP_DIR="$(dirname "$SCRIPT_DIR")"
cd "$NP_DIR"

# shellcheck source=/dev/null
[ -f "$SCRIPT_DIR/backup.env" ] && . "$SCRIPT_DIR/backup.env"

RCLONE_REMOTE="${RCLONE_REMOTE:-}"
GPG_PASSPHRASE_FILE="${GPG_PASSPHRASE_FILE:-/root/.chloe-backup-pass}"
KEEP_DAYS="${KEEP_DAYS:-30}"
KEEP_LOCAL="${KEEP_LOCAL:-3}"
PYTHON_BIN="${PYTHON_BIN:-$NP_DIR/.venv/bin/python}"
DB_PATH="${DB_PATH:-$NP_DIR/data/newperson.db}"

die() { echo "✗ $*" >&2; exit 1; }
say() { echo "[$(date -u +%FT%TZ)] $*"; }

[ -n "$RCLONE_REMOTE" ] || die "没配 RCLONE_REMOTE。看 scripts/backup.env.example"
# 结尾少一个斜杠，"$RCLONE_REMOTE$NAME" 就会拼成 b2:bucketnewperson-xxx.db.gpg。
# 更糟的是它不报错：上传照样成功（传到一个叫 bucketnewperson-... 的地方），
# 而 restore.sh 用同样的拼法去下载就找不到。每天都显示 ✓，一份都恢复不了。
# 示例配置里只写了句注释提醒，注释拦不住任何人，这里直接补上。
case "$RCLONE_REMOTE" in
    */|*:) ;;
    *) RCLONE_REMOTE="$RCLONE_REMOTE/" ;;
esac
[ -r "$GPG_PASSPHRASE_FILE" ] || die "读不到密码文件 $GPG_PASSPHRASE_FILE"
[ -f "$DB_PATH" ] || die "找不到数据库 $DB_PATH。检查 backup.env 里的 DB_PATH"
# cron 的 PATH 通常只有 /usr/bin:/bin，装在 /usr/local/bin 的东西找不到。
# 这两条比"半夜静默失败"友好得多。
command -v rclone >/dev/null || die "找不到 rclone（cron 的 PATH 很窄，可以在 backup.env 里写 PATH=...）"
command -v gpg >/dev/null || die "找不到 gpg"
# 先确认这个 Python 能 import newperson。不查的话下面失败只会给你一个退出码 1，
# 而真正的原因（虚拟环境里没装这个包、PYTHON_BIN 指错了）藏在 stderr 里。
# shellcheck disable=SC2086
$PYTHON_BIN -c "import newperson.config" >/dev/null 2>&1 \
    || die "$PYTHON_BIN 跑不了 newperson（包或依赖缺失）。虚拟环境对吗？（cd $NP_DIR && .venv/bin/pip install -e .）"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="newperson-$STAMP.db.gpg"
# mktemp -d 默认 700，明文快照只在这里待几秒，不落到 data/ 里去。
WORK="$(mktemp -d)"
SNAP="$WORK/snapshot.db"
trap 'rm -rf "$WORK"' EXIT

# ---- 1. 一致快照 -----------------------------------------------------------
# 她正开着数据库在写。用的是 SQLite 的在线备份接口（见 newperson/backup.py），
# 所以**不用停服务**：她一边写，我们一边拷，拿到的仍然是一致的快照。
# 拷完那一步自己会跑 integrity_check，坏了它删掉文件并退非零。
#
# $PYTHON_BIN 故意不加引号：允许它是一条多词命令，
# 比如容器部署可以写 PYTHON_BIN="docker compose exec -T newperson python"。
# 返回 3 表示拷出来了但里面一条消息都没有（见 __main__.EMPTY_BACKUP）。
# 那种情况照传，但**不清理旧备份**——否则 DB_PATH 配错的那天起，
# 每天一份空备份，一个月之后把所有真备份全顶掉了，而全程没有一句报错。
EMPTY_BACKUP=3
say "拷快照"
set +e
# shellcheck disable=SC2086
$PYTHON_BIN -m newperson backup "$SNAP" --db "$DB_PATH"
SNAP_RC=$?
set -e
case "$SNAP_RC" in
    0) PRUNE=yes ;;
    "$EMPTY_BACKUP") PRUNE=no; say "!! 空备份，这一轮不清理旧的，也不记时间" ;;
    127) die "跑不起来 $PYTHON_BIN —— 路径对吗？虚拟环境里装了 newperson 吗？" ;;
    *) die "快照失败（$SNAP_RC）" ;;
esac
[ -s "$SNAP" ] || die "快照文件不见了：$SNAP"

# ---- 2. 加密 --------------------------------------------------------------
# 这个文件是你们全部的对话。它要躺在别人的硬盘上，就不该是明文。
# gpg 自带压缩，不用再 gzip 一遍。
say "加密"
gpg --batch --yes --quiet --symmetric --cipher-algo AES256 \
    --passphrase-file "$GPG_PASSPHRASE_FILE" \
    --output "$WORK/$NAME" "$SNAP" || die "加密失败"

# ---- 3. 传走，并且确认真的传到了 ------------------------------------------
# rclone copy 成功退出不等于对面有这个文件。多问一句，几乎不要钱。
say "上传 $NAME"
rclone copy "$WORK/$NAME" "$RCLONE_REMOTE" || die "上传失败"
rclone lsf "$RCLONE_REMOTE" 2>/dev/null | grep -qx "$NAME" || die "传完了但对面没有这个文件"

# 回读大小。原来这里 sed 匹配不到时会得到空串，而空串会让下面的比较**整个被跳过**，
# 于是"确认传到了"这一步在出问题时恰好什么都不确认。现在读不出数字就是失败。
LOCAL_SIZE="$(wc -c < "$WORK/$NAME")"
REMOTE_SIZE="$(rclone size --json "$RCLONE_REMOTE$NAME" 2>/dev/null | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p' || true)"
case "$REMOTE_SIZE" in
    ''|*[!0-9]*) die "读不出对面那份的大小（rclone size 没给出数字）。这一轮不清理旧备份" ;;
esac
[ "$REMOTE_SIZE" = "$LOCAL_SIZE" ] || die "大小对不上：本地 $LOCAL_SIZE，对面 $REMOTE_SIZE。这一轮不清理旧备份"

# ---- 4. 本地也留几份，顺手清掉旧的 ----------------------------------------
# 本地这几份是为了"手滑删了数据库"这种当场就发现的事故，不算异地备份。
mkdir -p "$NP_DIR/backups"
cp "$WORK/$NAME" "$NP_DIR/backups/$NAME"
ls -1t "$NP_DIR/backups"/newperson-*.db.gpg 2>/dev/null | tail -n +$((KEEP_LOCAL + 1)) | while read -r old; do
    rm -f "$old"
done

if [ "$PRUNE" = yes ]; then
    say "清理 $KEEP_DAYS 天以前的远端备份"
    # 限定文件名：这个 bucket 里可能还有别的东西，别替人家做主
    rclone delete --min-age "${KEEP_DAYS}d" --include 'newperson-*.db.gpg' "$RCLONE_REMOTE" || true
    # B2 删除只是打个隐藏标记，旧版本还在按量收钱。cleanup 才是真的删。
    rclone cleanup "$RCLONE_REMOTE" 2>/dev/null || true
fi

# ---- 5. 记时间。只有走到这里才算真的备份过 --------------------------------
# 格式要能被 newperson.backup.last_backup_at 解析，tests/test_backup.py 盯着这个格式。
if [ "$PRUNE" = yes ]; then
    date -u +%FT%TZ > "$(dirname "$DB_PATH")/.last_backup_at"
else
    say "!! 不更新 .last_backup_at —— \`!np status\` 会继续提醒你备份有问题"
fi

REMAINING="$(rclone lsf "$RCLONE_REMOTE" 2>/dev/null | grep -c 'newperson-.*\.db\.gpg' || true)"
say "✓ 好了。远端现在有 $REMAINING 份"
