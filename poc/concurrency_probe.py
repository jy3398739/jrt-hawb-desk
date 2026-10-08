# -*- coding: utf-8 -*-
"""并发容量实测：几个制单员同时在岗，这套系统扛得住多少。

三个瓶颈分开压（合在一起只会看到最先断的那一根）：
  A 读接口与票面渲染 —— 制单员一天里点的绝大多数是这个；
  B 解析排队 —— DESK_CONCURRENCY 个槽位，一张票的最坏路径（LibreOffice + 全页渲染 + 两次模型调用）；
  C 提交回公司 —— J9 限流与 90 秒幂等锁。

不烧钱也不外发：模型调用换成**固定延迟的假件**（延迟取 2026-10-08 实测值：官方 S2 约 8 秒/张、
公司 qwen 约 10 秒/张），公司系统是 mock 模式，写入目录全部指到临时目录，
读的一侧只读真实的 output/（渲染与台账本来就不写盘）。

用法（在仓库根目录）：
  python poc/concurrency_probe.py                # 默认三档并发
  python poc/concurrency_probe.py --levels 1,8,32 --tickets 60 --parse-seconds 8
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2 as httpx        # 锁里装的是 httpx2；starlette 的 TestClient 也是这么别名的

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import config 之前钉：这些键决定它去哪读配置、往哪写结果
_tmp = Path(tempfile.mkdtemp(prefix="hawb_probe_"))
os.environ["AUTH_USERS_FILE"] = str(_tmp / "users.json")
os.environ["SUBMIT_GUARD_DIR"] = str(_tmp / "guard")
for k in ("ARCHIVE_DIR", "TRANSCRIPT_DIR", "OUTPUT_RAW_DIR", "OUTPUT_AIR_DIR",
          "OUTPUT_QC_DIR", "PREVIEW_DIR"):
    os.environ[k] = str(_tmp / k.lower())          # 写入一律进临时目录，绝不碰真 output/
os.environ["COMPANY_API_MODE"] = "mock"
os.environ["COMPANY_API_URL"] = ""
os.environ["COMPANY_MAWB_KEY"] = ""
os.environ["COMPANY_HAWB_KEY"] = ""
os.environ["MASTER_VLM_MODEL"] = ""
os.environ["VLM_MODEL"] = "intern-s2-official"

import auth            # noqa: E402
import config          # noqa: E402
import desk_queue      # noqa: E402
import server          # noqa: E402

OPS = ["马殿齐", "宛平", "陈新", "叶庭伸", "赵文宇", "杨皓荃", "隗一航", "董文志"]
PW = "probe-pw-123"


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1))))]


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) / 1024.0
    except OSError:
        pass
    return -1.0


class Probe:
    """假模型 + 真服务，跑在一个独立端口上（不碰线上 8020 那个进程）。"""

    def __init__(self, port, parse_seconds):
        self.port, self.parse_seconds = port, parse_seconds
        self.srv = None
        self.stats = defaultdict(lambda: {"n": 0, "ms": [], "err": 0})
        self._lock = threading.Lock()

    # ── 假模型：只替掉解析那一段，其余（鉴权/渲染/落盘/队列）全是真代码 ─────────
    def fake_handle_file(self, path, save=True):
        t0 = time.time()
        time.sleep(self.parse_seconds)
        stem = Path(path).stem
        air = {"MAWB_NO": "235-96146363", "HAWB_NO": stem[:12], "DEST_NAME": "LAX"}
        return {"stem": stem, "channel": "vlm", "elapsed": round(time.time() - t0, 2),
                "raw": {"MAWB NO.": "235-96146363"}, "air": air,
                "transcript": {"lines": [{"i": 1, "text": "MAWB NO. 235-96146363",
                                          "bbox": [0, 0, 100, 10]}],
                               "full_text": "MAWB NO. 235-96146363"},
                "warns": [], "fidelity": {"passed": 1, "checked": 1},
                "archive": None, "qc": {"needs_review": False, "flags": [], "fidelity": None,
                                        "error": "", "md5": "probe"}, "error": ""}

    def timed(self, name, fn):
        t0 = time.perf_counter()
        try:
            r = fn()
            ok = r.status_code < 400
            code = r.status_code
        except Exception as e:                                   # 连接被掐/超时也算这一档的失败
            ok, code = False, type(e).__name__
            r = None
        ms = (time.perf_counter() - t0) * 1000
        with self._lock:
            s = self.stats[name]
            s["n"] += 1
            s["ms"].append(ms)
            if not ok:
                s["err"] += 1
                s.setdefault("codes", []).append(str(code))
        return ok, r

    def start(self):
        import uvicorn
        auth.USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        auth.ensure_seed()
        for n in OPS:
            auth.set_password(n, PW, "reviewer")
        server.handle_file = self.fake_handle_file
        cfg = uvicorn.Config(server.app, host="127.0.0.1", port=self.port,
                             log_level="error", loop="asyncio")
        self.srv = uvicorn.Server(cfg)
        threading.Thread(target=self.srv.run, daemon=True).start()
        for _ in range(120):
            if self.srv.started:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("探针服务没起来")

    def stop(self):
        if self.srv:
            self.srv.should_exit = True
            time.sleep(1.0)

    def login(self, name):
        s = httpx.Client(timeout=120)
        r = s.post(f"http://127.0.0.1:{self.port}/login", json={"name": name, "password": PW})
        assert r.status_code == 200, r.text
        return s


# ── A：读接口与票面渲染 ────────────────────────────────────────────────────
def phase_a(p, levels, rounds):
    """一个"制单员一次核对"= 列表 → 打开票 → 看票面首页 → 翻台账。"""
    real = config.ARCHIVE_DIR
    try:
        # 读的一侧指回真实归档：/render 与 /day 本来就只读
        config.ARCHIVE_DIR = Path(os.environ.get("PROBE_REAL_ARCHIVE") or (ROOT / "output/archive"))
        config.TRANSCRIPT_DIR = Path(os.environ.get("PROBE_REAL_TRANSCRIPT") or (ROOT / "output/transcript"))
        config.OUTPUT_RAW_DIR = Path(os.environ.get("PROBE_REAL_RAW") or (ROOT / "output/raw"))
        config.OUTPUT_AIR_DIR = Path(os.environ.get("PROBE_REAL_AIR") or (ROOT / "output/air"))
        config.OUTPUT_QC_DIR = Path(os.environ.get("PROBE_REAL_QC") or (ROOT / "output/qc"))
        stems = sorted(x.name for x in config.ARCHIVE_DIR.iterdir() if x.is_dir())[:12]
        if not stems:
            print("   （归档目录是空的，A 段跳过）")
            return
        print(f"   归档里可渲染的票：{len(stems)} 张")
        for n in levels:
            p.stats.clear()
            sessions = [p.login(OPS[i % len(OPS)]) for i in range(n)]
            t0 = time.perf_counter()

            def one(sess, i):
                stem = stems[i % len(stems)]
                B = f"http://127.0.0.1:{p.port}"
                p.timed("GET /api/me", lambda: sess.get(B + "/api/me"))
                p.timed("GET /results", lambda: sess.get(B + "/results"))
                p.timed("GET /ticket/{stem}", lambda: sess.get(B + f"/ticket/{stem}"))
                p.timed("GET /render/{stem}", lambda: sess.get(B + f"/render/{stem}?page=1&scale=2"))
                p.timed("GET /day", lambda: sess.get(B + "/day"))

            with ThreadPoolExecutor(max_workers=n) as ex:
                list(ex.map(lambda i: one(sessions[i % len(sessions)], i), range(n * rounds)))
            wall = time.perf_counter() - t0
            print(f"   {n:>3} 人 × {rounds} 轮：{wall:5.1f}s 墙钟 · "
                  f"{sum(s['n'] for s in p.stats.values()) / wall:6.1f} 请求/秒 · "
                  f"失败 {sum(s['err'] for s in p.stats.values())}")
            for name in sorted(p.stats):
                s = p.stats[name]
                print(f"        {name:22} n={s['n']:4} p50={pct(s['ms'],50):7.1f}ms "
                      f"p90={pct(s['ms'],90):7.1f}ms max={max(s['ms']):8.1f}ms 失败 {s['err']}"
                      + (f"  ← {set(s['codes'])}" if s["err"] else ""))
    finally:
        config.ARCHIVE_DIR = real


# ── B：解析排队 ────────────────────────────────────────────────────────────
def phase_b(p, burst, slots):
    """一次传 burst 张，看最后一张等了多久、位次报得对不对、有没有丢作业。"""
    server.DESK_QUEUE = desk_queue.DeskQueue(slots=slots, max_pending=config.DESK_QUEUE_MAX)
    sess = p.login(OPS[0])
    B = f"http://127.0.0.1:{p.port}"
    jobs, pos_seen = {}, {}

    def submit(i):
        r = sess.post(B + "/extract", params={"save": False},
                      files={"file": (f"probe{i}.pdf", b"%PDF-1.4 probe", "application/pdf")})
        assert r.status_code == 202, r.text
        jobs[r.json()["job"]] = time.time()
        pos_seen.setdefault(r.json()["job"], r.json()["position"])

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(burst, 16)) as ex:
        list(ex.map(submit, range(burst)))
    inflight = set(jobs)
    worst_pos = 0
    while inflight:
        for j in list(inflight):
            r = sess.get(B + f"/job/{j}")
            if r.status_code == 404:
                raise RuntimeError(f"作业 {j} 404 了——队列把任务丢了")
            d = r.json()
            worst_pos = max(worst_pos, d.get("position", 0) + d.get("running", 0))
            if d["state"] in ("done", "failed"):
                inflight.discard(j)
        time.sleep(0.2)
    wall = time.time() - t0
    per = wall / max(1, burst) * slots
    print(f"   槽位={slots} · 假模型 {p.parse_seconds}s/张 · 一次传 {burst} 张：")
    print(f"        全部完成用时 {wall:.1f}s（吞吐 {burst/wall*60:.1f} 张/分钟）")
    print(f"        平均每张 {per:.1f}s；最后一张排队约 {max(0.0, wall - p.parse_seconds):.1f}s")
    print(f"        位次峰值 {worst_pos}（一次传 {burst} 张，最后一张应看到接近 {burst - 1}："
          f"前面排队的 + 槽位里在跑的）")


# ── C：提交回公司 ──────────────────────────────────────────────────────────
def phase_c(p, n):
    """先摸一次门禁（拿到未清红旗清单，按它 ack），再让 n 个人同时点提交。
    mock 模式下不发公司请求，测的是这一段真开销：门禁重算 + 台账写盘 + 幂等锁。"""
    sess = p.login(OPS[0])
    B = f"http://127.0.0.1:{p.port}"

    def payload(i, acked=()):
        return {"tickets": [{"stem": f"probe{i}", "filename": f"probe{i}.pdf",
                             "air_reviewed": {"MAWB_NO": "235-96146363", "HAWB_NO": f"PROBE{i:04d}",
                                              "ORIGIN_NAME": "Beijing", "DEST_NAME": "LAX",
                                              "PIECES": "1", "WEIGHT": "1500"},
                             "acked_flags": list(acked)}]}

    probe = sess.post(B + "/submit", json=payload(0))
    acked = []
    if probe.status_code == 400 and isinstance(probe.json().get("detail"), dict):
        acked = probe.json()["detail"].get("flags") or []
    print(f"   门禁先摸一次：HTTP {probe.status_code}，未清红旗 {len(acked)} 条（提交时逐条 ack 掉，"
          f"才能测到真提交那一段而不是被门挡回）")
    out = []

    def go(i):
        ok, r = p.timed("POST /submit", lambda: sess.post(B + "/submit", json=payload(i, acked)))
        out.append((r.status_code if r is not None else 0,
                    (r.json() if r is not None and r.content else {})))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(go, range(n)))
    wall = time.perf_counter() - t0
    s = p.stats["POST /submit"]
    print(f"   {n} 个人同时点提交：墙钟 {wall:.2f}s · "
          f"p50={pct(s['ms'],50):.0f}ms p90={pct(s['ms'],90):.0f}ms max={max(s['ms']):.0f}ms · 失败 {s['err']}")
    codes = {}
    for c, b in out:
        codes[(c, str(b.get("ok")))] = codes.get((c, str(b.get("ok"))), 0) + 1
    print(f"        返回分布：{codes}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,8,32")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--tickets", default="24,60")
    ap.add_argument("--parse-seconds", type=float, default=8.0)
    ap.add_argument("--slots", type=int, default=0, help="0=用 .env 里的 DESK_CONCURRENCY")
    ap.add_argument("--submit", type=int, default=8)
    ap.add_argument("--port", type=int, default=8199)
    a = ap.parse_args()
    slots = a.slots or config.DESK_CONCURRENCY
    print(f"== 环境：{os.cpu_count()} 核 · 解析槽位 DESK_CONCURRENCY={slots} · "
          f"LibreOffice 槽位 XLSX_PDF_SLOTS={config.XLSX_PDF_SLOTS} · "
          f"J9 限流 {config.J9_RATE_PER_SEC}/秒 · 内存闸 {rss_mb():.0f}MB 当前")
    p = Probe(a.port, a.parse_seconds)
    p.start()
    try:
        print("\n== A 读接口与票面渲染（制单员日常点得最多的）")
        phase_a(p, [int(x) for x in a.levels.split(",")], a.rounds)
        print("\n== B 解析排队（一次传一批，看最后一张等多久）")
        for t in [int(x) for x in a.tickets.split(",")]:
            phase_b(p, t, slots)
        print("\n== C 同时点提交")
        phase_c(p, a.submit)
        print(f"\n== 探针进程峰值内存：{rss_mb():.0f}MB（含被测服务本体）")
    finally:
        p.stop()


if __name__ == "__main__":
    main()
