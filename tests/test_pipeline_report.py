# -*- coding: utf-8 -*-
"""pipeline.process_file 的返回值回归。

这里单独测，是因为 jobs 那组用例把 process_file 整个换成了桩：管道自己少返回一样东西，
全量回归一片绿，直到线上统计算错了才发现。model 就是那样一样东西。
"""
import json
import tempfile
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
           pipeline.transcribe_l1, vlm_extract.extract_one)
    case = json.loads(FIX.read_text(encoding="utf-8"))
    try:
        config.VLM_MODEL, config.VLM_MODEL_CHOICE = "model-A", "preset-a"
        pipeline.transcribe_l1 = lambda src: {"lines": [{"i": 1, "text": "MAWB NO. 1", "bbox": [0, 0, 9, 9]}],
                                              "full_text": "MAWB NO. 1"}

        def extract_switching_midway(path, transcript=None):
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
         pipeline.transcribe_l1, vlm_extract.extract_one) = old
