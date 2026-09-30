# -*- coding: utf-8 -*-
"""上传解析的进程内队列：限并发 + 排队可见。

为什么不引外部任务队列：结果本来就落 output/*.json，进程要新增的状态只有"排队中/解析中"。
重启丢的是队列本身，票与已完成的解析都在磁盘上——前端拿 404 会自动重传一次并说明。
限并发是这里存在的理由：一张票的最坏路径是 LibreOffice 子进程(~300MB) + 全页渲染 + 两次
模型调用，没有闸门时"同时传了几张"就直接决定内存峰值，而撞满 MemoryMax 杀的是整个服务。
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
KEEP_SECONDS = 1800          # 完成的作业留 30 分钟供前端回看，之后就地清理


class QueueFull(RuntimeError):
    """排队的人都快把队列塞满了——后端已经吃不消，这时候该让人稍后再传而不是继续接。"""


class DeskQueue:
    def __init__(self, slots: int = 2, max_pending: int = 200):
        self._pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="hawb")
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._waiting: list[str] = []
        self.slots, self.max_pending = slots, max_pending

    def submit(self, fn, who: str = "", name: str = "") -> str:
        with self._lock:
            self._prune(time.time())
            live = [j for j in self._jobs.values() if j["state"] in (QUEUED, RUNNING)]
            if len(live) >= self.max_pending:
                raise QueueFull(f"排队中已有 {len(live)} 张，请等一会儿再传")
            jid = uuid.uuid4().hex[:12]
            self._jobs[jid] = {"id": jid, "state": QUEUED, "who": who, "name": name,
                               "result": None, "error": "", "at": time.time()}
            self._waiting.append(jid)
        self._pool.submit(self._run, jid, fn)
        return jid

    def _run(self, jid: str, fn):
        with self._lock:
            if jid in self._waiting:
                self._waiting.remove(jid)
            self._jobs[jid]["state"] = RUNNING
        try:
            result = fn()
            with self._lock:
                self._jobs[jid].update(state=DONE, result=result, at=time.time())
        except Exception as e:                      # 解析失败是任务结果，不该把 worker 线程带走
            with self._lock:
                self._jobs[jid].update(state=FAILED, error=f"{type(e).__name__}: {e}", at=time.time())

    def get(self, jid: str):
        """返回作业快照；queued 时带 position（前面还有几张）。"""
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return None
            out = dict(job)
            if out["state"] == QUEUED and jid in self._waiting:
                out["position"] = self._waiting.index(jid) + 1
            else:
                out["position"] = 0
            out["waiting"], out["running"] = len(self._waiting), sum(
                1 for j in self._jobs.values() if j["state"] == RUNNING)
            return out

    def stats(self) -> dict:
        with self._lock:
            return {"waiting": len(self._waiting),
                    "running": sum(1 for j in self._jobs.values() if j["state"] == RUNNING),
                    "slots": self.slots}

    def _prune(self, now: float):
        """调用方已持锁：清掉超时的已完结作业，否则跑一天内存只涨不落。"""
        for jid in [k for k, j in self._jobs.items()
                    if j["state"] in (DONE, FAILED) and now - j["at"] > KEEP_SECONDS]:
            self._jobs.pop(jid, None)
