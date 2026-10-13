# -*- coding: utf-8 -*-
"""这一票到底花了多少 token、时间花在哪儿：2026-10-13 效率审查 P0-5。

生产代码里从前没有一处读 `resp.usage`（全库 prompt_tokens/completion_tokens 0 命中），qc 只有
一格 elapsed。于是"换哪个模型""开思考档多烧多少"这类决定只能拿秒数粗算——而模型额度是本项目
最贵的资源。这一层是**纯加法可观测性**：不改任何判据、不改送进模型的字节，账记在 qc 里。
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import config
import jobs
import usage
import vlm_extract

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity" / "gl26090330_hawb.json"


def _usage(prompt=100, completion=30, total=130, reasoning=None):
    u = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
    if reasoning is not None:
        u.completion_tokens_details = SimpleNamespace(reasoning_tokens=reasoning)
    return u


def _resp(content="{}", use=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                           usage=_usage() if use is None else use)


def _fake_client(reply=None):
    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        calls["kw"] = kw
        return _resp(json.dumps(reply if reply is not None else {}))

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


# ── 一、账本本身 ──────────────────────────────────────────────────────────


def test_one_ticket_is_one_window_and_the_next_starts_empty():
    """账按线程记、一张票一个窗口：take() 关窗。跨票不许串账——两张票记成一张，
    按模型分账的那个结论就整个错了（同一个教训见 test_pipeline_report）。"""
    usage.take()                              # 关掉可能残留的窗口
    usage.note(_resp(), "L2")                 # 没人开窗时不记账：脚本直调模型不该撑爆内存
    assert usage.take() == {}, "没 begin 就记账：批处理跑一晚会把每个线程都挂上一堆历史"
    usage.begin()
    usage.note(_resp(), "L1")
    got = usage.take()
    assert got["L1"]["calls"] == 1, got
    usage.note(_resp(), "L1")                 # 窗口已经关了
    assert usage.take() == {}


def test_tokens_add_up_per_stage_and_keep_the_reasoning_split():
    usage.begin()
    usage.note(_resp(use=_usage(1200, 260, 1460, 180)), "L2")
    usage.note(_resp(use=_usage(900, 40, 940)), "L2")
    usage.note(_resp(use=_usage(100, 30, 130)), "L1")
    got = usage.take()
    assert got["L2"]["calls"] == 2 and got["L1"]["calls"] == 1, got
    assert got["L2"]["prompt_tokens"] == 2100 and got["L2"]["total_tokens"] == 2400, got
    assert got["L2"]["reasoning_tokens"] == 180, \
        "思考档烧的那部分要单独看得见，否则「开思考值不值」永远答不出"
    assert got["L1"]["reasoning_tokens"] == 0, got


def test_a_response_without_usage_is_counted_but_stays_zero():
    """有些渠道不给 usage（或字段缺项）：那一趟照样要数得上来，但不许因此把解析带崩。"""
    usage.begin()
    usage.note(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]), "L2")
    got = usage.take()
    assert got["L2"]["calls"] == 1 and got["L2"]["total_tokens"] == 0, got


def test_booking_never_breaks_the_parse():
    """账本是旁路的：它自己出任何错都不许把一张票搞失败（额度记录没有票值钱）。"""
    usage.begin()
    usage.note(object(), "L2")                # 连 usage 都没有的对象
    usage.note(None, "L2")
    assert usage.take() == {"L2": {"calls": 2, "prompt_tokens": 0, "completion_tokens": 0,
                                   "total_tokens": 0, "reasoning_tokens": 0}}


# ── 二、模型调用那两处各自进自己的栏 ──────────────────────────────────────


def test_l2_and_the_master_chain_pay_into_different_stages():
    """分单 L2 与主单链共用同一个 _chat_json：记到同一个栏里就答不出"主单一张多少钱"。"""
    usage.begin()
    client, calls = _fake_client({"PIECES": 1})
    vlm_extract._chat_json("提示", [], ["PIECES"], "某票", client=client, stage="L2")
    vlm_extract._chat_json("提示", [], ["MAWB_NO"], "主单", client=client, stage="主单")
    got = usage.take()
    assert calls["n"] == 2
    assert set(got) == {"L2", "主单"}, got
    assert got["L2"]["calls"] == 1 and got["主单"]["calls"] == 1, got


def test_l1_transcription_books_under_its_own_stage():
    """L1 转录与 L2 提取是两次独立收费：合成一笔就分不出"扫描件那条链贵在转录还是提取"。"""
    import transcribe
    client, calls = _fake_client({"lines": [{"i": 1, "text": "MAWB NO. 1", "bbox": [0, 0, 9, 9]}]})
    old = (transcribe._client, transcribe._client_channel, transcribe.img_to_data_urls,
           config.MODEL_VISION)
    transcribe._client, transcribe._client_channel = client, config.vlm_base_url()
    transcribe.img_to_data_urls = lambda p: ["data:image/png;base64,AAAA"]
    config.MODEL_VISION = True
    try:
        usage.begin()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "某分单 TAO1.pdf"
            f.write_bytes(b"%PDF-1.4")
            tr = transcribe.transcribe_one(f)
        got = usage.take()
        assert calls["n"] == 1 and tr["lines"], tr
        assert got["L1"]["calls"] == 1 and got["L1"]["prompt_tokens"] == 100, got
    finally:
        (transcribe._client, transcribe._client_channel, transcribe.img_to_data_urls,
         config.MODEL_VISION) = old


# ── 三、落进质检记录 ──────────────────────────────────────────────────────


def test_the_master_chain_books_onto_its_own_record():
    """主单链默认跑公司百炼、又是"开思考档值不值"最关心的那条：账要落到这条主单的 qc 里，
    不然只有分单有数，而那条链恰恰更贵。"""
    import master_pipeline
    client, calls = _fake_client({"MAWB_NO": "176-62400004"})
    old = (vlm_extract._client_for, master_pipeline._write)
    vlm_extract._client_for = lambda bundle: client
    master_pipeline._write = lambda rec: rec
    try:
        usage.take()                       # 上一张票不该把账带进这一条
        rec = master_pipeline.run_master("176-62400004",
                                        {"MASTER_NO": "176-62400004",
                                         "AMS_RECORD": {"MAWB_NO": "176-62400004"}})
    finally:
        vlm_extract._client_for, master_pipeline._write = old
    assert calls["n"] == 1, calls
    assert rec["qc"]["usage"] == {"主单": {"calls": 1, "prompt_tokens": 100,
                                           "completion_tokens": 30, "total_tokens": 130,
                                           "reasoning_tokens": 0}}, rec["qc"]
    assert usage.take() == {}, "取过就该关窗：下一条主单从空账开始"


def _handle_with(stub) -> dict:
    old = jobs.process_file
    jobs.process_file = stub
    try:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "某分单 TAO7654321.pdf"
            f.write_bytes(b"%PDF-1.4")
            return jobs.handle_file(f, save=False)
    finally:
        jobs.process_file = old


def test_qc_carries_the_tokens_and_where_the_time_went():
    """qc 里要能一眼读出：这张票几次调用、各花多少 token，以及渲染/转录/提取/清洗各占几秒。
    没有分段耗时，"扫描件渲染两遍值不值得修"这种事只能靠猜。"""
    case = json.loads(FIX.read_text(encoding="utf-8"))

    def stub(path, do_transcript=True):
        usage.note(_resp(use=_usage(1200, 260, 1460, 180)), "L2")
        usage.note(_resp(use=_usage(400, 60, 460)), "L1")
        return {"raw": case["raw"], "air": dict(case["raw"]), "channel": "vlm",
                "transcript": None, "src": str(path),
                "model": "intern-s2-official", "model_choice": "s2",
                "timings": {"render": 0.4, "l1": 5.1, "l2": 8.2, "l3": 0.0}}

    qc = _handle_with(stub)["qc"]
    assert qc["usage"]["L2"]["total_tokens"] == 1460, qc["usage"]
    assert qc["usage"]["L2"]["reasoning_tokens"] == 180, qc["usage"]
    assert qc["usage"]["L1"]["calls"] == 1, qc["usage"]
    assert qc["timings"]["l2"] == 8.2 and qc["timings"]["render"] == 0.4, qc["timings"]


def test_a_ticket_that_died_midway_still_reports_what_it_spent():
    """解析失败的票照样烧了额度——那几张最需要算清"这次白花多少"。
    账是在收尾取的，所以中途抛异常不许把账一起丢掉。"""
    def stub(path, do_transcript=True):
        usage.note(_resp(use=_usage(1200, 260, 1460)), "L2")
        raise RuntimeError("模型答的不是 JSON")

    r = _handle_with(stub)
    assert r["error"].startswith("RuntimeError"), r
    assert r["qc"]["usage"]["L2"]["prompt_tokens"] == 1200, r["qc"]


def test_two_tickets_in_a_row_do_not_share_the_bill():
    """批处理与队列 worker 是同一个线程反复跑：上一张的账没清干净，下一张就报出双倍的数。"""
    case = json.loads(FIX.read_text(encoding="utf-8"))

    def stub(path, do_transcript=True):
        usage.note(_resp(use=_usage(100, 10, 110)), "L2")
        return {"raw": case["raw"], "air": dict(case["raw"]), "channel": "vlm",
                "transcript": None, "src": str(path), "model": "m", "model_choice": "c",
                "timings": {}}

    first, second = _handle_with(stub), _handle_with(stub)
    assert first["qc"]["usage"]["L2"]["total_tokens"] == 110, first["qc"]["usage"]
    assert second["qc"]["usage"]["L2"]["total_tokens"] == 110, second["qc"]["usage"]
