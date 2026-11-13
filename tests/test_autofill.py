# -*- coding: utf-8 -*-
"""主单兜底补号（2026-11-14 方案 A）回归：按主体配对、只填空格、留痕台账、旗面与合并。
全程不碰真 output：台账与质检目录指到 tmp。"""
import json
import shutil
import tempfile
from pathlib import Path

import autofill
import config

ORIG = {}


def _setup(tmp: Path):
    ORIG.update(file=autofill.FILE, qc=config.OUTPUT_QC_DIR)
    autofill.FILE = tmp / "auto_fill.json"
    config.OUTPUT_QC_DIR = tmp / "qc"
    (tmp / "qc").mkdir(exist_ok=True)
    return tmp


def _teardown():
    autofill.FILE = ORIG["file"]
    config.OUTPUT_QC_DIR = ORIG["qc"]


MASTER = {"GOODS_INFO_HSCODE": "8412909090,85171200",
          "SHIPPER_INFO_EORI": "SE5563646560", "SHIPPER_INFO_TAX": "USCI911201117706402073",
          "CONSIGNEE_INFO_EORI": "IT03268900267", "CONSIGNEE_INFO_TAX": "VAT769638661",
          "NOTIFY_INFO_EORI": "FR12345678901"}
AIR = {"MAWB_NO": "999-90273120", "GOODS_HS_CODE": "",
       "SHIPPER_INFO_EORI": "", "SHIPPER_INFO_TAX": "分单自己的税号",
       "CONSIGNEE_INFO_EORI": "", "CONSIGNEE_INFO_TAX": "",
       "NOTIFY_INFO_EORI": ""}


def test_plan_pairs_by_party_and_truncates_hs_keeps_house_values():
    fills = autofill.plan(AIR, MASTER, "999-90273120")
    assert fills["GOODS_HS_CODE"]["to"] == "841290,851712", "主单 HS 多段按前 6 位截好再补"
    assert fills["SHIPPER_INFO_EORI"]["to"] == "SE5563646560"
    assert fills["CONSIGNEE_INFO_TAX"]["to"] == "VAT769638661"
    assert "SHIPPER_INFO_TAX" not in fills, "分单自己有号的格绝不碰（业主定案）"
    assert "NOTIFY_INFO_EORI" not in fills, "分单没有通知人格，主单那份不硬塞"
    assert all(f["src"] == "999-90273120" for f in fills.values())


def test_plan_is_quiet_when_master_has_nothing():
    assert autofill.plan(AIR, {}, "999-90273120") == {}
    assert autofill.plan(dict(AIR, GOODS_HS_CODE="841290"), MASTER, "999-90273120") == \
        {k: v for k, v in autofill.plan(AIR, MASTER, "999-90273120").items()
         if k != "GOODS_HS_CODE"}, "分单 HS 已有值就不再补"


def test_record_load_merge_only_fills_empty_cells():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_af_"))
    try:
        _setup(tmp)
        fills = autofill.plan(AIR, MASTER, "999-90273120")
        autofill.record("AF1", fills)
        assert autofill.load("AF1") == fills
        # 人先给发货人 EORI 补了个自己的号：合并不得覆盖
        air = dict(AIR, SHIPPER_INFO_EORI="DK1111111111")
        merged = autofill.merge("AF1", air)
        assert merged["SHIPPER_INFO_EORI"] == "DK1111111111", "人有值按人的"
        assert merged["CONSIGNEE_INFO_EORI"] == "IT03268900267", "空的格才补"
        assert merged["GOODS_HS_CODE"] == "841290,851712"
        # 台账里不认识的键不外溢
        autofill.record("AF1", dict(fills, HACKED_X={"to": "x"}))
        merged2 = autofill.merge("AF1", AIR)
        assert "HACKED_X" not in merged2
    finally:
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_flags_for_fire_until_human_changes_the_cell():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_af_"))
    try:
        _setup(tmp)
        autofill.record("AF2", autofill.plan(AIR, MASTER, "999-90273120"))
        flags = autofill.flags_for("AF2", AIR)
        assert [f.split()[0] for f in flags] == ["GOODS_HS_CODE", "SHIPPER_INFO_EORI",
                                                 "CONSIGNEE_INFO_EORI", "CONSIGNEE_INFO_TAX"], \
            "旗以列名打头（审核台按列名挂旗）"
        assert any("999-90273120" in f and "自主单" in f for f in flags)
        # 人把格子改成了别的值：那一格不再催
        flags2 = autofill.flags_for("AF2", dict(AIR, CONSIGNEE_INFO_TAX="人自己的号"))
        assert not [f for f in flags2 if f.startswith("CONSIGNEE_INFO_TAX")], flags2
    finally:
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_quick_fill_needs_a_parsed_master_and_parks_flags_in_qc():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_af_"))
    try:
        _setup(tmp)
        (tmp / "qc").mkdir(exist_ok=True)
        (tmp / "qc" / "AF3.json").write_text('{"flags": [], "needs_review": false}', encoding="utf-8")
        rec = {"state": "done", "ams": dict(MASTER)}
        autofill.quick_fill("AF3", AIR, lambda mawb: rec)
        assert autofill.load("AF3"), "主单解析过就该补"
        qc = json.loads((tmp / "qc" / "AF3.json").read_text(encoding="utf-8"))
        assert len(qc["flags"]) == 4 and qc["needs_review"] is True, qc["flags"]
        # 主单没解析过：安静放弃
        autofill.quick_fill("AF4", AIR, lambda mawb: None)
        assert autofill.load("AF4") == {}
        # 五格都有号：根本不问主单
        called = []
        autofill.quick_fill("AF5", dict(AIR, GOODS_HS_CODE="841290", SHIPPER_INFO_EORI="a",
                                        CONSIGNEE_INFO_EORI="b", CONSIGNEE_INFO_TAX="c"),
                            lambda mawb: called.append(mawb))
        assert called == [], "没有缺格就不去查主单"
    finally:
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_background_fill_fetches_and_parses_the_master_when_missing():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_af_"))
    try:
        _setup(tmp)
        (tmp / "qc").mkdir(exist_ok=True)
        (tmp / "qc" / "AF6.json").write_text('{"flags": [], "needs_review": false}', encoding="utf-8")
        rec = {"state": "done", "ams": dict(MASTER)}
        autofill.background_fill("AF6", AIR, lambda mawb: None,
                                 lambda mawb: (_ for _ in ()).throw(RuntimeError("公司接口不通")))
        assert autofill.load("AF6") == {}, "接口不通安静放弃，不挂旗不报错"
        autofill.background_fill("AF6", AIR, lambda mawb: None, lambda mawb: rec)
        assert autofill.load("AF6"), "检索+解析成功就按方案 A 补"
        # 无主单号、或五格全有号：根本不进检索
        autofill.background_fill("AF7", {"MAWB_NO": "999"}, lambda m: None, lambda m: rec)
        autofill.background_fill("AF7", dict(AIR, GOODS_HS_CODE="841290", SHIPPER_INFO_EORI="a",
                                             CONSIGNEE_INFO_EORI="b", CONSIGNEE_INFO_TAX="c"),
                                 lambda m: None, lambda m: rec)
        assert autofill.load("AF7") == {}
    finally:
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)
