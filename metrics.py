# -*- coding: utf-8 -*-
"""额度与耗时的聚合出口（2026-10-18 效率审查 O1）。

token 账与分段耗时从 v1.20.0 起就落在每张票的 `qc` 里（`usage` / `timings`），但一直
没有聚合出口——"额度花在哪、哪类失败最多、换模型省了多少"只能一张张翻 qc。

这里**只读**：输入是 `store.load_qc()` 那份 stem -> qc 映射，不改任何落盘格式、不引入新的
记账口径，数字全部来自 qc 里已有的那几栏。算术只有这一处——页面、导出、以后任何读数都从
这里出（同 `stats.py` 的分工）。
"""
# 失败分档：qc 里的 `error` 是 `类名: 说明` 那种自由文本（jobs 里拼的），
# 直接拿它当分类键会让每个失败各自成一类。按已知文案收成几档，认不出的退回类名。
#
# 顺序有讲究：**先因后果**。超时排在最前——L1 超时和 L2 超时是同一个模型调用、
# 同一份超时预算，修法也一样（抬 VLM_TIMEOUT），按层拆开反而把同一件事拆成两栏。
# 其余按"钱烧在哪一层"分：哪一层失败多，就去看那一层。
_KINDS = (
    ("超时", ("超时", "Timeout", "timeout")),
    ("L1 转录失败", ("转录失败",)),
    ("L2 提取失败", ("提取失败",)),
    ("模型未配置", ("未配置", "SystemExit", "NotConfigured")),
    ("文件类型不支持", ("不支持的文件类型",)),
)
_STAGES = ("render", "l1", "l2", "l3")


def kind_of(err: str) -> str:
    """把一条失败说明收成一档。空串不是失败，原样返回空串。"""
    s = str(err or "").strip()
    if not s:
        return ""
    for name, keys in _KINDS:
        if any(k in s for k in keys):
            return name
    head = s.split(":", 1)[0].strip()
    return (head or s)[:40]


def _blank() -> dict:
    return {"tickets": 0, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "reasoning_tokens": 0, "failed": 0, "flagged": 0,
            "seconds": {s: 0.0 for s in _STAGES}}


def _add(bucket: dict, qc: dict) -> None:
    bucket["tickets"] += 1
    if str(qc.get("error") or "").strip():
        bucket["failed"] += 1
    if qc.get("needs_review"):
        bucket["flagged"] += 1
    for stage, u in (qc.get("usage") or {}).items():
        if not isinstance(u, dict):
            continue
        bucket["calls"] += int(u.get("calls") or 0)
        for k in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
            bucket[k] = bucket.get(k, 0) + int(u.get(k) or 0)
    for stage, sec in (qc.get("timings") or {}).items():
        if isinstance(sec, (int, float)):
            bucket["seconds"][stage] = round(bucket["seconds"].get(stage, 0.0) + float(sec), 2)


def _finish(bucket: dict) -> dict:
    bucket["seconds"] = {k: v for k, v in bucket["seconds"].items() if v}
    return bucket


def collect(qcs: dict, date_from: str = "", date_to: str = "") -> dict:
    """qcs = store.load_qc()。日期窗按 qc 的 `processed_at` 前 10 位比，含端点；
    没写日期的票只在没给窗口时进统计（给了窗口就无从判断它在不在窗口里）。"""
    totals = _blank()
    by_day, by_model, by_stage, failures = {}, {}, {}, {}
    for qc in (qcs or {}).values():
        if not isinstance(qc, dict):
            continue
        day = str(qc.get("processed_at") or "")[:10]
        if date_from or date_to:
            if not day or (date_from and day < date_from) or (date_to and day > date_to):
                continue
        _add(totals, qc)
        _add(by_day.setdefault(day or "（没有解析时刻）", _blank()), qc)
        model = str(qc.get("model") or "") or "（没记模型）"
        key = f"{model}｜{qc.get('model_choice') or '-'}"
        _add(by_model.setdefault(key, _blank()), qc)
        for stage, u in (qc.get("usage") or {}).items():
            if not isinstance(u, dict):
                continue
            b = by_stage.setdefault(stage, {"stage": stage, "calls": 0, "prompt_tokens": 0,
                                            "completion_tokens": 0, "total_tokens": 0,
                                            "reasoning_tokens": 0})
            b["calls"] += int(u.get("calls") or 0)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
                b[k] += int(u.get(k) or 0)
        kind = kind_of(qc.get("error"))
        if kind:
            failures[kind] = failures.get(kind, 0) + 1
    return {
        "window": {"from": date_from, "to": date_to},
        "totals": _finish(totals),
        "by_day": [dict(day=k, **_finish(v)) for k, v in sorted(by_day.items())],
        "by_model": [dict(model=k, **_finish(v)) for k, v in sorted(by_model.items())],
        "by_stage": [by_stage[k] for k in sorted(by_stage)],
        "failures": [{"kind": k, "count": v}
                     for k, v in sorted(failures.items(), key=lambda kv: (-kv[1], kv[0]))],
    }
