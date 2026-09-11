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
#   scripts/restore.sh --install          真的装回去（会停服务、会留下当前这份）
#
# 定期演练：0 5 * * 0 /opt/New_person/scripts/restore.sh >> /var/log/chloe-drill.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NP_DIR="$(dirname "$SCRIPT_DIR")"
cd "$NP_DIR"

# shellcheck source=/dev/null
[ -f "$SCRIPT_DIR/backup.env" ] && . "$SCRIPT_DIR/backup.env"

RCLONE_REMOTE="${RCLONE_REMOTE:-}"
GPG_PASSPHRASE_FILE="${GPG_PASSPHRASE_FILE:-/root/.chloe-backup-pass}"
PYTHON_BIN="${PYTHON_BIN:-$NP_DIR/.venv/bin/python}"
DB_PATH="${DB_PATH:-$NP_DIR/data/newperson.db}"
SERVICE_NAME="${SERVICE_NAME:-chloe}"
SERVICE_STOP="${SERVICE_STOP:-systemctl stop $SERVICE_NAME}"
SERVICE_START="${SERVICE_START:-systemctl start $SERVICE_NAME}"

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
command -v rclone >/dev/null || die "找不到 rclone"
# shellcheck disable=SC2086
$PYTHON_BIN -c "import newperson.config" >/dev/null 2>&1 \
    || die "$PYTHON_BIN 跑不了 newperson（包或依赖缺失）。虚拟环境对吗？（cd $NP_DIR && .venv/bin/pip install -e .）"

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

# ---- 验。这一步才是"能恢复"的证据 -----------------------------------------
# 跑 integrity_check，再把里面有什么数出来。一个 0 条消息的完好数据库同样完好，
# 但那不是她——所以 verify 数不出消息时也退非零。
say "验"
# shellcheck disable=SC2086
$PYTHON_BIN -m newperson verify "$WORK/restored.db" || die "这份备份不能用"

if [ "$MODE" = "drill" ]; then
    echo
    say "✓ 演练通过。线上那份一个字节都没动。"
    say "  真要装回去：scripts/restore.sh --install --at ${WANT:-<日期>}"
    exit 0
fi

# ---- 真的装回去 -----------------------------------------------------------
echo
echo "要把 $PICK 装成线上的数据库（$DB_PATH）。"
echo "当前那份会改名留着，不会直接删。"
printf "确认？输 yes："
read -r ANSWER
[ "$ANSWER" = "yes" ] || die "算了"

# 停不下来就**不能继续**。原来这里是失败也往下走，后果很重：
# 服务还开着数据库在写，我们把它的文件改名、再 rm 掉 -wal，
# 她会继续往一个已经没有名字的 WAL 里写，那些话在下次重启时凭空消失，
# 然后她打开的是那份旧的恢复库。宁可什么都不做。
say "停服务"
$SERVICE_STOP || die "停不下来 $SERVICE_NAME。她还开着数据库，这时候换文件会丢数据。
   先手动停：systemctl stop $SERVICE_NAME
   或者在 backup.env 里把 SERVICE_STOP/SERVICE_START 改成你这台机器上对的命令"

# 从这里开始，任何一条失败路径都不能把她留在停着的状态。
# shellcheck disable=SC2064
trap "rm -rf '$WORK'; $SERVICE_START || true" EXIT

DATA_DIR="$(dirname "$DB_PATH")"
NEW="$DB_PATH.incoming-$$"
# 先拷到同一个文件系统上，成功之后再原子改名。直接往 $DB_PATH 上覆盖的话，
# 拷到一半断电就两份都没了。
cp "$WORK/restored.db" "$NEW" || die "拷不过去（$DATA_DIR 满了？）"

# 服务用哪个用户跑，恢复出来的文件就得归谁。root 跑的话不用管。
OWNER="$(systemctl show -p User --value "$SERVICE_NAME" 2>/dev/null || true)"
if [ -n "$OWNER" ] && [ "$OWNER" != "root" ]; then
    chown "$OWNER" "$NEW" 2>/dev/null || say "!! chown $OWNER 失败，起不来的话手动改一下属主"
fi

if [ -f "$DB_PATH" ]; then
    ASIDE="$DB_PATH.replaced-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$DB_PATH" "$ASIDE"
    # **-wal 要跟着旧库一起挪走，不能删。** WAL 模式下刚说过的话还躺在
    # -wal 里没写进主库；直接 rm 掉的话，这份"留着以防万一"的副本
    # 恰好缺了她最后几句话——而你会在最需要它的那天才发现。
    for side in wal shm; do
        if [ -f "$DB_PATH-$side" ]; then
            mv "$DB_PATH-$side" "$ASIDE-$side"
        fi
    done
    say "当前那份挪到了 $(basename "$ASIDE")（连 -wal 一起）"
fi
mv "$NEW" "$DB_PATH"
# 兜一下：上面没进 if 分支（$DB_PATH 本来就不在）时，旁文件可能还留着，
# 那些是旧库的，和新库对不上。
rm -f "$DB_PATH-wal" "$DB_PATH-shm"

say "起服务"
trap 'rm -rf "$WORK"' EXIT   # 下面自己起，不用兜底的那次了
$SERVICE_START || die "起不来了。看 journalctl -u $SERVICE_NAME -n 50"

say "✓ 装好了。看一眼日志：journalctl -u $SERVICE_NAME -f"
say "  她会以为中间那段时间自己没看手机。"
