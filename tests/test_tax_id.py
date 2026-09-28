# -*- coding: utf-8 -*-
"""税号（USCI/统一社会信用代码、CNPJ、VAT NO、TAX ID）专项回归。
起因：DRAFT - HAWB No_ TSN10359645 发货人票面印着 USCI: 911201117706402073，
37 字段里没有税号落点，模型按规则既不放 TEL 也不放 EORI，就直接丢了——
而正向保真只证明 L2 抄的都有出处，看不出票面有的没抄，所以漏号必须反向查。
"""
import json
import re
from pathlib import Path

import hawb2json as h
import to_air
import validator
from fidelity import find_missing_tax, verify_fidelity

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity"
USCI = "911201117706402073"


def test_field_contract_is_40_and_shared():
    """字段清单只有一个真值源：后端契约、prompt 要抽的字段、前端表格行三处必须同集合。"""
    assert len(h.TARGET_KEYS_OUT) == 40 and len(set(h.TARGET_KEYS_OUT)) == 40
    assert set(h.TARGET_KEYS_OUT) == set(vlm_fields()), (
        "prompt 的字段清单与后端契约不一致，模型抽出来的键会对不上列")
    assert set(h.TARGET_KEYS_OUT) == set(desk_keys()), (
        "审核台字段行与后端契约不一致，制单员看不到就是没审核")
    for k in ("SHIPPER_INFO_TAX_ID", "CONSIGNEE_INFO_TAX_ID", "GOODS_HS_CODE"):
        assert k in h.TARGET_KEYS_OUT


def test_hs_code_extracted_from_description():
    """GOODS_HS_CODE：货物描述里 HS CODE(S) 标签后的号码，点号归一、多个逗号连、非纯数字丢弃。"""
    def hs(desc):
        return h._hs_code_text([{"description": desc}])
    assert hs("HSCODE: 8471.30.00") == "84713000"
    assert hs("HS Code: 8471.30, 8517.12") == "847130,851712"
    assert hs("HS CODES: 8471300000, 8517120000") == "8471300000,8517120000"
    assert hs("HS CODE 999999999999999") == "", "超过 12 位不是 HS 码，宁空勿错"
    assert hs("玩具 TOYS") == "", "没标签绝不猜"
    assert hs("HS CODE: AB1234") == "", "非纯数字丢弃"
    assert h._hs_code_text([]) == ""


def vlm_fields():
    import vlm_extract
    return vlm_extract.FIELDS


def desk_keys():
    html = (Path(to_air.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    body = re.search(r"const FIELDS = \[(.*?)\n\];", html, re.S).group(1)
    return re.findall(r'\["([A-Z][A-Z0-9_]+)"', body)


def test_tax_id_is_verified_against_l1_forward():
    """新字段也得进正向回查：填错号/编号要能被保真层抓住。"""
    from fidelity import SHORT_FIELDS
    assert "SHIPPER_INFO_TAX_ID" in SHORT_FIELDS and "CONSIGNEE_INFO_TAX_ID" in SHORT_FIELDS


def test_missing_tax_on_real_ticket_is_flagged():
    """用户报的那张票：票面有 USCI、L2 三十九个字段里一处都没有 → 必须打红旗并说出该填哪列。"""
    c = json.loads((FIX / "draft_hawb_no_tsn10359645.json").read_text(encoding="utf-8"))
    assert find_missing_tax(c["raw"], c["transcript"]) == [("USCI", USCI)]
    fid = verify_fidelity(c["raw"], c["transcript"])
    hits = [x for x in fid["failed"] if x["field"] == "TAX_ID"]
    assert len(hits) == 1 and USCI in hits[0]["reason"], fid["failed"]
    assert "SHIPPER_INFO_TAX_ID" in hits[0]["reason"] and "CONSIGNEE_INFO_TAX_ID" in hits[0]["reason"], \
        "红旗得告诉复核的人填哪一列，否则只等于报个错"
    # 号一旦落进字段，同一张票就该闭嘴（--recheck 与重跑都靠这条判定收敛）
    filled = dict(c["raw"], SHIPPER_INFO_TAX_ID=USCI)
    assert find_missing_tax(filled, c["transcript"]) == []


def test_missing_tax_catches_eori_labels_from_eu_tickets():
    """2026-09-26 八票对台实测坐实：欧票把海关号印成 `EORI IT03268900267` / `EORI NO.: IT03599210261`
    这类形态，标签表里没有 EORI ⇒ 五张全漏、红旗全没响。EORI 进标签后必须抓住；
    号若已被任何字段收走（EORI 列、或还黏在地址行里）则照旧闭嘴，不给复核的人添重复旗。"""
    tr = {"lines": [{"text": t} for t in [
        "OZONE S.R.L", "VIALE DELLE INDUSTRIE 10", "EORI IT03268900267", "TEL 390422470376"]],
        "full_text": ""}
    assert find_missing_tax({}, tr) == [("EORI", "IT03268900267")]
    tr2 = {"lines": [{"text": "EORI NO.: DE4827430"}], "full_text": ""}
    assert find_missing_tax({}, tr2) == [("EORI NO", "DE4827430")]
    assert find_missing_tax({"CONSIGNEE_INFO_EORI": "IT03268900267"}, tr) == [], \
        "号已落 EORI 列就该闭嘴"
    tr3 = {"lines": [{"text": "FORMERVANGEN 5 EORI NO DK28490704"}], "full_text": ""}
    glued = {"CONSIGNEE_INFO_COMP_ADDRESS": "FORMERVANGEN 5 EORI NO DK28490704"}
    assert find_missing_tax(glued, tr3) == [], "号还黏在我方地址行里=已捕获，不重复报"


def test_tax_check_needs_a_label_and_a_real_number():
    """只认带标签的号：裸 18 位数字可能是货值/账号，抓它做红旗就是误报。短号同理不算税号。"""
    tr = {"lines": [{"text": t} for t in [
        "TIANJIN 300385 CN", "TE +862258388999 USCI: " + USCI, "ACCOUNT 640958392012345678",
        "TAX ID: 12345"]], "full_text": ""}
    assert find_missing_tax({}, tr) == [("USCI", USCI)], "带标签的真税号必须被抓到"
    assert find_missing_tax({"X": ""}, {"lines": [{"text": "ACCOUNT 640958392012345678"}]}) == [], \
        "无标签裸数字不该当税号报出来"


def test_l3_pulls_tax_out_of_the_address():
    """模型把 USCI/CNPJ 连号留在地址格里的历史票据：L3 摘出来单独成列，地址里不再挂着。"""
    cases = [
        ("BEI-SI-HUAN MIDDLE ROAD, HAIDIAN DISTRICT, USCI:9111010880211232X4",
         "9111010880211232X4", "BEI-SI-HUAN MIDDLE ROAD, HAIDIAN DISTRICT"),
        ("R THOMAS NILSEN JUNIOR 150, PARTE A, CNPJ: 00280273000218",
         "00280273000218", "R THOMAS NILSEN JUNIOR 150, PARTE A"),
        ("NO.91 AV XAVANTES 416, 410 507", "", "NO.91 AV XAVANTES 416, 410 507"),
    ]
    for addr, want_tax, want_addr in cases:
        addr2, tax = to_air._pull_tax(addr, "", set())
        assert (tax, addr2) == (want_tax, want_addr), f"{addr!r} -> {(tax, addr2)!r}"


def test_l3_does_not_duplicate_tax_already_in_another_column():
    """页底 customs 区的 VAT 常已被 L2 归到 EORI：地址里再出现时只摘号，不重复建列。"""
    addr = "via Galvano Fiamma 18 Italy EU VAT NR: IT13055460961 CONTACT: SIMONA"
    taken = {to_air._alnum0("IT13055460961")}
    addr2, tax = to_air._pull_tax(addr, "", taken)
    assert tax == "", f"与 EORI 重复: {tax!r}"
    assert "VAT NR" not in addr2 and "CONTACT: SIMONA" in addr2, addr2


def test_l2_tax_id_wins_over_address_leftovers():
    """L2 已单列税号时以它为准，地址里重复出现只清列；两者不一致也不能被地址覆盖。"""
    addr = "HAIDIAN DISTRICT, USCI:9111010880211232X4"
    addr2, tax = to_air._pull_tax(addr, h.clean_tax(" 9111010880211232x4 "), set())
    assert tax == "9111010880211232X4" and "USCI" not in addr2


def test_l3_falls_back_to_merged_info_string():
    """模型只把 USCI 留在 L2 合并串里（地址、电话都干净）时，L3 重建合并串前要先把它摘出来。
    BJS00032108 发货人即如此：不兜底则整号随合并串被重写而丢失。"""
    l2 = {k: "" for k in h.TARGET_KEYS_OUT}
    l2.update({"MAWB_NO": "999-30825351", "HAWB_NO": "BJS00032108",
               "SHIPPER_INFO": "BEIJING SHUNYI CO LTD, ROOM 101 BEIJING CN, "
                               "TEL: +861065766886 USCI:91110105562078969J",
               "SHIPPER_INFO_COMP_NAME": "BEIJING SHUNYI CO LTD",
               "SHIPPER_INFO_COMP_ADDRESS": "ROOM 101 BEIJING 101300 CN",
               "SHIPPER_INFO_CITY": "BEIJING", "SHIPPER_INFO_COUNTRY": "CN",
               "SHIPPER_INFO_TEL": "+861065766886"})
    l3 = to_air.to_air(l2)
    assert l3["SHIPPER_INFO_TAX_ID"] == "91110105562078969J", l3["SHIPPER_INFO_TAX_ID"]
    assert l3["SHIPPER_INFO_TEL"] == "+861065766886", l3["SHIPPER_INFO_TEL"]


def test_to_air_end_to_end_keeps_tax_and_clean_phone():
    """票面同一格 TEL 与 USCI 并存：L3 电话只剩号码，税号进自己的列，两者都不丢。"""
    l2 = {k: "" for k in h.TARGET_KEYS_OUT}
    l2.update({"MAWB_NO": "999-30825351", "HAWB_NO": "TSN10359645",
               "SHIPPER_INFO_COMP_NAME": "PARKER HANNIFIN HYDRAULICS (TIANJIN) CO LTD",
               "SHIPPER_INFO_COMP_ADDRESS": "NO 21 HONGYUAN ROAD, TIANJIN 300385 CN, "
                                            "TE +862258388999 USCI: " + USCI,
               "SHIPPER_INFO_CITY": "TIANJIN", "SHIPPER_INFO_COUNTRY": "CN",
               "SHIPPER_INFO_POSTAL": "300385",
               "SHIPPER_INFO_TEL": "+862258388999 USCI: " + USCI,
               "CONSIGNEE_INFO_COMP_NAME": "CALZONI HYDRAULICS ITALY SRL",
               "CONSIGNEE_INFO_COMP_ADDRESS": "VIA CASTELDEBOLE 10, 40069 IT",
               "CONSIGNEE_INFO_CITTY": "ZOLA PREDOSA", "CONSIGNEE_INFO_COUNTRY": "IT"})
    l3 = to_air.to_air(l2)
    assert l3["SHIPPER_INFO_TEL"] == "+862258388999", l3["SHIPPER_INFO_TEL"]
    assert l3["SHIPPER_INFO_TAX_ID"] == USCI, l3["SHIPPER_INFO_TAX_ID"]
    assert USCI not in l3["SHIPPER_INFO_COMP_ADDRESS"], l3["SHIPPER_INFO_COMP_ADDRESS"]
    assert USCI not in l3["SHIPPER_INFO"], l3["SHIPPER_INFO"]


def test_validator_tax_shape():
    base = {"SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_TAX_ID": USCI,
            "SHIPPER_INFO_TEL": "+862258388999"}
    assert not [w for w in validator.validate_raw(dict(base)) if "TAX_ID" in w], \
        "18 位 USCI 是合法形态，别乱报"
    short = dict(base, SHIPPER_INFO_TAX_ID="911201117706402")
    assert any("TAX_ID" in w for w in validator.validate_raw(short)), "被截断的 USCI 必须报"
    dup = dict(base, SHIPPER_INFO_TEL=USCI)
    assert any("与电话同值" in w for w in validator.validate_raw(dup)), "税号串进电话栏要报出来"
    br = {"CONSIGNEE_INFO_COUNTRY": "BR", "CONSIGNEE_INFO_TAX_ID": "00.280.273/0001-37"}
    assert not [w for w in validator.validate_raw(dict(br)) if "TAX_ID" in w], \
        "CNPJ 带点斜横线是号码本身，不该按长度误判"
    junk = dict(base, SHIPPER_INFO_TAX_ID="ABC123")
    assert any("TAX_ID" in w for w in validator.validate_raw(junk))
    short_br = dict(br, CONSIGNEE_INFO_TAX_ID="1234567")   # 非 CN 也要有长度下限
    assert any("TAX_ID" in w for w in validator.validate_raw(short_br)), \
        "过短税号只在 CN 分支判长度等于没判"


def test_l3_strips_ticket_label_off_the_tax_number():
    """票面把 'VAT#769638661' 整格印出来，模型照抄就带着标签：L3 摘标签只留号码，
    L2 保持照抄但要冒红旗，让复核的人知道这一列被加工过。"""
    assert h.clean_tax("VAT#769638661") == "769638661"
    assert h.clean_tax("TAX ID 123456789") == "123456789"
    assert h.clean_tax("PL7010468168") == "PL7010468168", "两位国家码开头的是号码本身"
    assert h.clean_tax("00.280.273/0001-37") == "00.280.273/0001-37"
    l2 = {k: "" for k in h.TARGET_KEYS_OUT}
    l2.update({"HAWB_NO": "TAO-7268550",
               "CONSIGNEE_INFO_COMP_NAME": "BIG METAL LTD",
               "CONSIGNEE_INFO_COMP_ADDRESS": "OCCUPATIONAL ROAD 1, LONDON SE17 3BE GB",
               "CONSIGNEE_INFO_CITTY": "LONDON", "CONSIGNEE_INFO_COUNTRY": "GB",
               "CONSIGNEE_INFO_TAX_ID": "VAT#769638661"})
    assert to_air.to_air(l2)["CONSIGNEE_INFO_TAX_ID"] == "769638661"
    assert any("混进了标签" in w for w in validator.validate_raw(l2))


def test_schema_declares_tax_columns():
    """建表语句里没有这两列的话，db_writer 写库直接 Unknown column。"""
    sql = (Path(validator.__file__).parent / "schema.sql").read_text(encoding="utf-8")
    ddl = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert re.search(r"^\s+SHIPPER_INFO_TAX_ID\s+VARCHAR", ddl, re.M)
    assert re.search(r"^\s+CONSIGNEE_INFO_TAX_ID\s+VARCHAR", ddl, re.M)
    assert "ADD COLUMN SHIPPER_INFO_TAX_ID" in sql, "老库缺升级语句的话写库会直接报错"
