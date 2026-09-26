# -*- coding: utf-8 -*-
"""V2 录入员侧：/webplan/{stem} CCSP 填表计划接口（纯 dry-run——只读归档、只算计划，
不碰平台不落浏览器）。入口在录入员检索台：主单核对本来就是录入员的活。"""
import json
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import server

WEB_INPUTTER = Path(__file__).resolve().parent.parent / "web" / "inputter.html"

AIR = {
    "MAWB_NO": "999-95764373", "HAWB_NO": "VCE4373", "ORIGIN_NAME": "BJS", "DEST_NAME": "VCE",
    "PIECES": 5, "SLAC": 5, "WEIGHT": 257.0,
    "GOODS_INFO": "Inflatable boat", "GOODS_HS_CODE": "89031200",
    "SHIPPER_INFO_COMP_NAME": "QINGDAO HAIHAO YACHT CO.,LTD", "SHIPPER_INFO_CITY": "TAO",
    "SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_TAX_ID": "91370214MA949PRM6F",
    "CONSIGNEE_INFO_COMP_NAME": "OZONE S.R.L", "CONSIGNEE_INFO_CITTY": "VENICE",
    "CONSIGNEE_INFO_COUNTRY": "IT", "CONSIGNEE_INFO_POSTAL": "31032",
    "CONSIGNEE_INFO_TEL": "+390422470376",
}
RAW = {"SHIPPER_INFO_CITY": "QINGDAO", "CONSIGNEE_INFO_CITTY": "VENICE"}


def _isolate(tmp: Path):
    old = (config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR, auth.USERS_FILE)
    (tmp / "air").mkdir(exist_ok=True)
    (tmp / "raw").mkdir(exist_ok=True)
    (tmp / "air" / "T1.json").write_text(json.dumps(AIR, ensure_ascii=False), encoding="utf-8")
    (tmp / "raw" / "T1.json").write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR = tmp / "air", tmp / "raw"
    users_tmp = Path(tempfile.mkdtemp(prefix="hawb_wp_users_"))
    auth.USERS_FILE = users_tmp / "users.json"
    return old, users_tmp


def _restore(old, users_tmp):
    config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR, auth.USERS_FILE = old
    shutil.rmtree(users_tmp, ignore_errors=True)


def test_webplan_requires_login():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_wp_"))
    old, users_tmp = _isolate(tmp)
    try:
        r = TestClient(server.app).get("/webplan/T1")
        assert r.status_code == 401, r.status_code
    finally:
        _restore(old, users_tmp)


def test_webplan_returns_dry_run_plan():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_wp_"))
    old, users_tmp = _isolate(tmp)
    try:
        auth.ensure_seed()
        client = TestClient(server.app)
        client.post("/login", json={"name": "admin", "password": "admin123"})
        r = client.get("/webplan/T1")
        assert r.status_code == 200, r.text[:200]
        plan = r.json()["plan"]
        v = plan["values"]
        assert v["hmr.mawb.awbPre"] == "999" and v["hmr.mawb.awbNo"] == "95764373"
        assert v["hmr.hawb.shipper.city"] == "QINGDAO", "city 优先取 L2 原文（TAO 是三字码口径）"
        assert v["hmr.hawb.hsCode"] == "890312"
        assert plan["platform_only"], "平台独有控件要透出"
        assert any("CONSIGNEE_INFO_TAX_ID" in n for n in plan["needs_human"])
    finally:
        _restore(old, users_tmp)


def test_webplan_unknown_stem_is_404():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_wp_"))
    old, users_tmp = _isolate(tmp)
    try:
        auth.ensure_seed()
        client = TestClient(server.app)
        client.post("/login", json={"name": "admin", "password": "admin123"})
        assert client.get("/webplan/NO_SUCH_STEM").status_code == 404
        assert client.get("/webplan/..%2Fetc").status_code in (404,), "路径穿越不给读"
    finally:
        _restore(old, users_tmp)


def test_inputter_page_has_webplan_entry():
    html = WEB_INPUTTER.read_text(encoding="utf-8")
    assert "填表计划" in html and "/webplan/" in html, "录入员检索台要有 CCSP 填表计划入口"
