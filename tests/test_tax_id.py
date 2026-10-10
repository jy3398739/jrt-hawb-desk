# -*- coding: utf-8 -*-
"""税号（USCI/统一社会信用代码、CNPJ、VAT NO、TAX ID）专项回归。

历史：37 字段时代票面没有税号落点，模型按规则既不放 TEL 也不放 EORI，号就直接丢了
（DRAFT - HAWB No_ TSN10359645 发货人印着 USCI: 911201117706402073 是那张起点票），
所以当年加了 `*_INFO_TAX_ID` 两列，并配了"漏号必须反向查"的红旗。

2026-10-10 业主定案：**这两列删掉，税号一律填进同主体的 `*_INFO_EORI`**（公司 AMS 列面
本来就没有税号格，v1.8.0 起提交前已经是"两个号用 ' / ' 拼进 EORI 一格"）。
这一档改动之后，EORI 那一格会同时装欧码海关号与本土税号，所以下面钉死三件事：
① 号不丢（正向照抄 + 反向漏号红旗都要指向 EORI）；
② 不重复（同格去重、反复暂存不越拼越长）；
③ 老记录不静默掉号（air 里还留着 TAX_ID 的历史票，提交时照样并进去）。
"""
import json
import re
from pathlib import Path

import codes
import company_api
import fieldspec
import to_air
import web_src
import validator
from fidelity import find_missing_tax, verify_fidelity

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity"
USCI = "911201117706402073"


def vlm_fields():
    import vlm_extract
    return vlm_extract.FIELDS


def desk_keys():
    html = web_src.desk()
    body = re.search(r"const FIELDS = \[(.*?)\n\];", html, re.S).group(1)
    return re.findall(r'\["([A-Z][A-Z0-9_]+)"', body)


def l2(**over):
    """一张干净的 L2 骨架（所有列都存在但为空），只覆盖传进来的那几格。"""
    d = {k: "" for k in codes.TARGET_KEYS_OUT}
    d.update(over)
    return d


def test_field_contract_is_38_and_has_no_tax_column():
    """字段清单只有一个真值源：后端契约、prompt 要抽的字段、前端表格行三处必须同集合，
    而且 `*_INFO_TAX_ID` 这一家三口都得消失——留一处就有一处还在往空列里写。"""
    assert len(codes.TARGET_KEYS_OUT) == 38 and len(set(codes.TARGET_KEYS_OUT)) == 38
    assert set(codes.TARGET_KEYS_OUT) == set(vlm_fields()), \
        "prompt 的字段清单与后端契约不一致，模型抽出来的键会对不上列"
    assert set(codes.TARGET_KEYS_OUT) == set(desk_keys()), \
        "审核台字段行与后端契约不一致，制单员看不到就是没审核"
    assert not [k for k in codes.TARGET_KEYS_OUT if k.endswith("_TAX_ID")], "税号列没删干净"
    assert not [k for k in fieldspec.desk_keys() if k.endswith("_TAX_ID")], "fieldspec 里还剩税号列"
    assert "GOODS_HS_CODE" in codes.TARGET_KEYS_OUT, "HS 码这列不在这次改动里"


def test_prompt_sends_tax_into_the_eori_cell():
    """提示词必须说清"税号填进 EORI 那一格"，否则模型照旧无处安放、干脆丢掉。"""
    import vlm_extract
    blob = vlm_extract.PROMPT_HEAD
    assert "TAX_ID" not in blob, "提示词还在要求一个已经删掉的字段"
    assert "EORI" in blob and "USCI" in blob and "CNPJ" in blob, "税号标签表还在，但要落到 EORI 上"


def test_l3_folds_a_tax_pulled_out_of_the_address_into_eori():
    """模型把 USCI/CNPJ 留在地址里：L3 摘干净地址，号进同主体的 EORI 格。"""
    l2d = l2(MAWB_NO="999-30825351", HAWB_NO="TSN10359645",
             SHIPPER_INFO_COMP_NAME="PARKER HANNIFIN HYDRAULICS (TIANJIN) CO LTD",
             SHIPPER_INFO_COMP_ADDRESS="NO 21 HONGYUAN ROAD, TIANJIN 300385 CN, "
                                       "TE +862258388999 USCI: " + USCI,
             SHIPPER_INFO_CITY="TIANJIN", SHIPPER_INFO_COUNTRY="CN",
             SHIPPER_INFO_POSTAL="300385", SHIPPER_INFO_TEL="+862258388999 USCI: " + USCI)
    l3 = to_air.to_air(l2d)
    assert l3["SHIPPER_INFO_TEL"] == "+862258388999", l3["SHIPPER_INFO_TEL"]
    assert l3["SHIPPER_INFO_EORI"] == USCI, "税号要落在 EORI 格，不是消失"
    assert USCI not in l3["SHIPPER_INFO_COMP_ADDRESS"], l3["SHIPPER_INFO_COMP_ADDRESS"]
    assert USCI not in l3["SHIPPER_INFO"], "合并串里也不能再挂着没归位的号"


def test_l3_keeps_both_the_eori_and_the_tax_number_in_one_cell():
    """欧票常见一格两个号：海关 EORI + 本土 VAT/USCI。两个都要留，用 ' / ' 拼在一格，
    顺序是 EORI 在前（那是公司那边这一列的本名）。"""
    l2d = l2(HAWB_NO="BJS00032109",
             CONSIGNEE_INFO_COMP_NAME="CALZONI HYDRAULICS ITALY SRL",
             CONSIGNEE_INFO_COMP_ADDRESS="VIA CASTELDEBOLE 10, 40069 IT",
             CONSIGNEE_INFO_CITTY="ZOLA PREDOSA", CONSIGNEE_INFO_COUNTRY="IT",
             CONSIGNEE_INFO_EORI="IT03268900267",
             CONSIGNEE_INFO_TEL="+390422470376")
    l3 = to_air.to_air(l2d)
    assert l3["CONSIGNEE_INFO_EORI"] == "IT03268900267", "票面没第二个号就别硬拼"
    l2d["CONSIGNEE_INFO_COMP_ADDRESS"] = "VIA CASTELDEBOLE 10, VAT IT03268900267, 40069 IT"
    l3 = to_air.to_air(l2d)
    assert l3["CONSIGNEE_INFO_EORI"] == "IT03268900267", "同一个号不该在同一格里写两遍"


def test_l3_strips_the_ticket_label_off_the_number_before_filing_it():
    """票面把 'VAT#769638661' 整格印出来：号码本身进 EORI，标签留在原地会被形态检查当异常。"""
    assert codes.clean_tax("VAT#769638661") == "769638661"
    assert codes.clean_tax("TAX ID 123456789") == "123456789"
    assert codes.clean_tax("PL7010468168") == "PL7010468168", "两位国家码开头的是号码本身"
    assert codes.clean_tax("00.280.273/0001-37") == "00.280.273/0001-37"
    l2d = l2(HAWB_NO="TAO-7268550",
             CONSIGNEE_INFO_COMP_NAME="BIG METAL LTD",
             CONSIGNEE_INFO_COMP_ADDRESS="OCCUPATIONAL ROAD 1, LONDON SE17 3BE GB",
             CONSIGNEE_INFO_CITTY="LONDON", CONSIGNEE_INFO_COUNTRY="GB",
             CONSIGNEE_INFO_EORI="VAT#769638661")
    assert to_air.to_air(l2d)["CONSIGNEE_INFO_EORI"] == "769638661"


def test_l3_falls_back_to_the_merged_info_string():
    """模型只把 USCI 留在 L2 合并串里（地址、电话都干净）时，L3 重建合并串前先摘出来，
    否则整号随合并串被重写而丢失（BJS00032108 发货人即如此）。"""
    l2d = l2(MAWB_NO="999-30825351", HAWB_NO="BJS00032108",
             SHIPPER_INFO="BEIJING SHUNYI CO LTD, ROOM 101 BEIJING CN, "
                          "TEL: +861065766886 USCI:91110105562078969J",
             SHIPPER_INFO_COMP_NAME="BEIJING SHUNYI CO LTD",
             SHIPPER_INFO_COMP_ADDRESS="ROOM 101 BEIJING 101300 CN",
             SHIPPER_INFO_CITY="BEIJING", SHIPPER_INFO_COUNTRY="CN",
             SHIPPER_INFO_TEL="+861065766886")
    l3 = to_air.to_air(l2d)
    assert l3["SHIPPER_INFO_EORI"] == "91110105562078969J", l3["SHIPPER_INFO_EORI"]
    assert l3["SHIPPER_INFO_TEL"] == "+861065766886", l3["SHIPPER_INFO_TEL"]


def test_l3_moves_a_legacy_tax_column_into_eori():
    """归档里的老 L2 还带着已删除的 `*_INFO_TAX_ID`（39 列那几年解析出来的票）：
    `--recheck` 重算 L3 时必须把它并进 EORI——不并就等于把当年核对过的号静默丢掉，
    而重跑一遍模型又是一次额度。出口由 to_air 末尾按列面重建，那列空壳不会跟着出去。"""
    l2d = l2(HAWB_NO="OLD1001",
             SHIPPER_INFO_EORI="SE5563646560", SHIPPER_INFO_TAX_ID=USCI,
             CONSIGNEE_INFO_TAX_ID="00280273000218")
    l3 = to_air.to_air(l2d)
    assert l3["SHIPPER_INFO_EORI"] == "SE5563646560 / " + USCI, l3["SHIPPER_INFO_EORI"]
    assert l3["CONSIGNEE_INFO_EORI"] == "00280273000218", l3["CONSIGNEE_INFO_EORI"]


def test_merge_ids_is_idempotent_and_splits_on_spaced_slash_only():
    """暂存会反复写同一格：先拆再拼，反复喂同一串不能越拼越长。
    分隔符必须两侧带空格——巴西 CNPJ 自己就含斜杠（07.454.234/0001-10），裸斜杠一切号码就碎了。"""
    one = codes.merge_ids("IT13055460961", "9111010880211232X4")
    assert one == "IT13055460961 / 9111010880211232X4"
    assert codes.merge_ids(one, "9111010880211232X4") == one, "再拼一次必须原样，不能变成 A / B / B"
    cnpj = "07.454.234/0001-10"
    assert codes.merge_ids(cnpj) == cnpj, "CNPJ 自己的斜杠不许被当成隔符拆开"
    assert codes.merge_ids("", None, "  ") == ""


def test_missing_tax_flag_points_at_the_eori_cell():
    """反向漏号红旗（票面有、字段里没有）还得响，而且要说清现在该填哪一列。"""
    c = json.loads((FIX / "draft_hawb_no_tsn10359645.json").read_text(encoding="utf-8"))
    assert find_missing_tax(c["raw"], c["transcript"]) == [("USCI", USCI)]
    fid = verify_fidelity(c["raw"], c["transcript"])
    hits = [x for x in fid["failed"] if x["field"] == "TAX_ID"]
    assert len(hits) == 1 and USCI in hits[0]["reason"], fid["failed"]
    assert "SHIPPER_INFO_EORI" in hits[0]["reason"] and "CONSIGNEE_INFO_EORI" in hits[0]["reason"], \
        "红旗得指向今天真正那一列，否则复核的人按图去找一个已经不存在的格子"
    assert find_missing_tax(dict(c["raw"], SHIPPER_INFO_EORI=USCI), c["transcript"]) == [], \
        "号落进 EORI 就该闭嘴"


def test_missing_tax_still_catches_eu_eori_labels():
    """欧票把海关号印成 `EORI IT03268900267`：号已被任何字段收走（含 EORI 格、或还黏在地址行）
    则闭嘴，不给人添重复旗。"""
    tr = {"lines": [{"text": t} for t in [
        "OZONE S.R.L", "VIALE DELLE INDUSTRIE 10", "EORI IT03268900267", "TEL 390422470376"]],
        "full_text": ""}
    assert find_missing_tax({}, tr) == [("EORI", "IT03268900267")]
    assert find_missing_tax({"CONSIGNEE_INFO_EORI": "IT03268900267"}, tr) == []
    tr3 = {"lines": [{"text": "FORMERVANGEN 5 EORI NO DK28490704"}], "full_text": ""}
    glued = {"CONSIGNEE_INFO_COMP_ADDRESS": "FORMERVANGEN 5 EORI NO DK28490704"}
    assert find_missing_tax(glued, tr3) == [], "号还黏在我方地址行里=已捕获，不重复报"


def test_tax_check_needs_a_label_and_a_real_number():
    """只认带标签的号：裸 18 位数字可能是货值/账号，抓它做红旗就是误报。"""
    tr = {"lines": [{"text": t} for t in [
        "TIANJIN 300385 CN", "TE +862258388999 USCI: " + USCI, "ACCOUNT 640958392012345678",
        "TAX ID: 12345"]], "full_text": ""}
    assert find_missing_tax({}, tr) == [("USCI", USCI)], "带标签的真税号必须被抓到"
    assert find_missing_tax({"X": ""}, {"lines": [{"text": "ACCOUNT 640958392012345678"}]}) == [], \
        "无标签裸数字不该当税号报出来"


def test_fidelity_checks_each_number_in_a_joined_eori_cell():
    """一格两个号时票面常常分两处印（收货人名下 IT 海关号、页底 customs 区 VAT）。
    正向保真必须逐段回查——拿拼好的整串去找原文，每张双号票都会挂一条假红旗，
    而假红旗看多了人会连真红旗一起无视。"""
    tr = {"lines": [{"text": "CALZONI HYDRAULICS ITALY SRL"},
                    {"text": "EORI IT03268900267 VIA CASTELDEBOLE 10"},
                    {"text": "VAT NO 12345678901"},
                    {"text": "TEL +390422470376"}], "full_text": ""}
    raw = {"CONSIGNEE_INFO_EORI": "IT03268900267 / 12345678901"}
    fid = verify_fidelity(raw, tr)
    assert [x for x in fid["failed"] if x["field"] == "CONSIGNEE_INFO_EORI"] == [], fid["failed"]
    bad = verify_fidelity({"CONSIGNEE_INFO_EORI": "IT03268900267 / 99999999999"}, tr)
    assert [x for x in bad["failed"] if x["field"] == "CONSIGNEE_INFO_EORI"], "编出来的号要照样抓出来"


def test_validator_reads_tax_shape_out_of_the_eori_cell():
    """EORI 格现在一格装多个号：形态检查要按 ' / ' 拆开逐段判，
    截断的 USCI、串进电话栏的号、混进来的标签都得照样报出来。"""
    def w(air):
        return [x for x in validator.validate_air(air) if "EORI" in x]
    ok = {"SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_EORI": USCI, "SHIPPER_INFO_TEL": "+862258388999"}
    assert not w(ok), "18 位 USCI 是合法形态，别乱报：" + str(w(ok))
    assert w(dict(ok, SHIPPER_INFO_EORI="911201117706402")), "被截断的 USCI 必须报"
    assert w(dict(ok, SHIPPER_INFO_EORI=USCI, SHIPPER_INFO_TEL=USCI)), "税号串进电话栏要报出来"
    br = {"CONSIGNEE_INFO_COUNTRY": "BR", "CONSIGNEE_INFO_EORI": "00.280.273/0001-37"}
    assert not w(br), "CNPJ 带点斜横线是号码本身，不该按长度误判：" + str(w(br))
    assert w(dict(ok, SHIPPER_INFO_EORI="ABC123")), "短到不像号的要报"
    assert w({"CONSIGNEE_INFO_COUNTRY": "BR", "CONSIGNEE_INFO_EORI": "1234567"}), \
        "过短税号只在 CN 分支判长度等于没判"
    both = {"SHIPPER_INFO_COUNTRY": "IT", "SHIPPER_INFO_EORI": "IT13055460961 / " + USCI}
    assert not w(both), "两个号拼一格都得认：" + str(w(both))
    assert any("混进了标签" in x for x in validator.validate_air(
        {"CONSIGNEE_INFO_COUNTRY": "GB", "CONSIGNEE_INFO_EORI": "VAT#769638661"}))


def test_legacy_air_records_lose_nothing_on_submit():
    """历史 air 里还留着 `*_INFO_TAX_ID`（老票、老暂存）：提交时照样并进 EORI，
    不保留这一步就等于把已经核对过的号静默丢掉。"""
    air = {"MAWB_NO": "999-90273120", "HAWB_NO": "OLD001",
           "SHIPPER_INFO_EORI": "IT13055460961", "SHIPPER_INFO_TAX_ID": USCI,
           "CONSIGNEE_INFO_TAX_ID": "00.280.273/0001-10", "CONSIGNEE_INFO_EORI": ""}
    rec = company_api.order_record("999-90273120", "OLD001", air, None)
    assert rec.get("SHIPPER_INFO_EORI") == "IT13055460961 / " + USCI, rec.get("SHIPPER_INFO_EORI")
    assert rec.get("CONSIGNEE_INFO_EORI") == "00.280.273/0001-10", rec.get("CONSIGNEE_INFO_EORI")
    assert not [k for k in rec if k.endswith("_TAX_ID")], "税号列不该出现在提交体里（公司列面没有）"


def test_desk_js_moves_its_tax_logic_onto_the_eori_cell():
    """审核台里三处认字段的代码都得跟着搬：即时提示（checks）、"混进标签"这类纯提示红旗的
    自动消除（isTaxLabelFlag/taxFlagCleared）。留一处还在认 `_TAX_ID`，制单员就会看到
    一条永远消不掉的红旗，或者改了格子却不消提示。"""
    js = web_src.part("js/desk.js")
    assert "_TAX_ID" not in js, "前端还在引用一列已经不存在的字段"
    assert 'k.endsWith("_EORI")' in js, "即时提示要搬到识别号那一格"
    for fn in ("isTaxLabelFlag", "taxFlagCleared"):
        i = js.index("function " + fn)
        body = js[i:i + 400]
        assert "SHIPPER_INFO_EORI" in body and "CONSIGNEE_INFO_EORI" in body, \
            fn + " 还指着已经不存在的税号列"


def test_schema_no_longer_creates_the_tax_columns():
    """新库不再建这两列；老库**不 DROP**（列与历史值留着，只是不再写）——删列不可逆，
    而留着既不碍事也保得住"当年确实解析出过这个号"的证据。"""
    sql = (Path(validator.__file__).parent / "schema.sql").read_text(encoding="utf-8")
    ddl = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert not re.search(r"^\s+SHIPPER_INFO_TAX_ID\s+VARCHAR", ddl, re.M), "建表语句还在造已删的列"
    assert not re.search(r"^\s+CONSIGNEE_INFO_TAX_ID\s+VARCHAR", ddl, re.M)
    assert "TAX_ID" in sql, "注释里要留下这段历史与老库怎么办，否则下一个人以为从没这两列"
