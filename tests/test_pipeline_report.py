# -*- coding: utf-8 -*-
"""pipeline.process_file 的返回值回归。

这里单独测，是因为 jobs 那组用例把 process_file 整个换成了桩：管道自己少返回一样东西，
全量回归一片绿，直到线上统计算错了才发现。model 就是那样一样东西。
"""
import json
import tempfile
import time
from pathlib import Path

import config
import pipeline
import vlm_extract

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity" / "gl26090330_hawb.json"


def test_pipeline_reports_the_model_that_actually_read_the_ticket():
    """质检记录里的 model 必须是**读这张票的那个模型**，不是建记录那一刻 config 里的值。

    审核台顶栏能在解析进行中热切模型（管理员会话）。一张票从送进去到出结果要十几秒，
    这期间切了模型，按"事后现读 config"记账就会把这张票记到下一个模型头上——
    而按模型分账正是需求三用来判断"能不能对接平台数据"的那个数，记错了整个结论就反了。
    所以模型名在调用模型的前一刻取好，随返回值一起交回去。"""
    old = (config.VLM_MODEL, config.VLM_MODEL_CHOICE,
           pipeline.transcribe_l1, vlm_extract.extract_one, vlm_extract.img_to_data_urls)
    case = json.loads(FIX.read_text(encoding="utf-8"))
    try:
        config.VLM_MODEL, config.VLM_MODEL_CHOICE = "model-A", "preset-a"
        vlm_extract.img_to_data_urls = lambda p: []      # 这一条测的是模型名，不是渲染
        pipeline.transcribe_l1 = lambda src, urls=None: {"lines": [{"i": 1, "text": "MAWB NO. 1", "bbox": [0, 0, 9, 9]}],
                                              "full_text": "MAWB NO. 1"}

        def extract_switching_midway(path, transcript=None, urls=None):
            config.VLM_MODEL, config.VLM_MODEL_CHOICE = "model-B", "preset-b"   # 解析途中被热切
            return dict(case["raw"])

        pipeline.vlm_extract.extract_one = extract_switching_midway
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "某分单 TAO1234567.pdf"
            pdf.write_bytes(b"%PDF-1.4 ticket")
            res = pipeline.process_file(pdf)
        assert res.get("model") == "model-A", \
            f"取的是调用前的模型名吗？返回 {res.get('model')!r}（中途已切到 model-B）"
        assert res["model_choice"] == "preset-a", res.get("model_choice")
    finally:
        (config.VLM_MODEL, config.VLM_MODEL_CHOICE,
         pipeline.transcribe_l1, vlm_extract.extract_one,
         vlm_extract.img_to_data_urls) = old


# ── 扫描件渲染与分段耗时（2026-10-13 效率审查 P0-3）──────────────────────


def _swap(new):
    """把管道要用的那几处换成桩，返回旧值；每个桩都接 urls，签名与真实现一致。"""
    old = (config.MODEL_VISION, pipeline.transcribe.transcribe_pdf_text_layer,
           pipeline.transcribe.transcribe_one, vlm_extract.extract_one,
           vlm_extract.img_to_data_urls, pipeline.to_air.to_air)
    config.MODEL_VISION = new.pop("_vision", True)
    pipeline.transcribe.transcribe_pdf_text_layer = new.get("text_layer", lambda p, n: None)
    pipeline.transcribe.transcribe_one = new.get("l1", lambda p, urls=None: None)
    vlm_extract.extract_one = new.get("l2", lambda p, transcript=None, urls=None: {})
    vlm_extract.img_to_data_urls = new.get("render", lambda p: [])
    pipeline.to_air.to_air = new.get("l3", lambda raw: dict(raw))
    return old


def _restore(old):
    (config.MODEL_VISION, pipeline.transcribe.transcribe_pdf_text_layer,
     pipeline.transcribe.transcribe_one, vlm_extract.extract_one,
     vlm_extract.img_to_data_urls, pipeline.to_air.to_air) = old


def test_a_scanned_ticket_is_rendered_once_not_twice():
    """L1 与 L2 从前各调一次 `img_to_data_urls`：同一份 PDF 开两遍、每页 pymupdf 渲染两遍、
    PNG+base64 各编两遍——一张四页票白付约 1 秒，而 `DESK_CONCURRENCY=4` 时内存里同时握着
    四份 base64。现在管道里渲染一次、两处共用同一份字节（所以准确率口径一个字都没变）。"""
    case = json.loads(FIX.read_text(encoding="utf-8"))
    n = {"render": 0}
    seen = {}

    def counting_render(path):
        n["render"] += 1
        return ["data:image/png;base64,AAAA"]

    def fake_l1(path, urls=None):
        seen["l1"] = urls
        return {"lines": [{"i": 1, "text": "MAWB NO. 1", "bbox": [0, 0, 9, 9]}],
                "full_text": "MAWB NO. 1"}

    def fake_l2(path, transcript=None, urls=None):
        seen["l2"] = urls
        return dict(case["raw"])

    old = _swap({"render": counting_render, "l1": fake_l1, "l2": fake_l2,
                 "text_layer": lambda p, mn: None})       # 扫描件：没有文字层
    try:
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "某分单 TAO7654321.pdf"
            pdf.write_bytes(b"%PDF-1.4 ticket")
            res = pipeline.process_file(pdf)
    finally:
        _restore(old)
    assert n["render"] == 1, f"票面被渲染了 {n['render']} 遍：L1 与 L2 该共用同一份页图"
    assert seen["l1"] == seen["l2"] == ["data:image/png;base64,AAAA"], \
        f"两处收到的页图不是同一份：L1={seen['l1']!r} L2={seen['l2']!r}"
    assert res["transcript"], "转录结果照旧要带回去"


def test_a_text_only_model_still_gets_no_images():
    """纯文本模型不下发票面图片——这条口径比省时间重要（收图的纯文本模型会顺着编，
    实测见 config 注记）。所以渲染一次也不能给它。"""
    case = json.loads(FIX.read_text(encoding="utf-8"))
    n = {"render": 0}
    seen = {}

    def counting_render(path):
        n["render"] += 1
        return ["data:image/png;base64,AAAA"]

    old = _swap({"_vision": False, "render": counting_render,
                 "l1": lambda p, urls=None: seen.update(
                     {"l1": urls}) or {"lines": [{"i": 1, "text": "MAWB NO. 1", "bbox": [0, 0, 9, 9]}],
                                       "full_text": "MAWB"},
                 "l2": lambda p, transcript=None, urls=None: seen.update({"l2": urls}) or dict(case["raw"])})
    try:
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "某分单 TAO8888888.pdf"
            pdf.write_bytes(b"%PDF-1.4 ticket")
            pipeline.process_file(pdf)
    finally:
        _restore(old)
    assert n["render"] == 0, "当前模型没有视觉，一页都不该渲染"
    assert seen["l1"] == [] and seen["l2"] == [], seen


def test_process_file_says_where_the_time_went():
    """分段耗时（渲染 / 转录 / 提取 / 清洗）是"这一秒值不值得省"的依据：没有 render 那一格，
    扫描件渲染两遍这种事只能靠猜。各段分别记，缺一段就少一格的判断依据。"""
    case = json.loads(FIX.read_text(encoding="utf-8"))

    def slow_l2(path, transcript=None, urls=None):
        time.sleep(0.05)
        return dict(case["raw"])

    old = _swap({"l1": lambda p, urls=None: {"lines": [], "full_text": ""},
                 "l2": slow_l2})
    try:
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "某分单 TAO2222222.pdf"
            pdf.write_bytes(b"%PDF-1.4 ticket")
            res = pipeline.process_file(pdf)
    finally:
        _restore(old)
    t = res["timings"]
    assert set(t) == {"render", "l1", "l2", "l3"}, t
    assert t["l2"] >= 0.04, f"提取那一格没在计时：{t}"
    for k, v in t.items():
        assert isinstance(v, float) and v >= 0, f"{k} 那一格不是个能读的秒数：{v!r}"


# ── L1 转录复用（2026-10-18 效率审查 E1）──────────────────────────────────


def test_a_reused_l1_skips_the_transcription_call():
    """自动路径重跑一张票时，L1 直接拿上次那份，不再调模型。
    转录与提取是两次独立收费：扫描件重跑一次就白付一半的钱。"""
    case = json.loads(FIX.read_text(encoding="utf-8"))
    n = {"l1": 0}
    seen = {}

    def counting_l1(path, urls=None):
        n["l1"] += 1
        return {"lines": [{"i": 1, "text": "MAWB NO. 1", "bbox": [0, 0, 9, 9]}],
                "full_text": "MAWB NO. 1"}

    def fake_l2(path, transcript=None, urls=None):
        seen["tr"] = transcript
        return dict(case["raw"])

    stored = {"lines": [{"i": 1, "text": "SHIPPER ALPHA", "bbox": [0, 0, 9, 9]}],
              "full_text": "SHIPPER ALPHA"}
    old = _swap({"l1": counting_l1, "l2": fake_l2})
    try:
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "某分单 TAO3333333.pdf"
            pdf.write_bytes(b"%PDF-1.4 ticket")
            res = pipeline.process_file(pdf, reuse=stored)
    finally:
        _restore(old)
    assert n["l1"] == 0, "给了上次的转录还是又调了一次 L1：这一步是单独收费的"
    assert res["transcript"] == stored, "交回去的不是复用的那一份"
    assert seen["tr"] == stored, "L2 拿到的不是复用的那一份"


def test_the_l1_we_already_paid_for_reaches_the_caller_when_l2_dies():
    """L2 挂了，L1 的钱已经花出去：那份转录必须交回调用方去落盘。
    管道自己不留盘，所以要有条路把它带出来——否则这份钱白花。"""
    def boom(path, transcript=None, urls=None):
        raise RuntimeError("L2 提取失败")

    old = _swap({"l1": lambda p, urls=None: {"lines": [{"i": 1, "text": "X", "bbox": [0, 0, 1, 1]}],
                                             "full_text": "X"},
                 "l2": boom})
    bag = {}
    try:
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "某分单 TAO4444444.pdf"
            pdf.write_bytes(b"%PDF-1.4 ticket")
            try:
                pipeline.process_file(pdf, bag=bag)
            except RuntimeError:
                pass
    finally:
        _restore(old)
    assert bag.get("transcript"), "L2 失败时 L1 转录没交出来：这份钱白花了"
    assert bag["transcript"]["full_text"] == "X"
