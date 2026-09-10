#!/usr/bin/env bash
#
# 从备份里把她拿回来。
#
# 默认是**演练**：下载、解密、验、把里面有什么打给你看，然后删掉临时文件，
# 一根手指都不碰线上的数据库。备份最常见的死法是备了一年从来没人试过能不能恢复，
# 所以这个脚本设计成可以随时无害地跑一遍。
#
#   scripts/restore.sh                    演练最新的一份
#   scripts/restore.sh --at 20260912      演练指定那天的（前缀匹配）
#   scripts/restore.sh --list             看远端有哪些
#   scripts/restore.sh --install          真的装回去（会停容器、会留下当前这份）
#
# 定期演练：0 5 * * 0 /root/New_person/scripts/restore.sh >> /var/log/chloe-restore-drill.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NP_DIR="$(dirname "$SCRIPT_DIR")"
cd "$NP_DIR"

# shellcheck source=/dev/null
[ -f "$SCRIPT_DIR/backup.env" ] && . "$SCRIPT_DIR/backup.env"

RCLONE_REMOTE="${RCLONE_REMOTE:-}"
GPG_PASSPHRASE_FILE="${GPG_PASSPHRASE_FILE:-/root/.chloe-backup-pass}"
DATA_DIR="${DATA_DIR:-$NP_DIR/data}"
DB_NAME="${DB_NAME:-newperson.db}"
COMPOSE="${COMPOSE:-docker compose}"

MODE="drill"
WANT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --install) MODE="install" ;;
        --list)    MODE="list" ;;
        --at)      WANT="${2:-}"; shift ;;
        *)         echo "不认识 $1" >&2; exit 2 ;;
    esac
    shift
done

die() { echo "✗ $*" >&2; exit 1; }
say() { echo "[$(date -u +%FT%TZ)] $*"; }

[ -n "$RCLONE_REMOTE" ] || die "没配 RCLONE_REMOTE。看 scripts/backup.env.example"
command -v rclone >/dev/null || die "没装 rclone"

if [ "$MODE" = "list" ]; then
    rclone lsl "$RCLONE_REMOTE" | sort -k4
    exit 0
fi

[ -r "$GPG_PASSPHRASE_FILE" ] || die "读不到密码文件 $GPG_PASSPHRASE_FILE"

# ---- 挑一份 ---------------------------------------------------------------
if [ -n "$WANT" ]; then
    PICK="$(rclone lsf "$RCLONE_REMOTE" | grep "newperson-$WANT" | sort | tail -1 || true)"
    [ -n "$PICK" ] || die "远端没有 $WANT 那天的备份。先 --list 看看有哪些"
else
    PICK="$(rclone lsf "$RCLONE_REMOTE" | grep '^newperson-.*\.db\.gpg$' | sort | tail -1 || true)"
    [ -n "$PICK" ] || die "远端一份备份都没有"
fi

WORK="$(mktemp -d)"
chmod 755 "$WORK"   # 容器里是 uid 10001，得读得到
trap 'rm -rf "$WORK"' EXIT

say "取 $PICK"
rclone copy "$RCLONE_REMOTE$PICK" "$WORK/" || die "下载失败"
# rclone copy 退出 0 不等于本地真的多了一个文件（过滤规则不匹配、远端文件是空的
# 都会这样）。不查这一下，下一步会以"解密失败"的名义报错，把你引到错误的方向去。
[ -s "$WORK/$PICK" ] || die "下载完了但本地没有 $PICK"

say "解密"
gpg --batch --yes --quiet --decrypt \
    --passphrase-file "$GPG_PASSPHRASE_FILE" \
    --output "$WORK/restored.db" "$WORK/$PICK" \
    || die "解密失败。要么密码文件不是当初加密用的那个，要么这份备份在传输或存储中被改坏了（gpg 会说 manipulated）"
chmod 644 "$WORK/restored.db"

# ---- 验。这一步才是"能恢复"的证据 -----------------------------------------
say "验"
$COMPOSE run --rm --no-deps -T -v "$WORK:/backup" newperson \
    python -m newperson verify /backup/restored.db || die "这份备份不能用"

if [ "$MODE" = "drill" ]; then
    echo
    say "✓ 演练通过。线上那份一个字节都没动。"
    say "  真要装回去：scripts/restore.sh --install --at ${WANT:-<日期>}"
    exit 0
fi

# ---- 真的装回去 -----------------------------------------------------------
echo
echo "要把 $PICK 装成线上的数据库。"
echo "当前那份会留成 $DB_NAME.replaced-$(date -u +%Y%m%dT%H%M%SZ)，不会直接删。"
printf "确认？输 yes："
read -r ANSWER
[ "$ANSWER" = "yes" ] || die "算了"

say "停容器"
$COMPOSE stop newperson || true

if [ -f "$DATA_DIR/$DB_NAME" ]; then
    ASIDE="$DATA_DIR/$DB_NAME.replaced-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$DATA_DIR/$DB_NAME" "$ASIDE"
    say "当前那份挪到了 $(basename "$ASIDE")"
fi
# -wal / -shm 是旧库的旁文件，留着会和新库对不上。
rm -f "$DATA_DIR/$DB_NAME-wal" "$DATA_DIR/$DB_NAME-shm"

cp "$WORK/restored.db" "$DATA_DIR/$DB_NAME"
chown 10001:10001 "$DATA_DIR/$DB_NAME" 2>/dev/null || true

say "起容器"
$COMPOSE start newperson || $COMPOSE up -d

say "✓ 装好了。看一眼日志：docker compose logs -f"
say "  她会以为中间那段时间自己没看手机。"
