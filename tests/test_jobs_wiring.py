# -*- coding: utf-8 -*-
"""jobs.handle_file 的接线回归：把 L0/L1/L2/L3/保真/落盘 粘起来的那 20 行。
管道本身用桩替换（真跑要调 API），这里只验"胶水有没有漏"：
少了任何一处校验调用，产出的 JSON 就会带着错单静静出厂，这类退化必须在离线测试里变红。
"""
import json
from pathlib import Path

import jobs
import to_air

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity" / "gl26090330_hawb.json"


class _FakeStore:
    def __init__(self):
        self.calls = []

    def archive_original(self, p):
        self.calls.append(("L0", Path(p).name))
        return {"md5": "stub"}

    def save_transcript(self, stem, tr):
        self.calls.append(("L1", stem))

    def save_result(self, stem, raw, air):
        self.calls.append(("L2L3", stem))

    def save_qc(self, stem, qc):
        self.calls.append(("QC", stem))

    def save_preview(self, stem, src):
        self.calls.append(("PREVIEW", stem))


def _run(raw, transcript, *, raises=None, save=True, src=None):
    """替换 jobs 的依赖跑一遍 handle_file，返回 (结果, 落盘调用记录)。src 模拟电子单转出的 PDF。"""
    fs = _FakeStore()
    real_store, real_pf = jobs.store, jobs.process_file

    def fake_process_file(path):
        if raises:
            raise raises
        out = {"raw": raw, "air": to_air.to_air(raw), "channel": "vlm", "transcript": transcript}
        if src is not None:
            out["src"] = src
        return out

    jobs.store, jobs.process_file = fs, fake_process_file
    try:
        return jobs.handle_file("某分单 TAO1234567.pdf", save=save), fs.calls
    finally:
        jobs.store, jobs.process_file = real_store, real_pf


def test_all_qc_layers_are_wired():
    case = json.loads(FIX.read_text(encoding="utf-8"))
    raw = dict(case["raw"])
    raw["TO3"] = "TO3X NOMAP"      # 码表里没有的地名：逼出 validate_air 的 L3 未映射告警
    r, calls = _run(raw, case["transcript"])
    assert r["error"] == "", r["error"]
    assert r["stem"] == "某分单 TAO1234567"
    assert any("MAWB_NO 缺失" in w for w in r["warns"]), r["warns"]          # validate_raw
    assert any(w.startswith("L3 ") and "TO3X NOMAP" in w for w in r["warns"]), r["warns"]  # validate_air
    assert r["fidelity"] and any("漏抄" in w for w in r["fidelity"]["warns"]), r["fidelity"]
    assert calls == [("L0", "某分单 TAO1234567.pdf"), ("L1", "某分单 TAO1234567"),
                     ("L2L3", "某分单 TAO1234567"), ("QC", "某分单 TAO1234567")], calls
    qc = r["qc"]
    assert qc["needs_review"] is True and qc["md5"] == "stub"
    assert any("MAWB_NO 缺失" in f for f in qc["flags"]), qc["flags"]
    assert any(f.startswith("保真 ") and "漏抄" in f for f in qc["flags"]), qc["flags"]


def test_save_false_touches_nothing():
    case = json.loads(FIX.read_text(encoding="utf-8"))
    r, calls = _run(case["raw"], case["transcript"], save=False)
    assert r["error"] == "" and calls == [], (r["error"], calls)
    assert r["fidelity"], "不落盘也要照校（HTTP 同步返回给业务方的那份同样要带告警）"
    assert r["qc"]["flags"], "不落盘也要把质检结论算出来，只是不写盘"


def test_missing_transcript_does_not_crash():
    case = json.loads(FIX.read_text(encoding="utf-8"))
    r, calls = _run(case["raw"], None)
    assert r["error"] == "" and r["fidelity"] is None
    assert [c[0] for c in calls] == ["L0", "L2L3", "QC"], calls
    assert r["qc"]["fidelity"] is None


def test_failure_is_reported_not_raised():
    r, calls = _run({}, None, raises=RuntimeError("L2 提取失败"))
    assert r["error"].startswith("RuntimeError: L2 提取失败"), r["error"]
    assert r["raw"] is None and r["fidelity"] is None
    assert [c[0] for c in calls] == ["L0", "QC"], "半路失败不得留下半成品 L2/L3"
    assert r["qc"]["needs_review"] is True and r["qc"]["flags"][0].startswith("处理失败")


def test_missing_token_is_reported_not_raised():
    """require_api_key 抛的是 SystemExit（BaseException），只 catch Exception 会整个漏过去：
    HTTP 端点变成光秃秃 500，CLI 批处理在第一张票上直接退出。令牌没配是可预期的运行态，
    要跟其他失败一样落进 error/qc，让人一眼看出是缺令牌，而不是以为票有问题。"""
    r, calls = _run({}, None, raises=SystemExit("未配置魔搭令牌：请在 .env 或环境变量中设置 MODELSCOPE_API_KEY"))
    assert r["error"].startswith("SystemExit: 未配置魔搭令牌"), r["error"]
    assert [c[0] for c in calls] == ["L0", "QC"], calls
    assert r["qc"]["needs_review"] is True
    assert any("处理失败" in f and "MODELSCOPE_API_KEY" in f for f in r["qc"]["flags"]), r["qc"]["flags"]


def test_converted_pdf_is_kept_so_the_desk_can_show_the_ticket():
    """电子单原件是 xlsx，浏览器渲染不了：转出的 PDF 得留一份，
    否则制单员刷新页面后就没法一边看票面一边核对字段了。"""
    case = json.loads(FIX.read_text(encoding="utf-8"))
    r, calls = _run(case["raw"], case["transcript"], src="/tmp/hawb_xlsx_pdf/某分单 TAO1234567.pdf")
    assert r["error"] == "", r["error"]
    assert ("PREVIEW", "某分单 TAO1234567") in calls, calls

    _, plain = _run(case["raw"], case["transcript"])
    assert not [c for c in plain if c[0] == "PREVIEW"], "没转换过就别凭空留预览件"

    _, nosave = _run(case["raw"], case["transcript"], save=False,
                     src="/tmp/hawb_xlsx_pdf/某分单 TAO1234567.pdf")
    assert not [c for c in nosave if c[0] == "PREVIEW"], "没勾落盘留档就一个字节都别写"