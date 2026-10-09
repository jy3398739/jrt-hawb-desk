# -*- coding: utf-8 -*-
"""company_api live 模式（j9 AMS 接口）回归：全部 mock HTTP，绝不真调公司接口。
判据来自 IT 的《AMS录入接口调用说明》：hawb2 整表写回（漏列=写 NULL）、SEND_STATUS 闸门、
ORGIN_NAME/CONSIGNEE_INFO_CITY 是公司列名（我们契约的 ORIGIN_NAME/CITTY 要换名）、
读接口 SHIPPER_INFO 是 repr 列表串。"""
import json
import shutil
import tempfile
from pathlib import Path

import company_api
import config
import j9_fake


def _patch_post(monkey_rows, monkey_master=None, sent=None):
    """把 _j9_post 换成假实现：记下每次调用，按 path 回数据。返回恢复函数。"""
    real = company_api._j9_post

    def fake(path, body, key):
        sent.append({"path": path, "body": body, "key": key})
        if path.endswith("/hawb"):
            return {"code": 0, "count": len(monkey_rows), "data": monkey_rows}
        if path.endswith("/mawb/"):
            return {"code": 0, "count": 1, "data": monkey_master or []}
        if path.endswith("/mawb2/"):
            return {"code": 0, "success": True, "action": "update", "rows": 1, "SEND_STATUS": 0}
        if path.endswith("/hawb2"):
            return {"code": 0, "success": True, "action": "update", "rows": 1, "SEND_STATUS": 0}
        if path.endswith("/mawb3/"):
            return {"code": 0, "success": True, "action": "update", "rows": 1, "SEND_STATUS": 1}
        if path.endswith("/hawb3"):
            return {"code": 0, "success": True, "action": "update", "rows": 1, "SEND_STATUS": 1}
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
    assert len(sent) == 2, "必须先读后写：一次 hawb 读、一次写"
    # 2026-10-09 IT 文档改口：2=暂存（恒落 SEND_STATUS=0，外围程序不取），3=提交发送（落 1）。
    # 提交若还打在 hawb2，我们这边记"提交成功"、公司库里全是 0，票永远不会被发出去。
    assert sent[1]["path"].endswith("/hawb3"), f"提交必须走 hawb3（提交发送）：{sent[1]['path']}"
    rec = sent[1]["body"]["HAWB_RECORD"]
    assert rec["ORGIN_NAME"] == "BJS", "我们契约的 ORIGIN_NAME 要换名成公司列 ORGIN_NAME"
    assert rec["CONSIGNEE_INFO_CITY"] == "WARSAW", "CITTY 要换名成公司列 CITY"
    assert rec["SHIPPER_INFO"] == ROW["SHIPPER_INFO"], "整表写回：读到的列就算我们没改也要原样带上"
    assert rec["SHIPPER_INFO_COMP_NAME"] == AIR_REVIEWED["SHIPPER_INFO_COMP_NAME"], "复核值覆盖读到的旧值"
    assert "GOODS_HS_CODE" not in rec, "分单表没有 HS 列，塞进去会当未识别字段"
    assert rec["HAWB_ID"] == 448 and rec["SEND_STATUS"] == 0, "读到的原样键要带上（服务端忽略状态/时间/ID）"


def test_master_submit_live_goes_to_the_send_endpoint():
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    master = [{"MASTER_NO": "176-62400004", "AMS_RECORD": {"MAWB_NO": "176-62400004", "SEND_STATUS": 0}}]
    sent = []
    restore = _patch_post([], monkey_master=master, sent=sent)
    try:
        company_api.submit_master({"mawb": "176-62400004", "ams": {"MAWB_NO": "176-62400004"}})
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old
    assert sent[-1]["path"].endswith("/mawb3/"), f"主单提交必须走 mawb3：{sent[-1]['path']}"


def test_submit_refuses_a_row_the_platform_already_sent():
    """文档的闸门是"仅 0 可改"：1=已提交发送、2=外围已成功发送，两个都 400。

    我们从前把 2 当"已改待重发"放行——那是把已成功发送的记录再覆盖一遍，
    而且发出去也白发（服务端必拒），所以本地就该拦下并说清是哪一档。"""
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    for st, word in ((1, "已提交"), (2, "已发送")):
        sent = []
        restore = _patch_post([dict(ROW, SEND_STATUS=st)], sent=sent)
        try:
            try:
                company_api.submit_order({"mawb": "020-18134281", "hawb": "NOAIRWAW050582",
                                          "stem": "T1", "air": AIR_REVIEWED})
                raise AssertionError(f"SEND_STATUS={st} 的行居然放行了")
            except company_api.CompanyLocked as e:
                assert str(st) in str(e), f"要说清是哪一档：{e}"
                assert word in str(e), f"措辞要认得：{e}"
        finally:
            restore()
            assert len(sent) == 1, f"闸门拦下时一个写请求都不发（SEND_STATUS={st}）"
    config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old


def test_master_submit_live_carries_the_renamed_notify_country():
    """2026-09-29 IT 改了列名：NOTIFYE_INFO_COUNTRY（多一个 E）→ NOTIFY_INFO_COUNTRY，旧名从此是
    "未识别字段"。列面停在旧名会双向吃亏：我们填的通知人国家提不上去，同时读-改-写只带我们那 36 列，
    公司那一列不在提交体里 → 整表写回把它抹成 NULL（把已有值删掉）。"""
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    master = [{"MASTER_NO": "176-62400004",
               "AMS_RECORD": {"MAWB_NO": "176-62400004", "NOTIFY_INFO_COUNTRY": "SA",
                              "NOTIFY_INFO_COMP_NAME": "MOHAMMED SALEEM", "SEND_STATUS": 0,
                              "HMY_ID": 1, "CREATE_TIME": "2026-09-26T13:49:15.677000"}}]
    sent = []
    restore = _patch_post([], monkey_master=master, sent=sent)
    try:
        company_api.submit_master({"mawb": "176-62400004", "ams": {"MAWB_NO": "176-62400004"}})
        rec = sent[-1]["body"]["AMS_RECORD"]
        assert "NOTIFYE_INFO_COUNTRY" not in rec, "旧名不该再出现在提交体里"
        assert rec.get("NOTIFY_INFO_COUNTRY") == "SA", \
            f"读到而没改的列要原样带回，否则整表写回把它抹成 NULL：{sorted(rec)}"
        sent.clear()
        company_api.submit_master({"mawb": "176-62400004",
                                   "ams": {"NOTIFY_INFO_COUNTRY": "CN"}})
        assert sent[-1]["body"]["AMS_RECORD"]["NOTIFY_INFO_COUNTRY"] == "CN", "人工改的值要覆盖读到旧值"
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old


def test_submit_body_carries_every_company_column_even_on_a_new_row():
    """IT 2026-10-09 定案：提交时全部字段都要在，没值的空着——字段缺了公司那边就写 NULL，
    而我们连"哪些列本来就该空"和"哪些列漏了"都分不清。首次录入（公司读不到行）最容易漏。"""
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    sent = []
    restore = _patch_post([], sent=sent)
    try:
        company_api.submit_order({"mawb": "020-18134281", "hawb": "NEW001", "stem": "T9",
                                  "air": {"MAWB_NO": "020-18134281", "HAWB_NO": "NEW001",
                                          "ORIGIN_NAME": "BJS", "DEST_NAME": "WAW",
                                          "PIECES": "", "WEIGHT": ""}})
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old
    rec = sent[-1]["body"]["HAWB_RECORD"]
    for col in company_api.HAWB_SUBMIT_COLS:
        assert col in rec, f"提交体少了列 {col}：公司会把它写成 NULL，且没人看得出来是漏还是空"
    assert rec["CONSIGNEE_INFO_EMAIL"] is None, "没值的要占位（null），不是缺键"
    assert rec["PIECES"] is None and rec["WEIGHT"] is None, \
        "空数字格要发 null：空串会被公司判「必须为整数」整条退回来"
    assert not [k for k in rec if k.endswith("_TAX_ID")], "公司列面没有税号格，别当未识别字段发出去"
    assert "GOODS_HS_CODE" not in rec, "分单表没有 HS 列"


def test_tax_number_shares_the_eori_cell_and_stays_idempotent():
    """同主体常常两个号都有（EORI=NL009076815、VAT=NL009076815B01）：两个都留，拼在一格。

    暂存能反复写，下一次读回来的 EORI 就已经是拼好的那串——不先拆再拼，
    每暂存一次就多挂一段，第三次就变成 "A / B / B / B"。"""
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    air = {"MAWB_NO": "020-18134281", "HAWB_NO": "NOAIRWAW050582",
           "CONSIGNEE_INFO_EORI": "NL009076815", "CONSIGNEE_INFO_TAX_ID": "NL009076815B01",
           "SHIPPER_INFO_TAX_ID": "91330109785349738M"}
    sent = []
    restore = _patch_post([dict(ROW)], sent=sent)
    try:
        company_api.submit_order({"mawb": "020-18134281", "hawb": "NOAIRWAW050582", "stem": "T1", "air": air})
        rec = sent[-1]["body"]["HAWB_RECORD"]
        assert rec["CONSIGNEE_INFO_EORI"] == "NL009076815 / NL009076815B01", rec["CONSIGNEE_INFO_EORI"]
        assert rec["SHIPPER_INFO_EORI"] == "91330109785349738M", "EORI 空着时税号直接占这格"
        # 第二次：公司那格已经是拼好的一串，读回来再拼一次不许变长
        # （换个字段，否则会被提交幂等锁当成连点吞掉——那是另一条用例管的事）
        restore()
        sent2 = []
        restore = _patch_post([dict(ROW, CONSIGNEE_INFO_EORI="NL009076815 / NL009076815B01")], sent=sent2)
        company_api.submit_order({"mawb": "020-18134281", "hawb": "NOAIRWAW050582", "stem": "T1",
                                  "air": dict(air, CONSIGNEE_INFO_EORI="NL009076815 / NL009076815B01",
                                              DEST_NAME="MXP")})
        again = [s for s in sent2 if j9_fake.is_write(s["path"])][-1]["body"]["HAWB_RECORD"]["CONSIGNEE_INFO_EORI"]
        assert again == "NL009076815 / NL009076815B01", f"重复提交把同一格越拼越长：{again}"
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old


def test_stage_order_live_writes_the_stage_endpoint_not_the_send_one():
    """公司暂存（用户 2026-10-09 要的那一档）：走 `2` 落状态 0，可反复改，外围不取。

    与提交发送共用先读后写、闸门与整表写回那一套——差别只有端点与落库状态。
    暂存**不写提交台账**：那是"已交付公司"的凭据，暂存没交付任何东西。"""
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    sent = []
    restore = _patch_post([dict(ROW)], sent=sent)
    try:
        out = company_api.stage_order({"mawb": "020-18134281", "hawb": "NOAIRWAW050582",
                                       "stem": "T1", "air": AIR_REVIEWED})
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old
    assert out["accepted"] is True
    assert any(j9_fake.is_stage(s["path"]) for s in sent), f"暂存要走 2：{[s['path'] for s in sent]}"
    assert not any(j9_fake.is_send(s["path"]) for s in sent), "暂存一次都不许碰提交发送口"
    rec = j9_fake.writes(sent)[0]["body"]["HAWB_RECORD"]
    assert rec["ORGIN_NAME"] == "BJS", "暂存同样要先读后写、整表带上没改的列"


def test_stage_then_submit_are_not_each_others_duplicate():
    """幂等锁按"同一份内容别写两次"设计，但暂存与提交内容相同是常态：
    制单员点完暂存马上点提交，第二次要是被同一把锁当连点吞掉，票就永远停在状态 0——
    正是这次改版要修的那个错。所以锁必须分档。"""
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    tmp = Path(tempfile.mkdtemp(prefix="hawb_guard_"))
    old_guard = config.SUBMIT_GUARD_DIR
    config.SUBMIT_GUARD_DIR = tmp
    sent = []
    restore = _patch_post([dict(ROW)], sent=sent)
    try:
        payload = {"mawb": "020-18134281", "hawb": "NOAIRWAW050582", "stem": "T1", "air": AIR_REVIEWED}
        company_api.stage_order(payload)
        out = company_api.submit_order(payload)
        assert out.get("idempotent") is not True, "暂存过的那一下不该把随后的提交当连点吞掉"
        assert any(j9_fake.is_send(s["path"]) for s in sent), f"提交必须真的发出去：{[s['path'] for s in sent]}"
    finally:
        restore()
        config.SUBMIT_GUARD_DIR = old_guard
        shutil.rmtree(tmp, ignore_errors=True)
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = old


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
