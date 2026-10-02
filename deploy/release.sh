#!/usr/bin/env bash
# 发一个版本：改号 → 跑回归 → 提交并打 tag → 推服务器 → 推 GitHub。
#
#   ./deploy/release.sh 1.1.0            # 发布
#   ./deploy/release.sh 1.1.0 --dry-run  # 只跑检查与改号，不动服务器也不提交
#
# 号怎么定（写死在这里，免得每次都问）：
#   修 bug / 内部重构        → 第三位 +1     （1.0.1）
#   加界面、加字段口径调整    → 第二位 +1     （1.1.0）
#   换形态（V2 那种）         → 第一位 +1，另在 config.FORM 标形态
set -euo pipefail
die() { echo "停：$*" >&2; exit 1; }

VER="${1:-}"; DRY=0; [ "${2:-}" = "--dry-run" ] && DRY=1
if ! printf '%s' "$VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "用法：./deploy/release.sh <x.y.z>（例如 1.1.0）" >&2; exit 2
fi
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "停：工作区有未提交改动。发版要干净：先提交或先 git stash" >&2; exit 1
fi
if git rev-parse -q --verify "refs/tags/v$VER" >/dev/null; then
  echo "停：tag v$VER 已存在，换一个号（别覆盖已发过的版本）" >&2; exit 1
fi

OLD="$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' config.py)"
echo "== 版本 $OLD → $VER"
sed -i "s/^APP_VERSION = \"[^\"]*\"/APP_VERSION = \"$VER\"/" config.py
trap 'if [ "$ROLLBACK" = "1" ]; then sed -i "s/^APP_VERSION = \"[^\"]*\"/APP_VERSION = \"$OLD\"/" config.py; echo "已把版本号退回 $OLD"; fi' EXIT
ROLLBACK=1

echo "== 跑全量回归"
# 跑一遍拿退出码，别为了取退出码再跑一遍（这套件要几分钟，跑两遍等于发版慢一倍）
LOG="/tmp/hawb-release-reg.log"
RC=0; python -X utf8 tests/run_tests.py >"$LOG" 2>&1 || RC=$?
tail -3 "$LOG"
[ "$RC" = "0" ] || { echo "停：回归没过（退出码 $RC，全文在 $LOG），版本号已退回（不发版）" >&2; exit 1; }
ROLLBACK=0

[ "$DRY" = "1" ] && { echo "== --dry-run：改号与检查完成，没提交也没推"; exit 0; }

echo "== 提交并打 tag v$VER"
git add config.py
git commit -q -m "release $VER"
git tag -a "v$VER" -m "版本 $VER（$(date +%F)）"

echo "== 推服务器"
./deploy/sync.sh

echo "== 自证：服务器上跑的确实是 $VER"
# sync.sh 结尾也会核一次，这里是发版口的独立凭据：tag、config、线上 /health 三处必须同一个号，
# 少一处就没法回答"客户现在用的到底是哪版"。
HOST="${HAWB_SSH:-efreight-vm}"
HEALTH="${HAWB_HEALTH_URL:-http://127.0.0.1:8020/health}"
BODY="$(ssh "$HOST" "curl -s --max-time 10 '$HEALTH'")" || die "/health 取不到：发版未完，先 ssh $HOST 'systemctl status hawb-desk'"
case "$BODY" in
  *"\"version\":\"$VER\""*) echo "   线上 $BODY" ;;
  *) die "/health 报的不是 $VER（实际：$BODY）——tag 已打但服务器可能是旧进程，别当作发版成功" ;;
esac

echo "== 推 GitHub（含 tag）"
if git remote get-url origin >/dev/null 2>&1; then
  git push origin master "v$VER" || echo "注意：GitHub 没推上（服务器已发版成功）。网络恢复后手动 git push origin master v$VER"
else
  echo "注意：没配 origin，只发了服务器"
fi

echo "== 完成：v$VER（服务器与 GitHub 均已按上面输出核对）"
