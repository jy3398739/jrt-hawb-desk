# -*- coding: utf-8 -*-
"""主单资料 → 我方字段视图（只读核对用）。用户口径 2026-09-26：
主单检索结果要能进「票面预览 + 核对」，但这一路不接提交；拆不出的绝不猜。"""
from master_parse import parse_master

# 公司真记录（176-62400004 精简）：AMS_RECORD 已录入
AMS = {
    "HMY_ID": 12, "MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901", "SLAC": 10,
    "CREATE_TIME": "2026-09-20 09:12:00", "SEND_STATUS": 0,
    "SHIPPER_INFO_COMP_NAME": "METSO (TIANJIN) INVESTMENT CO., LTD.",
    "SHIPPER_INFO_CITY": "SHANGHAI", "SHIPPER_INFO_COUNTRY": "CN",
    "CONSIGNEE_INFO_COMP_NAME": "MAADEN GOLD AND BASE METALS CO NATIONAL",
    "CONSIGNEE_INFO_CITY": "KINGDOM", "CONSIGNEE_INFO_POSTAL": "44441-8114***",
    "NOTIFY_INFO_COMP_NAME": "MOHAMMED SALEEM", "NOTIFYE_INFO_COUNTRY": "SA",
}
OUTER = {
    "MASTER_NO": "176-62400004",
    "GOODS_NAME": "PISTON ROD HS CODE:8412909090",
    "SHIPPER_INFO": "METSO (TIANJIN) INVESTMENT CO., LTD. NO. 4 WAREHOUSE TEL NO.: +86 22 25322285 CHINA",
    "CONSIGNEE_INFO": "MAADEN GOLD AND BASE METALS CO NATIONAL SAUDI ARABIA EORI: SA4827430",
}


def test_maps_ams_record_with_our_column_spellings():
    out = parse_master(dict(OUTER, AMS_RECORD=AMS))
    f = out["fields"]
    assert f["GOODS_HS_CODE"] == "841290901", "公司列 GOODS_INFO_HSCODE 要换成我方 GOODS_HS_CODE"
    assert f["CONSIGNEE_INFO_CITTY"] == "KINGDOM", "公司 CITY 要落到我方库表原拼写 CITTY"
    assert "CONSIGNEE_INFO_CITY" not in f
    assert f["SHIPPER_INFO_CITY"] == "SHANGHAI"
    assert out["source"] == "ams_record"


def test_notify_block_is_dropped_not_mapped():
    """主单表有 NOTIFY 九列，我方契约没有——不能塞进 fields 让核对区冒出不认识的格子。"""
    f = parse_master(dict(OUTER, AMS_RECORD=AMS))["fields"]
    assert not any(k.startswith("NOTIFY") for k in f), f
    assert "HMY_ID" not in f


def test_goods_info_from_outer_goods_name_and_hs_tail_stripped():
    out = parse_master({"GOODS_NAME": "PISTON ROD HS CODE:8412909090", "AMS_RECORD": None})
    assert out["fields"]["GOODS_INFO"] == "PISTON ROD"
    assert out["fields"]["GOODS_HS_CODE"] == "8412909090", "AMS_RECORD 为空时 HS 从货名尾巴拿"
    assert out["source"] == "blobs"


def test_blobs_only_take_labeled_values_and_flag_the_rest():
    mo = {"GOODS_NAME": "PISTON ROD", "SHIPPER_INFO": OUTER["SHIPPER_INFO"],
          "CONSIGNEE_INFO": OUTER["CONSIGNEE_INFO"], "AMS_RECORD": None}
    out = parse_master(mo)
    f = out["fields"]
    assert f["SHIPPER_INFO_TEL"].startswith("+86 22"), "带 TEL NO.: 标签的电话要拿到"
    assert f["SHIPPER_INFO_COUNTRY"] == "CN", "资料块里整词 CHINA → CN"
    assert f["CONSIGNEE_INFO_COUNTRY"] == "SA"
    assert "MAADEN GOLD" not in f.get("CONSIGNEE_INFO_COMP_ADDRESS", ""), "不猜地址边界"
    notes = "\n".join(out["notes"])
    assert "CONSIGNEE_INFO_COMP_ADDRESS" in notes and "SHIPPER_INFO_POSTAL" in notes, \
        "拆不出的列必须逐条进人工清单，而不是留个空格子让人以为公司就是这么填的"


def test_never_invents_master_missing_columns():
    """件数/重量/始发/目的/航路/分单号：主单表压根没有，只能进清单，不许凭空出现。"""
    out = parse_master(dict(OUTER, AMS_RECORD=AMS))
    for col in ("PIECES", "WEIGHT", "ORIGIN_NAME", "DEST_NAME", "TO1", "HAWB_NO"):
        assert col not in out["fields"], col
        assert any(col in n for n in out["notes"]), f"{col} 要出现在人工清单里"


def test_eori_in_blob_goes_to_eori_not_tax_id():
    mo = {"CONSIGNEE_INFO": "BKT ELEKTRONIK SP. Z O.O. EORI:PL554289446200000 TEL:48 785 557563",
          "AMS_RECORD": None}
    f = parse_master(mo)["fields"]
    assert f["CONSIGNEE_INFO_EORI"] == "PL554289446200000"
    assert "CONSIGNEE_INFO_TAX_ID" not in f, "EORI 归 EORI，别和税号混一格（票面侧同样口径）"
