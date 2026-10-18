# -*- coding: utf-8 -*-
"""额度与耗时的聚合出口（2026-10-18 效率审查 O1）。

`usage` 与 `timings` 从 v1.20.0 起就落在每张票的 qc 里，但一直没有聚合出口——
"额度花在哪、哪类失败最多、换模型省了多少"只能一张张翻 qc。这里盯三件事：
数字加起来对不对、失败分档认得出来、以及这个口子只给管理员。
"""
import re
from pathlib import Path

import metrics
import server


def _qc(**kw):
    base = {"source_name": "a.pdf", "channel": "vlm", "elapsed": 12.0,
            "model": "qwen3.8-flash", "model_choice": "qwen38-flash-bailian",
            "usage": {}, "timings": {}, "processed_at": "2026-10-18T09:00:00",
            "needs_review": False, "flags": [], "error": ""}
    base.update(kw)
    return base


def _use(l1=0, l2=0, l1t=0, l2t=0, reason=0):
    """按 usage.py 真实落的形状造一份账：{阶段: {calls, prompt/completion/total/reasoning}}。"""
    out = {}
    if l1:
        out["L1"] = {"calls": 1, "prompt_tokens": l1, "completion_tokens": 0,
                     "total_tokens": l1, "reasoning_tokens": 0}
    if l2:
        out["L2"] = {"calls": 1, "prompt_tokens": l2 - l2t, "completion_tokens": l2t,
                     "total_tokens": l2, "reasoning_tokens": reason}
    return out


def test_it_sums_tokens_by_day_model_and_stage():
    qcs = {
        "T1": _qc(usage=_use(l1=100, l2=500, l2t=200, reason=50),
                  timings={"render": 0.5, "l1": 3.0, "l2": 8.0, "l3": 0.1}),
        "T2": _qc(usage=_use(l1=120, l2=600, l2t=250),
                  timings={"render": 0.4, "l1": 3.5, "l2": 9.0, "l3": 0.2}),
    }
    out = metrics.collect(qcs)
    assert out["totals"]["tickets"] == 2
    assert out["totals"]["calls"] == 4, out["totals"]
    assert out["totals"]["total_tokens"] == 100 + 500 + 120 + 600, out["totals"]
    assert out["totals"]["reasoning_tokens"] == 50, "推理 token 要单独能看——开思考档就贵在这儿"
    assert out["totals"]["seconds"]["l1"] == 6.5 and out["totals"]["seconds"]["l2"] == 17.0, \
        out["totals"]["seconds"]

    day = out["by_day"][0]
    assert day["day"] == "2026-10-18" and day["tickets"] == 2, out["by_day"]
    model = out["by_model"][0]
    assert model["model"] == "qwen3.8-flash｜qwen38-flash-bailian", model
    assert model["total_tokens"] == out["totals"]["total_tokens"], model

    stages = {s["stage"]: s for s in out["by_stage"]}
    assert set(stages) == {"L1", "L2"}, stages
    assert stages["L1"]["calls"] == 2 and stages["L1"]["total_tokens"] == 220, stages["L1"]
    assert stages["L2"]["total_tokens"] == 1100, stages["L2"]


def test_two_models_are_kept_apart():
    """按模型分账正是"换模型省了多少"的答案：两条链混一栏就答不出来。"""
    qcs = {
        "T1": _qc(usage=_use(l2=500)),
        "T2": _qc(model="intern-s2-official", model_choice="s2", usage=_use(l2=100)),
    }
    out = metrics.collect(qcs)
    got = {m["model"]: m["total_tokens"] for m in out["by_model"]}
    assert got == {"qwen3.8-flash｜qwen38-flash-bailian": 500,
                   "intern-s2-official｜s2": 100}, got


def test_failures_are_grouped_by_kind():
    """失败的分类是"哪类失败最多"的答案。qc 里的 error 是自由文本，
    直接拿它当键会让每个失败各自成一类。

    超时单占一档、排在最前：L1 超时与 L2 超时是同一个模型调用、同一份超时预算，
    修法一样（抬 VLM_TIMEOUT），按层拆开只是把同一件事拆成两栏。"""
    qcs = {
        "T1": _qc(error="RuntimeError: L1 转录失败 扫描件.png: APITimeoutError 超时"),
        "T2": _qc(error="RuntimeError: L1 转录失败 另一张.png: 转录被 max_tokens 截断了"),
        "T3": _qc(error="RuntimeError: L2 提取失败 某票: 参数不合法"),
        "T4": _qc(error="SystemExit: 未配置魔搭令牌"),
        "T5": _qc(error="ValueError: 不支持的文件类型 .txt"),
        "T6": _qc(error="RuntimeError: L2 提取失败 又一张: 连不上"),
        "T7": _qc(),
    }
    out = metrics.collect(qcs)
    got = {f["kind"]: f["count"] for f in out["failures"]}
    assert got == {"L2 提取失败": 2, "超时": 1, "L1 转录失败": 1,
                   "模型未配置": 1, "文件类型不支持": 1}, got
    assert out["totals"]["failed"] == 6 and out["totals"]["tickets"] == 7, out["totals"]
    assert [f["kind"] for f in out["failures"]][0] == "L2 提取失败", \
        "要按条数从多到少排，先看最大的一档"


def test_an_unknown_failure_falls_back_to_its_exception_class():
    """认不出的文案退回异常类名：不许把一整段堆栈当分类键，那会让每个失败各自成一类。"""
    out = metrics.collect({"T1": _qc(error="OSError: 磁盘满了：/data 没有空间"),
                           "T2": _qc(error="OSError: 磁盘满了：/data 没有空间（另一张）")})
    assert [f["kind"] for f in out["failures"]] == ["OSError"], out["failures"]
    assert out["failures"][0]["count"] == 2, out["failures"]


def test_a_ticket_without_usage_or_timings_does_not_break_the_sum():
    """老票（v1.20.0 之前落的 qc）没有 usage/timings 这两栏：不能因此把整张表算崩，
    也不许凭空补一个数——它就只是 0。"""
    qcs = {"T1": _qc(), "T2": _qc(usage=_use(l2=300))}
    out = metrics.collect(qcs)
    assert out["totals"]["tickets"] == 2 and out["totals"]["total_tokens"] == 300, out["totals"]
    assert out["totals"]["seconds"] == {}, "没耗时就不该凭空出现几档 0 秒"


def test_the_window_is_inclusive_and_skips_undated_tickets():
    qcs = {"T1": _qc(processed_at="2026-10-16T09:00:00"),
           "T2": _qc(processed_at="2026-10-17T09:00:00"),
           "T3": _qc(processed_at="2026-10-18T09:00:00"),
           "T4": _qc(processed_at="")}
    out = metrics.collect(qcs, "2026-10-17", "2026-10-18")
    assert out["totals"]["tickets"] == 2, "窗口含端点，没日期的票在给了窗口时无从判断，不进统计"
    assert [d["day"] for d in out["by_day"]] == ["2026-10-17", "2026-10-18"], out["by_day"]


def test_the_metrics_endpoint_is_admin_only():
    """这是运维视角的数：制单员/录入员用不到，也不该看到别人的账单。"""
    from fastapi.testclient import TestClient
    anon = TestClient(server.app)
    assert anon.get("/metrics").status_code == 401, "没登录就能看账单"
    src = Path(server.__file__).read_text(encoding="utf-8")
    block = re.search(r'@app\.get\("/metrics"\)(.*?)\n\n\n', src, re.S)
    assert block, "/metrics 路由没找到"
    assert "require_admin" in block.group(1), "/metrics 没挂管理员门禁"
    assert "metrics.collect(store.load_qc()" in block.group(1), \
        "/metrics 自己另算了一套：算术只许在 metrics.py 一处"
