#!/usr/bin/env bash
#
# 每天把她的记忆传到这台机器外面去。
#
# 这台服务器随时可能没了——欠费、误删、供应商故障、你自己 rm 错一个目录。
# 代码和人设都在 git 里，丢了十分钟就能重来；`data/newperson.db` 不行，
# 它是你们全部的对话，只此一份。
#
# 流程：一致快照 -> 当场验 -> 加密 -> 传走 -> 确认传到了 -> 删旧的 -> 记时间。
# 任何一步失败都退出非零，并且**不写** .last_backup_at，
# 这样 `!np status` 会一直显示"上次备份是很久以前"，而不是骗你说刚备过。
#
# 装：cp scripts/backup.env.example scripts/backup.env && nano scripts/backup.env
# 跑：scripts/backup.sh
# 定时：0 4 * * * /root/New_person/scripts/backup.sh >> /var/log/chloe-backup.log 2>&1

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
DATA_DIR="${DATA_DIR:-$NP_DIR/data}"
DB_NAME="${DB_NAME:-newperson.db}"
COMPOSE="${COMPOSE:-docker compose}"

die() { echo "✗ $*" >&2; exit 1; }
say() { echo "[$(date -u +%FT%TZ)] $*"; }

[ -n "$RCLONE_REMOTE" ] || die "没配 RCLONE_REMOTE。看 scripts/backup.env.example"
[ -r "$GPG_PASSPHRASE_FILE" ] || die "读不到密码文件 $GPG_PASSPHRASE_FILE"
command -v rclone >/dev/null || die "没装 rclone"
command -v gpg >/dev/null || die "没装 gpg"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="newperson-$STAMP.db.gpg"
WORK="$(mktemp -d)"
# 快照必须落在 data/ 里：容器只有这个目录可写，而快照是容器里的进程生成的。
SNAP_HOST="$DATA_DIR/.snapshot.db"
cleanup() { rm -rf "$WORK" "$SNAP_HOST"; }
trap cleanup EXIT

# ---- 1. 一致快照（顺带跑 integrity_check，坏了它自己会删掉并退非零）----------
# 优先用正在跑的那个容器。她没在跑的时候数据库是静止的，
# 起一个一次性容器同样安全——不能因为容器停了就三周没有备份。
# 返回 3 表示拷出来了但里面一条消息都没有（见 __main__.EMPTY_BACKUP）。
# 那种情况照传，但**不清理旧备份**——否则 DB_PATH 配错的那天起，
# 每天一份空备份，一个月之后把所有真备份全顶掉了，而全程没有任何报错。
EMPTY_BACKUP=3
say "拷快照"
set +e
if $COMPOSE ps --status running --services 2>/dev/null | grep -qx newperson; then
    $COMPOSE exec -T newperson python -m newperson backup "/app/data/.snapshot.db"
else
    say "容器没在跑，用一次性容器拷"
    $COMPOSE run --rm --no-deps -T newperson python -m newperson backup "/app/data/.snapshot.db"
fi
SNAP_RC=$?
set -e
case "$SNAP_RC" in
    0) PRUNE=yes ;;
    "$EMPTY_BACKUP") PRUNE=no; say "!! 空备份，这一轮不清理旧的" ;;
    *) die "快照失败（$SNAP_RC）" ;;
esac
[ -s "$SNAP_HOST" ] || die "快照文件不见了：$SNAP_HOST"

# ---- 2. 加密 --------------------------------------------------------------
# 这个文件是你们全部的对话。它要躺在别人的硬盘上，就不该是明文。
# gpg 自带压缩，不用再 gzip 一遍。
say "加密"
gpg --batch --yes --quiet --symmetric --cipher-algo AES256 \
    --passphrase-file "$GPG_PASSPHRASE_FILE" \
    --output "$WORK/$NAME" "$SNAP_HOST" || die "加密失败"

# ---- 3. 传走，并且确认真的传到了 ------------------------------------------
# rclone copy 成功退出不等于对面有这个文件。多问一句，几乎不要钱。
say "上传 $NAME"
rclone copy "$WORK/$NAME" "$RCLONE_REMOTE" || die "上传失败"
rclone lsf "$RCLONE_REMOTE" 2>/dev/null | grep -qx "$NAME" || die "传完了但对面没有这个文件"

LOCAL_SIZE="$(wc -c < "$WORK/$NAME")"
REMOTE_SIZE="$(rclone size --json "$RCLONE_REMOTE$NAME" 2>/dev/null | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p' || true)"
if [ -n "$REMOTE_SIZE" ] && [ "$REMOTE_SIZE" != "$LOCAL_SIZE" ]; then
    die "大小对不上：本地 $LOCAL_SIZE，对面 $REMOTE_SIZE"
fi

# ---- 4. 本地也留几份，顺手清掉旧的 ----------------------------------------
# 本地这几份是为了"手滑删了数据库"这种当场就发现的事故，不算异地备份。
mkdir -p "$NP_DIR/backups"
cp "$WORK/$NAME" "$NP_DIR/backups/$NAME"
ls -1t "$NP_DIR/backups"/newperson-*.db.gpg 2>/dev/null | tail -n +$((KEEP_LOCAL + 1)) | while read -r old; do
    rm -f "$old"
done

if [ "$PRUNE" = yes ]; then
    say "清理 $KEEP_DAYS 天以前的远端备份"
    rclone delete --min-age "${KEEP_DAYS}d" "$RCLONE_REMOTE" || true
    # B2 删除只是打个隐藏标记，旧版本还在按量收钱。cleanup 才是真的删。
    rclone cleanup "$RCLONE_REMOTE" 2>/dev/null || true
fi

# ---- 5. 记时间。只有走到这里才算真的备份过 --------------------------------
# 格式要能被 newperson.backup.last_backup_at 解析，tests/test_backup.py 盯着这个格式。
if [ "$PRUNE" = yes ]; then
    date -u +%FT%TZ > "$DATA_DIR/.last_backup_at"
else
    say "!! 不更新 .last_backup_at —— \`!np status\` 会继续提醒你备份有问题"
fi

REMAINING="$(rclone lsf "$RCLONE_REMOTE" 2>/dev/null | grep -c 'newperson-.*\.db\.gpg' || true)"
say "✓ 好了。远端现在有 $REMAINING 份"
