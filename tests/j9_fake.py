# -*- coding: utf-8 -*-
"""j9 假 HTTP 桩的共用判据。

四个用例文件各自抄过一份 `_j9_post` 桩，2026-10-09 提交口从 `2` 挪到 `3` 时一次炸八条——
"哪条路径算写口"这种事实也只许有一处。写口分两类：`2`=暂存（落状态 0）、`3`=提交发送（落状态 1），
状态一律由端点决定（我们传什么都会被服务端忽略）。
"""

STAGE = ("/hawb2", "/mawb2/")
SEND = ("/hawb3", "/mawb3/")


def is_write(path):
    return path.endswith(STAGE + SEND)


def is_send(path):
    return path.endswith(SEND)


def is_stage(path):
    return path.endswith(STAGE)


def writes(sent):
    """挑出记录下来的写请求（读请求不算）。"""
    return [s for s in sent if is_write(s["path"])]


def write_ok(path, action="update"):
    return {"code": 0, "success": True, "action": action, "rows": 1,
            "SEND_STATUS": 1 if is_send(path) else 0}
