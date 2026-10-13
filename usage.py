# -*- coding: utf-8 -*-
"""模型调用的 token 账：按线程记，一张票一个窗口。

为什么要这一层：额度是本项目最贵的资源，而生产代码里从前没有一处读 `resp.usage`——
"换哪个模型""开思考档多烧多少"只能拿 elapsed 秒数粗算。它是**纯加法可观测性**：
不改任何判据、不改送进模型的字节，账由 `jobs.handle_file` 与 `master_pipeline` 在收尾时取走，
落进质检记录。

窗口是显式的（begin / take）：脚本直调模型时没人开窗，`note()` 就安静跳过——既不会让解析失败，
也不会让一个长跑进程的线程挂着越攒越多的账。
"""
import threading

_local = threading.local()


def _blank():
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "reasoning_tokens": 0}


def begin() -> None:
    _local.buckets = {}


def note(resp, stage: str) -> None:
    """记一次调用进了哪个阶段。账本自己出任何错都不许把一张票带崩，所以整段兜住。"""
    b = getattr(_local, "buckets", None)
    if b is None:
        return
    try:
        d = b.setdefault(stage, _blank())
        d["calls"] += 1
        u = getattr(resp, "usage", None)
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            d[k] += int(getattr(u, k, 0) or 0)
        det = getattr(u, "completion_tokens_details", None)
        d["reasoning_tokens"] += int(getattr(det, "reasoning_tokens", 0) or 0)
    except Exception:
        pass


def take() -> dict:
    """取走并关窗：下一张票从空账开始（批处理与队列 worker 是同一个线程反复跑）。"""
    b = getattr(_local, "buckets", None) or {}
    _local.buckets = None
    return b
