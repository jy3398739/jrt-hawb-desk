# -*- coding: utf-8 -*-
"""company_api live 模式（j9 AMS 接口）回归：全部 mock HTTP，绝不真调公司接口。
判据来自 IT 的《AMS录入接口调用说明》：hawb2 整表写回（漏列=写 NULL）、SEND_STATUS 闸门、
ORGIN_NAME/CONSIGNEE_INFO_CITY 是公司列名（我们契约的 ORIGIN_NAME/CITTY 要换名）、
读接口 SHIPPER_INFO 是 repr 列表串。"""
import json

import company_api
import config


def _patch_post(monkey_rows, monkey_master=None, sent=None):
    """把 _j9_post 换成假实现：记下每次调用，按 path 回数据。返回恢复函数。"""
    real = company_api._j9_post

    def fake(path, body, key):
        sent.append({"path": path, "body": body, "key": key})
        if path.endswith("/hawb"):
            return {"code": 0, "count": len(monkey_rows), "data": monkey_rows}
        if path.endswith("/mawb/"):
            return {"code": 0, "count": 1, "data": monkey_master or []}
        if path.endswith("/hawb2"):
            return {"code": 0, "success": True, "action": "update", "rows": 1}
        raise AssertionError("未知路径 " + path)

    company_api._j9_post = fake

    def restore():
        company_api._j9_post = real
    return restore


ROW = {
    "HAWB_ID": 448, "MAWB_NO": "020-18134281", "HAWB_NO": "NOAIRWAW050582",
    "SHIPPER_INFO": "['DONGGUAN OREN, ELECTRONIC HARDWARE CO., LTD. OF']",
    "CONSIGNEE_INFO": "['BKT ELEKTRONIK SP. Z O.O.']",
    "ORGIN_NAME": "BEIJING", "TO1": "WAW", "TO2": None, "TO3": None, "DEST_NAME": "WARSAW",
    "GOODS_INFO": "['1.35CBM']", "PIECES": 12, "WEIGHT": 170.0, "SLAC": 0,
    "SEND_STATUS": 0,
    "SHIPPER_INFO_COMP_NAME": "DONGGUAN OREN", "SHIPPER_INFO_COMP_ADDRESS": "A2 BLDG",
    "SHIPPER_INFO_CITY": "DONGGUAN", "SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_STATE": "GUANGDONG",
    "SHIPPER_INFO_POSTAL": "", "SHIPPER_INFO_TEL": "18575297929",
    "CONSIGNEE_INFO_COMP_NAME": "BKT ELEKTRONIK", "CONSIGNEE_INFO_COMP_ADDRESS": "UL WIEJSKA 6",
    "CONSIGNEE_INFO_CITY": "LISI OGON", "CONSIGNEE_INFO_COUNTRY": "PL", "CONSIGNEE_INFO_STATE": "",
    "CONSIGNEE_INFO_POSTAL": "86-065", "CONSIGNEE_INFO_TEL": "+48 785 557563",
}

AIR_REVIEWED = {
    "MAWB_NO": "020-18134281", "HAWB_NO": "NOAIRWAW050582",
    "ORIGIN_NAME": "BJS", "TO1": "WAW", "DEST_NAME": "WAW",
    "GOODS_INFO": "CAR PARTS", "GOODS_HS_CODE": "87089999",
    "PIECES": 12, "WEIGHT": 170.0, "SLAC": 12,
    "SHIPPER_INFO_COMP_NAME": "DONGGUAN OREN ELECTRONIC HARDWARE CO LTD",
    "CONSIGNEE_INFO_CITTY": "WARSAW", "CONSIGNEE_INFO_COUNTRY": "PL",
}


def test_search_mawb_live_reads_j9_and_joins_local_stem():
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    sent = []
    restore = _patch_post([ROW], monkey_master=[{"MASTER_NO": "020-18134281", "AMS_RECORD": None}], sent=sent)
    real_lookup = company_api.store.lookup_stem
    company_api.store.lookup_stem = lambda m, h: "T1"
    try:
        out = company_api.search_mawb("020-18134281")
    finally:
        company_api.store.lookup_stem = real_lookup
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old
    assert out["mode"] == "live"
    assert out["mawb_order"]["AMS_RECORD"] is None, "主单原生资料要透传（录入员页通用渲染直接吃）"
    row = out["hawb_orders"][0]
    assert row["hawb"] == "NOAIRWAW050582" and row["stem"] == "T1", "本机归档 stem 要联结进去供 /source"
    assert any(p["path"].endswith("/j9/hawb") and p["key"] == "kh" for p in sent), "分单行用分单 key"
    assert any(p["path"].endswith("/j9/mawb/") and p["key"] == "km" for p in sent), "主单资料用主单 key"
    assert "parsed" not in out, "主单解析改走 master_pipeline，检索只管把公司数据原样带回"


def test_submit_live_reads_then_sends_full_merged_row():
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    sent = []
    restore = _patch_post([dict(ROW)], sent=sent)
    try:
        out = company_api.submit_order({"mawb": "020-18134281", "hawb": "NOAIRWAW050582",
                                        "stem": "T1", "air": AIR_REVIEWED})
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old
    assert out["accepted"] is True and out["action"] == "update"
    assert len(sent) == 2, "必须先读后写：一次 hawb 读、一次 hawb2 写"
    rec = sent[1]["body"]["HAWB_RECORD"]
    assert rec["ORGIN_NAME"] == "BJS", "我们契约的 ORIGIN_NAME 要换名成公司列 ORGIN_NAME"
    assert rec["CONSIGNEE_INFO_CITY"] == "WARSAW", "CITTY 要换名成公司列 CITY"
    assert rec["SHIPPER_INFO"] == ROW["SHIPPER_INFO"], "整表写回：读到的列就算我们没改也要原样带上"
    assert rec["SHIPPER_INFO_COMP_NAME"] == AIR_REVIEWED["SHIPPER_INFO_COMP_NAME"], "复核值覆盖读到的旧值"
    assert "GOODS_HS_CODE" not in rec, "分单表没有 HS 列，塞进去会当未识别字段"
    assert rec["HAWB_ID"] == 448 and rec["SEND_STATUS"] == 0, "读到的原样键要带上（服务端忽略状态/时间/ID）"


def test_submit_live_refuses_locked_row_without_writing():
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    locked = dict(ROW, SEND_STATUS=1)
    sent = []
    restore = _patch_post([locked], sent=sent)
    try:
        try:
            company_api.submit_order({"mawb": "020-18134281", "hawb": "NOAIRWAW050582",
                                      "stem": "T1", "air": AIR_REVIEWED})
            raise AssertionError("SEND_STATUS=1 的行居然放行了")
        except company_api.CompanyLocked as e:
            assert "SEND_STATUS" in str(e) or "1" in str(e)
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old
    assert len(sent) == 1, "闸门拦下时绝不能发出 hawb2 写请求"
