#!/usr/bin/env bash
# 把当前提交里的源码推到 efreight-vm，并自证"服务器上跑的确实是这一版"。
#
#   ./deploy/sync.sh              # 推 HEAD
#   ./deploy/sync.sh --allow-dirty  # 明知工作区脏还是要推（应急）
#   ./deploy/sync.sh --no-restart   # 只推前端（css/js 按请求现读），不重启、不踢在线会话
#   ./deploy/sync.sh --dry-run      # 只报差异，不动服务器
#
# 为什么要有这个脚本：从前每次部署都是手敲 tar 管道，出过两类事故——
#   ① 漏推文件（P1-① 推了没推 ② 的兄弟，VM 回归少 6 项才发现）；
#   ② 新旧混跑（tar 中断，一半新一半旧，而 uvicorn 只在启动时读一次代码）。
# 这里把"备份 → 推 → 逐文件哈希校验 → 跑回归 → 重启 → /health 自证"钉成一条链，
# 任何一步不对就停下并说出停在哪。清单取 git ls-files：.env / users.json / output/ 天然不在里面。
set -euo pipefail

HOST="${HAWB_SSH:-efreight-vm}"
REMOTE_DIR="${HAWB_REMOTE:-~/jrt-hawb/release/hawb_extractor}"
UNIT="${HAWB_UNIT:-hawb-desk}"
SERVICE_URL="${HAWB_HEALTH_URL:-http://127.0.0.1:8020/health}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DRY=0
DIRTY_OK=0
NO_RESTART=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --allow-dirty) DIRTY_OK=1 ;;
    --no-restart) NO_RESTART=1 ;;
    *) echo "未知参数：$a" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf '停：%s\n' "$*" >&2; exit 1; }

git rev-parse --git-dir >/dev/null 2>&1 || die "这里不是 git 仓库，拿不到可信的文件清单"
if [ -n "$(git status --porcelain)" ] && [ "$DRY" = "0" ] && [ "$DIRTY_OK" = "0" ]; then
  die "工作区有未提交改动（部署的是工作区的文件，不是提交）。先提交再部署，应急可加 --allow-dirty"
fi

LIST="$(mktemp)"; LOCAL="$(mktemp)"; REMOTE="$(mktemp)"
trap 'rm -f "$LIST" "$LOCAL" "$REMOTE"' EXIT

git -c core.quotePath=false ls-files > "$LIST"
[ -s "$LIST" ] || die "git ls-files 是空的，拒绝部署"
MISSING="$(while read -r f; do [ -f "$f" ] || echo "$f"; done < "$LIST" | head -5)"
[ -z "$MISSING" ] || die "清单里有文件不在磁盘上：$MISSING"

say "== 待推 $(wc -l < "$LIST") 个文件（$(git log -1 --format='%h %s' | cut -c1-60)）"
xargs -a "$LIST" md5sum | sed 's/^\([0-9a-f]*\)[ *][ *]*/\1 /' | LC_ALL=C sort > "$LOCAL"
ssh "$HOST" "cat > /tmp/hawb-deploy-list" < "$LIST"
ssh "$HOST" "cd $REMOTE_DIR && xargs -a /tmp/hawb-deploy-list md5sum 2>/dev/null | sed 's/^\([0-9a-f]*\)[ *][ *]*/\1 /' | LC_ALL=C sort" > "$REMOTE" || true
# 服务器上有哪几个文件和本机不一样（新文件也算）：--no-restart 要靠它判断有没有动服务端代码
CHANGED="$(awk 'NR==FNR{h[$2]=$1; next} { if (!($2 in h) || h[$2] != $1) print $2 }' "$REMOTE" "$LOCAL" | LC_ALL=C sort)"

if [ "$DRY" = "1" ]; then
  say "== 差异 $(printf '%s\n' "$CHANGED" | grep -c . || true) 个文件："
  printf '%s\n' "$CHANGED" | head -20
  exit 0
fi

if [ "$NO_RESTART" = "1" ]; then
  NEEDS="$(printf '%s\n' "$CHANGED" | grep '\.py$' | grep -v '^tests/' || true)"
  [ -z "$NEEDS" ] || die "--no-restart 只给纯前端改动用：这次要推的里有服务端 .py（$(printf '%s\n' "$NEEDS" | head -3 | tr '\n' ' ')），改了代码必须重启才生效"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BK=".bak-$STAMP-deploy.tar"
ssh "$HOST" "cd $REMOTE_DIR && tar cf ~/$BK --ignore-failed-read -T /tmp/hawb-deploy-list 2>/dev/null; test -f ~/$BK" \
  || die "远端备份失败（$BK），没有动任何文件"   # tar 退出码非 0 视为不可用：宁可不推
say "== 服务器已备份到 ~/$BK"

tar cf - -T "$LIST" | ssh "$HOST" "cd $REMOTE_DIR && tar xf -" || die "推送中断，服务器仍是备份里的旧版"

ssh "$HOST" "cd $REMOTE_DIR && xargs -a /tmp/hawb-deploy-list md5sum | sed 's/^\([0-9a-f]*\)[ *][ *]*/\1 /' | LC_ALL=C sort" > "$REMOTE" \
  || die "远端复算哈希失败"
if ! diff -q "$LOCAL" "$REMOTE" >/dev/null; then
  diff "$LOCAL" "$REMOTE" | head -10
  die "推完仍有文件对不上（上面前 10 行）。回滚：ssh $HOST 'cd $REMOTE_DIR && tar xf ~/$BK'"
fi
say "== 全部文件与本机逐字节一致"

ssh "$HOST" "cd $REMOTE_DIR && .venv/bin/python -X utf8 tests/run_tests.py 2>&1 | tail -3" \
  || die "服务器回归没过（代码没重启，仍是旧版在服务里跑）"

if [ "$NO_RESTART" = "1" ]; then
  BODY="$(ssh "$HOST" "curl -s --max-time 10 '$SERVICE_URL'")"
  case "$BODY" in
    *'"stale_files":[]'*) say "== 没重启：css/js 每次请求现读，在线会话不受影响（/health 仍新鲜）" ;;
    *"commit"*) say "== 服务在线，但 /health 报 stale_files 非空（页面骨架 index.html 也归它管）：不介意就多刷一次页面，介意就正常重启一次" ;;
    *) die "服务没在跑或 /health 无响应——文件已推，先别撤：ssh $HOST 'systemctl status $UNIT'，回滚包 ~/$BK" ;;
  esac
  say "== 部署完成（回滚包 ~/$BK）"
  exit 0
fi

ssh "$HOST" "sudo systemctl restart $UNIT && sleep 4 && systemctl is-active $UNIT" | grep -q '^active$' \
  || die "服务没能起来，立刻回滚：ssh $HOST 'cd $REMOTE_DIR && tar xf ~/$BK && sudo systemctl restart $UNIT'"

BODY="$(ssh "$HOST" "curl -s --max-time 10 '$SERVICE_URL'")"
say "== /health $BODY"
case "$BODY" in
  *'"stale_files":[]'*) ;;
  *) die "服务仍在跑旧代码或文件不齐（stale_files 非空）。回滚：ssh $HOST 'tar xf ~/$BK -C $REMOTE_DIR && sudo systemctl restart $UNIT'" ;;
esac
say "== 部署完成（回滚包 ~/$BK）"
