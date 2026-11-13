# -*- coding: utf-8 -*-
"""主单解析引擎回归：主单专用 prompt（39 列 + 主单口径 + 无图）、主单红旗、保真按主单列面回查。
全程不打真实模型：客户端换成假的（照 test_model_switch 的写法）。"""
import json
from types import SimpleNamespace

import codes
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
                   "NOTIFY_INFO_COUNTRY": "SA", "HMY_ID": 12, "SEND_STATUS": 0},
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
                "NOTIFY_INFO_COUNTRY": "SA",                   # 区号 +86 对不上 SA
                "SHIPPER_INFO_EORI": "12345"})
    warns = validate_master(ams)
    joined = "\n".join(warns)
    assert "MAWB_NO" in joined and "3位-横杠-8位" in joined
    assert any("SHIPPER_INFO_POSTAL" in w and "50" in w for w in warns), "限长 50 列要提前报，别等公司 400"
    assert any("NOTIFY_INFO_TEL" in w for w in warns), "通知人那组也要查（三组同规则，不是只查收发货人）"
    assert any("SHIPPER_INFO_EORI" in w for w in warns), "怪值仍要报（EORI 格只收两位国家字母开头的海关号）"
    assert not any("CONSIGNEE_INFO_COUNTRY 电话" in w for w in warns)


def test_validate_master_points_uncaptured_tax_at_the_tax_column():
    """资料里印着 USCI/VAT 而列面里一份都没有时要出声，而且要说清今天该填哪一列。
    2026-11-14 两列拆分：税号落同主体 *_INFO_TAX、EORI 号落 _INFO_EORI——
    提示停在"无处落点，交人工定夺"等于把一条已经定了的事挂成待办。"""
    ams = {c: None for c in MASTER_COLS}
    ams["MAWB_NO"] = "176-62400004"
    warns = validate_master(ams, build_transcript(ORDER))
    assert any("税号" in w and "USCI" in w and "_INFO_TAX" in w for w in warns), warns
    assert any("_INFO_TAX" in w and "/" in w for w in warns), f"要说清落点与拼法: {warns}"
    assert any("_INFO_EORI" in w and "SA4827430" in w for w in warns), \
        f"EORI 标签的号漏了要指向 EORI 列: {warns}"


def test_validate_master_accepts_tax_numbers_joined_in_the_tax_column():
    """2026-11-14 两列拆分：税号住同主体 *_INFO_TAX，多个号用 " / " 拼一格、两个都留。
    反过来，USCI 落进 EORI 格现在是错位，要报——那格只收海关号。"""
    ams = {c: None for c in MASTER_COLS}
    ams.update({"MAWB_NO": "176-62400004", "SHIPPER_INFO_TAX": "911201117706402073 / 300057757900003"})
    warns = validate_master(ams, build_transcript(ORDER))
    assert not any("SHIPPER_INFO_TAX" in w for w in warns), warns
    assert not any("911201117706402073" in w for w in warns), f"USCI 已落到 TAX，不该再报: {warns}"
    assert not any("300057757900003" in w for w in warns), f"VAT 也已经被接住，不该再报: {warns}"
    bad = {c: None for c in MASTER_COLS}
    bad.update({"MAWB_NO": "176-62400004", "SHIPPER_INFO_EORI": "911201117706402073"})
    assert any("SHIPPER_INFO_EORI" in w for w in validate_master(bad)), "纯数字税号落在 EORI 格要报错位"


def test_validate_master_still_flags_odd_eori_values():
    """两列拆分后 EORI 格只收海关号，形态检查随之收紧成"像不像 EORI"：
    纯数字（那是税号，该去 _INFO_TAX）、带标签词、成句的散文、短得不成样子的都要报。"""
    for bad in ("USCI 911201117706402073", "12345", "911201117706402073 / 123",
                "SEE INVOICE / 911201117706402073", "税号见发票"):
        ams = {c: None for c in MASTER_COLS}
        ams.update({"MAWB_NO": "176-62400004", "SHIPPER_INFO_EORI": bad})
        assert any("SHIPPER_INFO_EORI" in w for w in validate_master(ams)), f"{bad} 该报形态异常"


def test_cnpj_with_its_own_slash_is_not_mistaken_for_two_numbers():
    """巴西 CNPJ 印成 07.454.234/0001-10：那根斜杠是号码的一部分。
    我们的拼串分隔符必须带空格（" / "），否则一个 CNPJ 会被当成两个号拆开重拼。"""
    ams = {c: None for c in MASTER_COLS}
    ams.update({"MAWB_NO": "176-62400004", "SHIPPER_INFO_TAX": "07.454.234/0001-10"})
    assert not any("SHIPPER_INFO_TAX" in w for w in validate_master(ams)), "带点的 CNPJ 是合法号码"
    merged = codes.merge_ids("IT03268900267", "07.454.234/0001-10")
    assert merged == "IT03268900267 / 07.454.234/0001-10", merged
    assert codes.merge_ids(merged, "07.454.234/0001-10") == merged, \
        f"再拼一次不许把 CNPJ 拆开：{codes.merge_ids(merged, '07.454.234/0001-10')}"


def test_master_prompt_splits_eori_and_tax_numbers_by_column():
    """口径改了，提示词必须跟着改（提示词进缓存指纹，旧解析会自动失效重跑）。
    2026-11-14 两列拆分：EORI 列只收海关号，税号统一进同主体 *_INFO_TAX、多号 " / " 拼——
    原来那句"所有税号统一进 EORI"必须消失，留着它模型会把 USCI 填进 EORI、TAX 列白留空。"""
    head = vlm_extract.MASTER_PROMPT_HEAD
    lines = head.splitlines()
    assert any("EORI" in ln and "_INFO_TAX" in ln and "税号" in ln for ln in lines), \
        f"缺一条把税号指向 *_INFO_TAX 的口径: {lines}"
    assert any("EORI" in ln and "海关号" in ln for ln in lines), "要写明 EORI 列只放海关号"
    assert any("_INFO_TAX" in ln and "/" in ln for ln in lines), "要写明多个号怎么落在一格（用 / 拼）"
    assert any("USCI" in ln and "_INFO_TAX" in ln for ln in lines), "USCI 该指到 TAX 列"
    assert not any("税号" in ln and "统一进 EORI" in ln for ln in lines), \
        "旧口径（税号统一进 EORI）还在，模型会把税号填错格"


def test_validate_master_flags_hs_with_label_left():
    """清洗兜不住的（模型给了个没标签的怪值）由红旗出声：HS 列只该是数字与逗号。"""
    ams = {c: None for c in MASTER_COLS}
    ams.update({"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "HS CODE:8526109"})
    assert any("GOODS_INFO_HSCODE" in w for w in validate_master(ams)), \
        "HS 列混进标签要报（与分单'税号混进了标签'同一条口径）"


def test_validate_master_is_quiet_on_a_clean_record():
    ams = {c: None for c in MASTER_COLS}
    ams.update({"MAWB_NO": "176-62400004", "SLAC": 10,
                "SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_TEL": "+86 22 25322285",
                "SHIPPER_INFO_TAX": "911201117706402073",
                "CONSIGNEE_INFO_COUNTRY": "SA", "CONSIGNEE_INFO_EORI": "SA4827430",
                "NOTIFY_INFO_COUNTRY": "SA"})
    assert validate_master(ams) == [], "干净记录不该有旗（否则复核的人会忽略真旗）"
    # 同一份值配上带 USCI/VAT 的资料文本：USCI 已被 TAX 格接住、EORI 号已被 EORI 格接住，
    # 只剩那个 VAT 没人管——就该只剩这一条，并说清落点
    only_tax = validate_master(ams, build_transcript(ORDER))
    assert len(only_tax) == 1 and "税号" in only_tax[0] and "_INFO_TAX" in only_tax[0], only_tax


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


def test_master_fidelity_does_not_flag_a_normalized_hs():
    """HS 是公司要求归一的列（去点号、逗号连接），拿"逐字找得到"去回查它等于自己打自己脸——
    分单侧同样不把 GOODS_HS_CODE 放进回查清单，漏抄交给反向核查兜。"""
    import master_pipeline as mp
    tr = {"lines": [{"i": 1, "text": "MASTER_NO: 176-62400004"},
                    {"i": 2, "text": "GOODS_NAME: PISTON ROD HS CODE:8412.9090,8517.1200"}],
          "full_text": "MASTER_NO: 176-62400004\nGOODS_NAME: PISTON ROD HS CODE:8412.9090,8517.1200"}
    raw = {"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "84129090,85171200"}
    assert "GOODS_INFO_HSCODE" not in mp._SHORT, "HS 列不进逐字回查清单"
    fid = fidelity.verify_fidelity(raw, tr, short=mp._SHORT, long=mp._LONG,
                                   hs_field="GOODS_INFO_HSCODE", tax=False)
    assert fid["failed"] == [], fid["failed"]


def test_regrouping_does_not_change_what_the_model_must_output():
    """分组只是给人看的排版；模型那份列清单必须还是 39 列一个不少（含通知人）。"""
    cols = [c for c, _l, _g in MASTER_FIELDS]
    assert len(cols) == 39
    assert any(c.startswith("NOTIFY") for c in cols) and "NOTIFY_INFO_COUNTRY" in cols
