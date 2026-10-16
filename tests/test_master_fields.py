# -*- coding: utf-8 -*-
"""主单字段面与 L1 文本拼装（2026-09-28 用户定案：主单是主单、分单是分单）。

主单解析的目标形状就是公司 `POST /api/v1/j9/mawb2/` 要的 `AMS_RECORD`，所以这里一律用
**公司列名原样**（通知人国家是 `NOTIFY_INFO_COUNTRY`——IT 在 2026-09-29 把库里多一个 E 的旧拼写
`NOTIFYE_INFO_COUNTRY` 改掉了；收货人城市是单 T 的 `CONSIGNEE_INFO_CITY`），
不套我方分单 40 字段契约（分单侧城市列是库表原拼写 CITTY，那是分单表的事）。
"""
from master_fields import (AMS_SERVER_COLS, MASTER_COLS, MASTER_FIELDS, MASTER_GROUP_LABELS,
                           MASTER_LIMIT50, MASTER_META_COLS, OUTER_REF_COLS,
                           build_transcript, clean_ams, norm_mawb_hyphen, original_lines, submit_body)

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
        "NOTIFY_INFO_COMP_NAME": "MOHAMMED SALEEM", "NOTIFY_INFO_COUNTRY": "SA",
    },
}


def test_groups_order_shipper_consignee_notify_last():
    """分组照分单那套走：基础信息（单号/货物）+ 发货人 + 收货人，通知人单列一段放在最后。
    通知人从前并进基础信息（2026-09-28 定案「不单列」），2026-10-15 业主改成按
    发货人 → 收货人 → 通知人 的顺序读——推翻旧决定时把旧理由留在 master_fields 的注释里。
    JOB_ID 也是主单多出来的，仍只读展示在基础信息里。"""
    groups = [g for _c, _l, g in MASTER_FIELDS]
    assert set(groups) == {"base", "shipper", "consignee", "notify"}, set(groups)
    assert groups[0] == "base" and groups[-1] == "notify", "基础信息在最前，通知人在最后"
    notify_at = [i for i, g in enumerate(groups) if g == "notify"]
    cons_at = [i for i, g in enumerate(groups) if g == "consignee"]
    assert max(cons_at) < min(notify_at), "通知人要排在收货人后面（发货人 → 收货人 → 通知人）"
    base = [c for c, _l, g in MASTER_FIELDS if g == "base"]
    assert {"MAWB_NO", "GOODS_INFO_HSCODE", "SLAC"} <= set(base)
    assert not any(c.startswith("NOTIFY") for c in base), "通知人不再并进基础信息"
    assert MASTER_GROUP_LABELS["base"] == "基础信息"
    assert MASTER_GROUP_LABELS["notify"] == "通知人 NOTIFY"
    # JOB_ID 不是可提交列，但要在基础信息里看得见（只读），来源与提交口径都要标清
    assert "JOB_ID" not in MASTER_COLS
    assert [(k, g) for k, _l, g in MASTER_META_COLS] == [("JOB_ID", "base")]


def test_field_table_is_the_company_master_shape():
    cols = [c for c, _lab, _grp in MASTER_FIELDS]
    assert len(cols) == 36, f"公司主单表是 36 列，实际 {len(cols)}"
    assert cols.count("MAWB_NO") == 1 and cols.count("SLAC") == 1 and cols.count("GOODS_INFO_HSCODE") == 1
    assert "NOTIFY_INFO_COUNTRY" in cols, "通知人国家以接口现名为准（旧拼写多一个 E 已被 IT 改掉）"
    assert not any("NOTIFYE" in c for c in cols), "旧名一旦留在列面里，提交体就带不上公司那一列 → 整表写回会抹成 NULL"
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
        "NOTIFY_INFO_CITY", "NOTIFY_INFO_COUNTRY", "NOTIFY_INFO_STATE"}, \
        "这九列超 50 字符公司直接 400，红旗要提前报出来"

def test_submit_body_is_the_only_master_column_surface():
    """36 列这条规则从前有两份：master_fields.submit_body（只有测试在调）和 company_api 里的
    同一句字典推导。改列面时漏改一份，就是往公司整表写回时少写/多写一列。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "company_api.py").read_text(encoding="utf-8")
    assert "mf.submit_body(" in src, "主单提交体要经 submit_body 组列面"
    assert "for col in mf.MASTER_COLS}" not in src, "company_api 里又长出一份列面抄本"


def test_original_lines_drops_only_what_we_sent_back():
    """第二次检索同一条主单时，读回的资料里会长出「本机上次写回公司的那些列」——主单没有票面原件，
    资料文本就是唯一的原始凭据，让它冒充原始资料，模型就是在抄自己上次的答案，漏取/保真两道核查
    当场失去独立性（2026-10-15 用户在 074-51188292 上实测到）。
    IT 2026-10-16 定的口径是**不许进资料**，不是"进资料但标一下"：所以这里剔掉。
    判据仍要两条同时成立才剔：这一行在首次钉下的基线里没有 + 值等于本机发出去的那一格
    （宁可少剔：误剔等于把公司真值从模型眼前藏掉，还会冒一条假漏取）。"""
    prev = [{"i": 1, "text": "MASTER_NO: 074-51188292"},
            {"i": 2, "text": "SHIPPER_INFO: DSV AIR & SEA CO., LTD."}]
    new = prev + [{"i": 3, "text": "MAWB_NO: 074-51188292"},
                  {"i": 4, "text": "GOODS_INFO_HSCODE: 702000"},
                  {"i": 5, "text": "CONSIGNEE_INFO_TEL: +49 69 6952060"},
                  {"i": 6, "text": "SHIPPER_INFO_TEL: 0532-80901573"},
                  {"i": 7, "text": "CONSIGNEE_INFO_EORI: DE999999999"}]
    sent = {"MAWB_NO": "074-51188292", "GOODS_INFO_HSCODE": "702000",
            "CONSIGNEE_INFO_TEL": "+49  69 6952060",     # 空白差一个也算同一格（出行时就是这么归一的）
            "SHIPPER_INFO_TEL": "0532-00000000"}         # 我们发的是另一个号码：公司改过了，是真资料
    base = ["MASTER_NO", "SHIPPER_INFO"]
    kept, gone, unknown = original_lines(new, base, sent)
    assert [x["label"] for x in gone] == ["MAWB_NO", "GOODS_INFO_HSCODE", "CONSIGNEE_INFO_TEL"], gone
    assert [x["text"] for x in gone][0] == "MAWB_NO: 074-51188292", "剔掉的行要原样记账，回头查得着"
    assert [x["text"].split(":")[0] for x in kept] == \
        ["MASTER_NO", "SHIPPER_INFO", "SHIPPER_INFO_TEL", "CONSIGNEE_INFO_EORI"], kept
    assert [x["i"] for x in kept] == [1, 2, 3, 4], "剔完不重排行号，前端说的第几行与点字段名的定位就指错行了"
    assert unknown == [], "有基线可比，就没有「分不清」这一档"
    kept2, gone2, unknown2 = original_lines(new, [], sent)
    assert gone2 == [] and len(kept2) == len(new), "连可比对象都没有：一行都不许剔"
    assert [x["i"] for x in kept2] == list(range(1, len(new) + 1)), "没剔行也要给连续行号"
    assert [x["label"] for x in unknown2] == ["MAWB_NO", "GOODS_INFO_HSCODE", "CONSIGNEE_INFO_TEL"], unknown2
    assert original_lines(new, base, {})[1] == [], "本机什么都没发过，就没有回填这回事"
    assert original_lines(new, base + ["MAWB_NO", "GOODS_INFO_HSCODE", "CONSIGNEE_INFO_TEL"],
                          sent)[1] == [], "基线里就有这几行 = 公司先录的，值相同也不许剔"
