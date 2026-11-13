# -*- coding: utf-8 -*-
"""EORI 官网核验的服务端接线回归：/eori/check 端点（门禁、判形、旗面、查不通安静）、
主单视图读取时并旗。网络一律假 urlopen；缓存与质检目录指到 tmp。"""
import json
import shutil
import tempfile
from pathlib import Path

import auth
import config
import eori_check
import server
from fastapi.testclient import TestClient

from test_master_http import _isolate, _restore, _login

ORIG = {}


def _setup(tmp: Path):
    ORIG.update(path=eori_check.CACHE_PATH, mem=eori_check._CACHE)
    eori_check.CACHE_PATH = tmp / "eori_cache.json"
    eori_check._CACHE = {}
    return tmp


def _teardown():
    eori_check.CACHE_PATH = ORIG["path"]
    eori_check._CACHE = ORIG["mem"]


def _stub_check_one(monkey_results):
    """results: {号: 状态}；不在表里的号回 unreachable。"""
    old = eori_check.check_one
    eori_check.check_one = lambda num, timeout=15.0: monkey_results.get(num, "unreachable")
    return lambda: setattr(eori_check, "check_one", old)


def test_eori_check_route_requires_login():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eorisrv_"))
    old = _isolate(tmp)
    _setup(tmp)
    try:
        auth.ensure_seed()
        c = TestClient(server.app)
        assert c.post("/eori/check", json={"cells": {}}).status_code == 401, "登录是唯一门禁"
    finally:
        _teardown()
        _restore(old, tmp)


def test_eori_check_route_reports_verdicts_and_parks_flags_in_qc():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eorisrv_"))
    old = _isolate(tmp)
    _setup(tmp)
    undo_stub = _stub_check_one({"IT03268900267": "invalid", "SE5563646560": "valid"})
    try:
        auth.ensure_seed()
        c = _login()
        # 质检记录只在解析后才有：先放一份（端点不硬造记录，与 append_qc_flags 同一口径）
        (tmp / "qc" / "EORIQC1.json").write_text('{"flags": [], "needs_review": false}',
                                                 encoding="utf-8")
        r = c.post("/eori/check", json={
            "cells": {"SHIPPER_INFO_EORI": "IT03268900267 / SE5563646560"},
            "stem": "EORIQC1", "force": True})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["results"]["SHIPPER_INFO_EORI"] == {"IT03268900267": "invalid",
                                                     "SE5563646560": "valid"}, j["results"]
        assert len(j["flags"]) == 1 and j["flags"][0].startswith("SHIPPER_INFO_EORI") \
            and "IT03268900267" in j["flags"][0], j["flags"]
        qc = json.loads((tmp / "qc" / "EORIQC1.json").read_text(encoding="utf-8"))
        assert qc["flags"] == j["flags"] and qc["needs_review"] is True, \
            "分单票给了 stem，invalid 的旗要落进质检记录"
        # unreachable 是中性档：不挂旗、不动质检记录
        undo_stub()
        undo_stub2 = _stub_check_one({})
        r2 = c.post("/eori/check", json={"cells": {"CONSIGNEE_INFO_EORI": "NL0000000000"},
                                         "stem": "EORIQC1", "force": True})
        j2 = r2.json()
        assert j2["unreachable"] == ["NL0000000000"] and j2["flags"] == [], j2
        qc2 = json.loads((tmp / "qc" / "EORIQC1.json").read_text(encoding="utf-8"))
        assert len(qc2["flags"]) == 1, "查不通不许追加旗"
        undo_stub2()
    finally:
        _teardown()
        _restore(old, tmp)


def test_master_view_merges_cached_eori_flags_with_human_edits():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eorisrv_"))
    old = _isolate(tmp)
    _setup(tmp)
    try:
        auth.ensure_seed()
        c = _login()
        mawb = "176-62400004"
        assert c.get("/company/mawb", params={"mawb": mawb}).status_code == 200, "检索即解析"
        # 模型那版没有 EORI：把人暂存的值盖上去（edits 覆盖 ams），缓存里种一个 invalid 号
        r = c.post("/master/stage", json={"mawb": mawb,
                                          "ams": {"SHIPPER_INFO_EORI": "IT03268900267"},
                                          "acked_flags": []})
        assert r.status_code == 200, r.text
        eori_check._CACHE.update({"IT03268900267": {
            "status": "invalid",
            "checked_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")}})
        j = c.get(f"/master/{mawb}").json()
        flags = (j.get("qc") or {}).get("flags") or []
        assert any(f.startswith("SHIPPER_INFO_EORI") and "IT03268900267" in f for f in flags), flags
        assert any("官网核验不通过" in f for f in flags), "旗面要教人去核票"
        # 翻案成 valid：读取时旗就消失（不回写记录，缓存说了算）
        eori_check._CACHE["IT03268900267"]["status"] = "valid"
        j2 = c.get(f"/master/{mawb}").json()
        assert not [f for f in ((j2.get("qc") or {}).get("flags") or [])
                    if "官网核验不通过" in f], "invalid 翻案后旧旗不该再出现"
    finally:
        _teardown()
        _restore(old, tmp)
