# -*- coding: utf-8 -*-
"""主单字段面与 L1 文本拼装（2026-09-28 用户定案：主单是主单、分单是分单）。

主单解析的目标形状就是公司 `POST /api/v1/j9/mawb2/` 要的 `AMS_RECORD`，所以这里一律用
**公司列名原样**（含 NOTIFYE_INFO_COUNTRY 多出来的那个 E、CONSIGNEE_INFO_CITY 单 T），
不套我方分单 40 字段契约（分单侧城市列是库表原拼写 CITTY，那是分单表的事）。
"""
from master_fields import (AMS_SERVER_COLS, MASTER_FIELDS, MASTER_LIMIT50,
                           OUTER_REF_COLS, build_transcript, clean_ams,
                           norm_mawb_hyphen, submit_body)

# 公司真返回（176-62400004 精简 + 一条 NOTIFY 块）
ORDER = {
    "JOB_ID": 40366281, "MASTER_NO": "176-62400004",
    "SHIPPER_INFO": "METSO (TIANJIN) INVESTMENT CO., LTD. NO. 4 WAREHOUSE TEL NO.: +86 22 25322285 CHINA",
    "CONSIGNEE_INFO": "MAADEN GOLD AND BASE METALS CO NATIONAL SAUDI ARABIA EORI: SA4827430",
    "NOTIFY_INFO": "    ",
    "GOODS_NAME": "PISTON ROD HS CODE:8412909090",
    "GOODS_DESC": "***VAT NUMBER: 300057757900003",
    "AMS_RECORD": {
        "HMY_ID": 12, "MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901", "SLAC": 10,
        "CREATE_TIME": "2026-09-20 09:12:00", "SEND_STATUS": 0,
        "SHIPPER_INFO_COMP_NAME": "METSO (TIANJIN) INVESTMENT CO., LTD.",
        "SHIPPER_INFO_CITY": "SHANGHAI", "SHIPPER_INFO_COUNTRY": "CN",
        "CONSIGNEE_INFO_COMP_NAME": "MAADEN GOLD AND BASE METALS CO NATIONAL",
        "CONSIGNEE_INFO_CITY": "KINGDOM", "CONSIGNEE_INFO_EORI": " sa4827430 ",
        "NOTIFY_INFO_COMP_NAME": "MOHAMMED SALEEM", "NOTIFYE_INFO_COUNTRY": "SA",
    },
}


def test_field_table_is_the_company_master_shape():
    cols = [c for c, _lab, _grp in MASTER_FIELDS]
    assert len(cols) == 36, f"公司主单表是 36 列，实际 {len(cols)}"
    assert cols.count("MAWB_NO") == 1 and cols.count("SLAC") == 1 and cols.count("GOODS_INFO_HSCODE") == 1
    assert "NOTIFYE_INFO_COUNTRY" in cols, "通知人国家列名多个 E 是库表原样，不是笔误"
    assert "CONSIGNEE_INFO_CITY" in cols and "CONSIGNEE_INFO_CITTY" not in cols, \
        "单 T 是公司主单表写法；双 T CITTY 属于分单表契约，别串"
    for absent in ("PIECES", "WEIGHT", "ORIGIN_NAME", "DEST_NAME", "TO1", "HAWB_NO",
                   "GOODS_INFO", "SHIPPER_INFO", "CREATE_TIME", "SEND_STATUS", "HMY_ID"):
        assert absent not in cols, f"{absent} 不是主单可提交列（外层只读参考或服务端维护）"
    assert not any(c.endswith("_TAX_ID") for c in cols), \
        "公司主单表没有税号列（分单表也没有）：票面 USCI/CNPJ 那类号在主单侧没有落点，不能凭空造列"
    for grp in ("shipper", "consignee", "notify"):
        sub = [c for c, _l, g in MASTER_FIELDS if g == grp]
        assert len(sub) == 11, f"{grp} 组应为 11 列，实际 {len(sub)}"
        assert any(c.endswith("_COMP_NAME") for c in sub) and any(c.endswith("_EMAIL") for c in sub)


def test_outer_reference_and_server_columns_are_declared():
    assert set(OUTER_REF_COLS) == {"JOB_ID", "MASTER_NO", "SHIPPER_INFO", "CONSIGNEE_INFO",
                                   "NOTIFY_INFO", "GOODS_NAME", "GOODS_DESC"}, \
        "外层 7 字段是只读参考：主单表没有货名列，GOODS_NAME/GOODS_DESC 不进提交体"
    assert set(AMS_SERVER_COLS) == {"HMY_ID", "CREATE_TIME", "SEND_STATUS"}


def test_transcript_keeps_company_text_verbatim_and_skips_server_columns():
    tr = build_transcript(ORDER)
    lines = [x["text"] for x in tr["lines"]]
    assert any(l.startswith("MASTER_NO: ") and "176-62400004" in l for l in lines)
    assert any(l.startswith("CONSIGNEE_INFO_EORI:") and "sa4827430" in l for l in lines), \
        "L1 要逐字保留公司写法（大小写/空格都不动），归一是清洗阶段的事"
    assert not any(l.startswith(("HMY_ID", "SEND_STATUS", "CREATE_TIME")) for l in lines), \
        "服务端维护的三列不进 L1：模型没有对应字段，给了只会诱发它编"
    assert not any("NOTIFY_INFO:    " in l for l in lines), "空资料块不出行"
    assert sum(1 for l in lines if l.startswith("GOODS_NAME: ")) == 1
    assert tr["full_text"] == "\n".join(lines)


def test_transcript_orders_ams_columns_before_the_blobs():
    lines = [x["text"] for x in build_transcript(ORDER)["lines"]]
    ams_at = [i for i, l in enumerate(lines) if l.startswith("SHIPPER_INFO_COMP_NAME: ")]
    blob_at = [i for i, l in enumerate(lines) if l.startswith("SHIPPER_INFO: ")]
    assert ams_at and blob_at and ams_at[0] < blob_at[0], \
        "已录入的列在前、拼接资料块在后：模型先看到结构化真值，才不会被黏行文本带偏"


def test_clean_types_slac_and_normalizes_eori():
    ams = clean_ams(dict(ORDER["AMS_RECORD"], SLAC="12",
                         SHIPPER_INFO_COMP_NAME="  METSO   (TIANJIN)  ",
                         CONSIGNEE_INFO_EMAIL=""))
    assert ams["CONSIGNEE_INFO_EORI"] == "SA4827430", "EORI 去空白转大写（公司按 EORI 形态校验）"
    assert ams["SLAC"] == 12, "SLAC 是整数"
    assert ams["SHIPPER_INFO_COMP_NAME"] == "METSO (TIANJIN)", "连续空白归一、首尾去掉"
    assert ams["CONSIGNEE_INFO_EMAIL"] is None, "空串→None，公司侧写 NULL"
    assert "HMY_ID" not in ams and "SEND_STATUS" not in ams and "CREATE_TIME" not in ams


def test_norm_mawb_hyphen_matches_company_hard_format():
    assert norm_mawb_hyphen("17662400004") == "176-62400004", "公司硬性要求 3位-横杠-8位"
    assert norm_mawb_hyphen(" 176 62400004 ") == "176-62400004"
    assert norm_mawb_hyphen("176-62400004") == "176-62400004"
    assert norm_mawb_hyphen("176-6240004") == "176-6240004", "位数不对的原样留着，交给红旗报，不硬凑"


def test_submit_body_carries_exactly_the_36_columns():
    body = submit_body(clean_ams(ORDER["AMS_RECORD"]))
    assert set(body) == {c for c, _l, _g in MASTER_FIELDS}
    assert body["MAWB_NO"] == "176-62400004"
    assert body["GOODS_INFO_HSCODE"] == "841290901"
    assert body["NOTIFY_INFO_COMP_ADDRESS"] is None, "整表写回：没值的列就是 NULL，明写在提交体里"
    for k in AMS_SERVER_COLS:
        assert k not in body, "服务端维护列不发"


def test_clean_strips_hs_labels_to_digits_only():
    """真跑撞到的：模型把「HS CODE:8526109 HS CODE:8412210」整串抄进 HS 列（保真回查还过了，
    因为它确实是原文逐字）。公司要的是号码本身，标签得摘，多个码用逗号连。"""
    ams = clean_ams({"GOODS_INFO_HSCODE": "HS CODE:8526109 HS CODE:8412210"})
    assert ams["GOODS_INFO_HSCODE"] == "8526109,8412210"
    assert clean_ams({"GOODS_INFO_HSCODE": "8526109"})["GOODS_INFO_HSCODE"] == "8526109"
    assert clean_ams({"GOODS_INFO_HSCODE": "无"})["GOODS_INFO_HSCODE"] == "无", \
        "一个数字都没有就原样留着，交给红旗报，别硬猜成空"


def test_limit50_list_is_the_company_400_lines():
    assert set(MASTER_LIMIT50) == {
        "SHIPPER_INFO_COUNTRY", "SHIPPER_INFO_STATE", "SHIPPER_INFO_POSTAL",
        "CONSIGNEE_INFO_CITY", "CONSIGNEE_INFO_COUNTRY", "CONSIGNEE_INFO_STATE",
        "NOTIFY_INFO_CITY", "NOTIFYE_INFO_COUNTRY", "NOTIFY_INFO_STATE"}, \
        "这九列超 50 字符公司直接 400，红旗要提前报出来"
