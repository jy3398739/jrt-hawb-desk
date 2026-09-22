# -*- coding: utf-8 -*-
"""站点取单进程：从分单审核台站点队列领任务 → 跑本机识别管线 → 回写结果。

同事在网页上传分单后，识别仍只在这台机器上跑（消耗本机魔搭额度、留档在本机 output/）。
本机关机或进程停掉时，任务就留在队列里等下一次开机，不会丢。

启动:
  python site_worker.py                      # 常驻，每 8 秒轮询一次
  python site_worker.py --once               # 只清一遍队列就退出（适合计划任务）
  python site_worker.py --url https://x.site --token xxxx --limit 2
前置: .env 里配 SITE_URL（站点地址）与 WORKER_TOKEN（与站点 Settings 里同名密钥一致）
"""
import argparse, json, os, re, shutil, socket, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

import config
from jobs import handle_file
import store

_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
API = "/functions/v1/app"


class Offline(Exception):
    """站点暂时够不着（网络抖动/网关 5xx）：本轮跳过，别退出。"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def safe_name(filename: str) -> str:
    """落盘名跟票面原件名一致才好对账，但路径分隔与控制字符必须清掉（上传方给什么名都有可能）。
    反斜杠先归一成 / 再取 basename，否则 Linux 上 Windows 风格的 '..\\x.pdf' 会整串留下。"""
    raw = str(filename or "").replace("\x00", "").replace("\\", "/")
    base = os.path.basename(raw)
    out = _BAD_NAME.sub("_", base).strip(" .")[:120] or "upload"
    return out


class Site:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/") + API
        self.token = token

    def call(self, op: str, payload=None, binary: bool = False, timeout: int = 600):
        """payload=None 走 GET；否则 POST JSON。binary=True 时返回原始字节。

        令牌不对 = 配置错了，直接退出；站点暂时不可用 = Offline，等下一轮；
        其余业务错误（如票已被别人领走）把 JSON 原样返回，由调用方打日志。
        """
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url + "?op=" + op, data=data, method="GET" if data is None else "POST")
        req.add_header("X-Worker-Token", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
            except Exception:
                parsed = None
            code = (parsed or {}).get("error", "")
            if e.code in (401, 403) or code in ("worker_not_configured", "bad_worker_token"):
                raise SystemExit(f"接口 {op} 被拒（{e.code} {code or detail}）：令牌与站点 Settings 里的一致吗？") from None
            if e.code == 429 or e.code >= 500:
                raise Offline(f"接口 {op} 暂不可用（{e.code} {code}）") from None
            if parsed is not None:
                return parsed
            raise SystemExit(f"接口 {op} 返回 {e.code} 且不是 JSON：{detail[:120]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise Offline(f"连不上站点：{getattr(e, 'reason', e)}") from None
        if binary:
            return body
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as e:
            raise SystemExit(f"接口 {op} 返回的不是 JSON：{body[:120]!r}") from None


def one_round(site: Site, limit: int, save: bool, worker: str) -> int:
    got = site.call("claim", {"limit": limit, "worker": worker})
    if got.get("error"):
        log(f"领单失败：{got['error']}")
        return 0
    jobs = got.get("jobs") or []
    if not jobs:
        return 0
    held = []
    try:
        for row in jobs:
            jid, name = row["id"], row["filename"]
            held.append(jid)
            tmp = Path(tempfile.mkdtemp(prefix="hawb_site_"))
            try:
                log(f"取单 {name}")
                blob = site.call(f"file&id={jid}", binary=True)
                path = tmp / safe_name(name)
                path.write_bytes(blob)
                r = handle_file(path, save)
                qc = r["qc"] or {}
                back = site.call("result", {
                    "id": jid, "ok": not r["error"], "stem": r["stem"], "channel": r["channel"],
                    "elapsed_ms": int(r["elapsed"] * 1000),
                    "needs_review": bool(qc.get("needs_review")), "flags": qc.get("flags") or [],
                    "fidelity": qc.get("fidelity"), "raw": r["raw"], "air": r["air"],
                    "error": r["error"] or "",
                })
                held.remove(jid)
                if back.get("error"):
                    # 识别已经跑完但站点没收（多半是超时被别的进程领走或表没写权限），别当成功
                    log(f"  回写失败 {back['error']} · {name}（结果只在本机 output/）")
                    continue
                head = "失败 " + (r["error"] or "") if r["error"] else f"完成 {r['elapsed']}s"
                log(f"  {head} · {name}"
                    + (f" · 红旗 {len(qc.get('flags') or [])} 条" if qc.get("flags") else ""))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if save:
            store.rebuild_summary()
    finally:
        if held:
            try:
                site.call("release", {"worker": worker, "ids": held})
                log(f"已交回 {len(held)} 张未完成的票")
            except Offline as e:
                log(f"{e}；未交回的票会在 30 分钟后自动回队列")
    return len(jobs)


def main() -> int:
    config.fix_console()
    ap = argparse.ArgumentParser(description="从分单审核台站点队列取单，跑本机识别管线")
    ap.add_argument("--url", default=config.SITE_URL, help="站点地址，默认取 .env 的 SITE_URL")
    ap.add_argument("--token", default=config.WORKER_TOKEN, help="取单令牌，默认取 .env 的 WORKER_TOKEN")
    ap.add_argument("--limit", type=int, default=2, help="每轮最多领几张（默认 2）")
    ap.add_argument("--interval", type=float, default=8, help="空队列时的轮询秒数（默认 8）")
    ap.add_argument("--once", action="store_true", help="清完当前队列就退出")
    ap.add_argument("--no-save", action="store_true", help="只回写站点，不在本机 output/ 落盘留档")
    args = ap.parse_args()

    if not args.url:
        raise SystemExit("未指定站点地址：在 .env 配 SITE_URL=https://你的站点地址 或用 --url")
    if not args.token:
        raise SystemExit("未配置取单令牌：在 .env 配 WORKER_TOKEN=站点 Settings 里 WORKER_TOKEN 的值")
    config.require_vlm_api_key()

    site = Site(args.url, args.token)
    worker = socket.gethostname()
    save = not args.no_save
    ping = site.call("ping")
    if not ping.get("worker_ready"):
        raise SystemExit("站点侧未配置 WORKER_TOKEN：请到站点 Settings 填入与本机相同的令牌后重试")
    log(f"已连接 {args.url} · 模型 {config.VLM_MODEL} · 本机留档 {'开' if save else '关'}")

    idle = 0
    while True:
        n = 0
        try:
            n = one_round(site, args.limit, save, worker)
            idle = 0
        except Offline as e:
            log(str(e))
            idle += 1
            if args.once or idle >= 5:
                raise SystemExit(f"{e}（连续 {idle} 次失败，退出）") from None
        if n == 0:
            if args.once:
                log("队列已清空")
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("已停止（Ctrl+C）")
