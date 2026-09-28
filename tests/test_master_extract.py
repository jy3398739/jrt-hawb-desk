# -*- coding: utf-8 -*-
"""主单解析引擎回归：主单专用 prompt（36 列 + 主单口径 + 无图）、主单红旗、保真按主单列面回查。
全程不打真实模型：客户端换成假的（照 test_model_switch 的写法）。"""
import json
from types import SimpleNamespace

import config
import fidelity
import vlm_extract
from master_fields import MASTER_COLS, MASTER_FIELDS, build_transcript
from validator import validate_master

ORDER = {
    "MASTER_NO": "176-62400004",
    "SHIPPER_INFO": "METSO (TIANJIN) INVESTMENT CO., LTD. USCI: 911201117706402073 CHINA",
    "CONSIGNEE_INFO": "MAADEN GOLD AND BASE METALS CO NATIONAL SAUDI ARABIA EORI: SA4827430",
    "NOTIFY_INFO": "MOHAMMED SALEEM JEDDAH SAUDI ARABIA",
    "GOODS_NAME": "PISTON ROD HS CODE:8412909090",
    "GOODS_DESC": "***VAT NUMBER: 300057757900003",
    "AMS_RECORD": {"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901", "SLAC": 10,
                   "SHIPPER_INFO_COMP_NAME": "METSO (TIANJIN) INVESTMENT CO., LTD.",
                   "CONSIGNEE_INFO_COMP_NAME": "MAADEN GOLD AND BASE METALS CO NATIONAL",
                   "CONSIGNEE_INFO_EORI": "SA4827430", "NOTIFY_INFO_COMP_NAME": "MOHAMMED SALEEM",
                   "NOTIFYE_INFO_COUNTRY": "SA", "HMY_ID": 12, "SEND_STATUS": 0},
}


class _FakeClient:
    def __init__(self, reply="{}"):
        self.calls = []
        self._reply = reply
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._reply))])


def _fake(reply="{}"):
    fake = _FakeClient(reply)
    old = (vlm_extract._client, vlm_extract._client_channel)
    vlm_extract._client = fake
    vlm_extract._client_channel = config.vlm_base_url()
    return fake, old


def test_master_prompt_lists_every_column_and_the_master_scope():
    fake, slot = _fake(json.dumps({}))
    old_vision = config.MODEL_VISION
    config.MODEL_VISION = True          # 就算当前是视觉模型，主单也没有票面图可发
    try:
        vlm_extract.extract_master(build_transcript(ORDER))
    finally:
        config.MODEL_VISION = old_vision
        vlm_extract._client, vlm_extract._client_channel = slot
    prompt = [p["text"] for p in fake.calls[0]["messages"][0]["content"] if p["type"] == "text"][0]
    for col in MASTER_COLS:
        assert col in prompt, f"主单 prompt 少了列 {col}"
    assert "PIECES" not in prompt and "WEIGHT" not in prompt and "HAWB_NO" not in prompt, \
        "主单口径段要明说这些栏位不存在，而不是列出来让它填"
    assert "NOTIFY" in prompt and "通知人" in prompt
    assert "没有" in prompt and "图片" in prompt, "要声明本次没有票面图片"
    assert "逐字" in prompt, "保真铁律要跟着过来：值必须逐字摘自资料文本"


def test_extract_master_never_sends_images_even_on_vision_model():
    fake, slot = _fake(json.dumps({"MAWB_NO": "176-62400004"}))
    old_vision = config.MODEL_VISION
    config.MODEL_VISION = True
    try:
        vlm_extract.extract_master(build_transcript(ORDER))
    finally:
        config.MODEL_VISION = old_vision
        vlm_extract._client, vlm_extract._client_channel = slot
    parts = fake.calls[0]["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["text"], "主单侧无版式原件，绝不下发图片"


def test_extract_master_fills_every_column_and_needs_text():
    fake, slot = _fake(json.dumps({"MAWB_NO": "176-62400004", "SLAC": 10}))
    old_vision = config.MODEL_VISION
    config.MODEL_VISION = False
    try:
        data = vlm_extract.extract_master(build_transcript(ORDER))
        assert set(data) == set(MASTER_COLS), "缺列要补齐，前端才能稳定渲染 36 行"
        assert data["SLAC"] == 10
        try:
            vlm_extract.extract_master({"lines": [], "full_text": ""})
            raise AssertionError("空资料也去调模型了")
        except RuntimeError as e:
            assert "空" in str(e)
    finally:
        config.MODEL_VISION = old_vision
        vlm_extract._client, vlm_extract._client_channel = slot


def test_validate_master_flags_company_400_lines():
    ams = {c: None for c in MASTER_COLS}
    ams.update({"MAWB_NO": "176-6240004",                       # 少一位：公司直接 400
                "SHIPPER_INFO_POSTAL": "201413" + "x" * 50,
                "NOTIFY_INFO_TEL": "+861069479536",
                "NOTIFYE_INFO_COUNTRY": "SA",                   # 区号 +86 对不上 SA
                "SHIPPER_INFO_EORI": "911201117706402073"})
    warns = validate_master(ams)
    joined = "\n".join(warns)
    assert "MAWB_NO" in joined and "3位-横杠-8位" in joined
    assert any("SHIPPER_INFO_POSTAL" in w and "50" in w for w in warns), "限长 50 列要提前报，别等公司 400"
    assert any("NOTIFY_INFO_TEL" in w for w in warns), "通知人那组也要查（三组同规则，不是只查收发货人）"
    assert any("SHIPPER_INFO_EORI" in w for w in warns), "USCI 那类税号不是 EORI 形态"
    assert not any("CONSIGNEE_INFO_COUNTRY 电话" in w for w in warns)


def test_validate_master_reports_tax_with_nowhere_to_go():
    """主单表没有税号列：资料里印着 USCI/VAT 时要出声，而不是让模型硬塞进 EORI。"""
    ams = {c: None for c in MASTER_COLS}
    ams["MAWB_NO"] = "176-62400004"
    warns = validate_master(ams, build_transcript(ORDER))
    assert any("税号" in w and ("USCI" in w or "VAT" in w) for w in warns), warns


def test_validate_master_is_quiet_on_a_clean_record():
    ams = {c: None for c in MASTER_COLS}
    ams.update({"MAWB_NO": "176-62400004", "SLAC": 10,
                "SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_TEL": "+86 22 25322285",
                "CONSIGNEE_INFO_COUNTRY": "SA", "CONSIGNEE_INFO_EORI": "SA4827430",
                "NOTIFYE_INFO_COUNTRY": "SA"})
    assert validate_master(ams) == [], "干净记录不该有旗（否则复核的人会忽略真旗）"
    # 同一份值配上带 USCI/VAT 的资料文本，就该只剩"税号无处落点"这两条
    only_tax = validate_master(ams, build_transcript(ORDER))
    assert len(only_tax) == 2 and all("税号" in w for w in only_tax), only_tax


def test_fidelity_can_check_the_master_column_face():
    tr = build_transcript(ORDER)
    ams = {"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901",
           "CONSIGNEE_INFO_COMP_NAME": "MAADEN GOLD AND BASE METALS CO NATIONAL",
           "NOTIFY_INFO_COMP_NAME": "MOHAMMED SALEEM"}
    long_cols = [c for c in ams if c.endswith(("_COMP_NAME", "_COMP_ADDRESS"))]
    fid = fidelity.verify_fidelity(ams, tr, short=[c for c in ams if c not in long_cols],
                                   long=long_cols, hs_field="GOODS_INFO_HSCODE", tax=False)
    assert fid["checked"] >= 4 and fid["passed"] == fid["checked"], fid["failed"]
    bad = dict(ams, CONSIGNEE_INFO_COMP_NAME="MAADEN GOLD AND COPPER CO")
    fid2 = fidelity.verify_fidelity(bad, tr, short=["MAWB_NO", "GOODS_INFO_HSCODE",
                                                    "CONSIGNEE_INFO_COMP_NAME"],
                                    long=["CONSIGNEE_INFO_COMP_NAME"], tax=False)
    assert any(x["field"] == "CONSIGNEE_INFO_COMP_NAME" for x in fid2["failed"]), "编出来的公司名要被抓出来"


def test_field_table_groups_drive_the_ui_sections():
    groups = {g for _c, _l, g in MASTER_FIELDS}
    assert groups == {"cargo", "shipper", "consignee", "notify"}, groups
