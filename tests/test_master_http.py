# -*- coding: utf-8 -*-
"""主单路由回归：检索自动起解析、/master 看进度、/master/submit 的门与错误码。
模型与公司 HTTP 全部打桩，绝不真调、绝不出网。"""
import json
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import company_api
import config
import master_pipeline as mp
import server
import vlm_extract

AMS = {"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901", "SLAC": 10,
       "SHIPPER_INFO_COMP_NAME": "METSO (TIANJIN) INVESTMENT CO., LTD.",
       "SHIPPER_INFO_COUNTRY": "CN"}
MASTER_ROW = {"JOB_ID": 1, "MASTER_NO": "176-62400004",
              "SHIPPER_INFO": "METSO (TIANJIN) INVESTMENT CO., LTD. CHINA",
              "GOODS_NAME": "PISTON ROD", "AMS_RECORD": dict(AMS, HMY_ID=12, SEND_STATUS=0)}


def _isolate(tmp: Path):
    old = (auth.USERS_FILE, config.SUBMIT_LEDGER, config.MASTER_DIR, config.MASTER_LEDGER)
    auth.USERS_FILE = tmp / "users.json"
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.MASTER_DIR = tmp / "master"
    config.MASTER_LEDGER = tmp / "master_submitted.json"
    return old


def _restore(old, tmp):
    (auth.USERS_FILE, config.SUBMIT_LEDGER,
     config.MASTER_DIR, config.MASTER_LEDGER) = old
    shutil.rmtree(tmp, ignore_errors=True)


def _login(name="admin", pw="admin123"):
    c = TestClient(server.app)
    r = c.post("/login", json={"name": name, "password": pw})
    assert r.status_code == 200, r.text
    return c


def _stub_live(sent=None, master_rows=None):
    """把公司 HTTP 与模型都换成假的：live 检索/提交不出网，解析不花钱。"""
    old_cfg = (config.COMPANY_API_MODE, config.COMPANY_API_URL,
               config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    real_post, real_extract = company_api._j9_post, vlm_extract.extract_master

    def fake_post(path, body, key):
        (sent if sent is not None else []).append({"path": path, "body": body})
        if path.endswith("/mawb/"):
            return {"code": 0, "data": master_rows if master_rows is not None else [MASTER_ROW]}
        if path.endswith("/mawb2/"):
            return {"code": 0, "success": True, "action": "update", "rows": 1}
        if path.endswith("/hawb"):
            return {"code": 0, "data": []}
        raise AssertionError("未知路径 " + path)

    def fake_extract(transcript, **_kw):
        data = {c: None for c in mp.MASTER_COLS}
        data.update(AMS)
        return data

    company_api._j9_post = fake_post
    vlm_extract.extract_master = fake_extract

    def undo():
        company_api._j9_post = real_post
        vlm_extract.extract_master = real_extract
        (config.COMPANY_API_MODE, config.COMPANY_API_URL,
         config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY) = old_cfg
    return undo


def test_master_routes_require_login():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mhttp_"))
    old = _isolate(tmp)
    try:
        auth.ensure_seed()
        c = TestClient(server.app)
        assert c.get("/master/176-62400004").status_code == 401
        assert c.post("/master/submit", json={"mawb": "176-62400004", "ams": {}}).status_code == 401
    finally:
        _restore(old, tmp)


def test_search_auto_parses_and_master_endpoint_reports_done():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mhttp_"))
    old = _isolate(tmp)
    try:
        auth.ensure_seed()
        undo = _stub_live()
        try:
            c = _login()
            r = c.get("/company/mawb", params={"mawb": "176-62400004"})
            assert r.status_code == 200, r.text
            j = r.json()
            assert j["master"]["state"] in ("parsing", "done"), \
                "响应是解析起跑前序列化的，所以这里可能是 parsing；结果由 /master 轮询给"
            g = c.get("/master/176-62400004")
            assert g.status_code == 200 and g.json()["state"] == "done", \
                "后台任务要真的把解析跑完（TestClient 会在响应返回前执行 BackgroundTasks）"
            assert g.json()["ams"]["MAWB_NO"] == "176-62400004"
            assert g.json()["qc"]["flags"] == []
            assert len(g.json()["transcript"]["lines"]) >= 3, "L1 原文要带回去给票面栏展示"
            j2 = c.get("/company/mawb", params={"mawb": "176-62400004"}).json()
            assert j2["master"]["state"] == "done", "第二次检索直接带回复用结果：不再花一次调用"
        finally:
            undo()
    finally:
        _restore(old, tmp)


def test_search_in_mock_mode_reports_master_without_network():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mhttp_"))
    old = _isolate(tmp)
    try:
        auth.ensure_seed()
        c = _login()
        j = c.get("/company/mawb", params={"mawb": "235-96146363"}).json()
        assert j["mode"] == "mock"
        assert j["master"]["state"] == "failed" and "空" in j["master"]["error"], \
            "mock 空壳不该起解析，但要如实报为什么没结果"
    finally:
        _restore(old, tmp)


def test_master_result_carries_the_labeled_field_table():
    """36 列的列名/中文名/分组只有一份真源（master_fields），前端照着渲染——不再抄第二份。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mhttp_"))
    old = _isolate(tmp)
    try:
        auth.ensure_seed()
        undo = _stub_live()
        try:
            c = _login()
            c.get("/company/mawb", params={"mawb": "176-62400004"})
            j = c.get("/master/176-62400004").json()
            cols = [f[0] for f in j["fields"]]
            assert len(cols) == 36 and "NOTIFY_INFO_COUNTRY" in cols and "CONSIGNEE_INFO_CITY" in cols
            assert all(len(f) == 3 for f in j["fields"]), "每列都要带中文名与分组，前端才有段落标题"
        finally:
            undo()
    finally:
        _restore(old, tmp)


def test_force_reparse_runs_the_model_again():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mhttp_"))
    old = _isolate(tmp)
    calls = []
    try:
        auth.ensure_seed()
        undo = _stub_live()
        # 计数桩要盖在 _stub_live 的假提取之上（反过来装会被它替换掉），结束后再还原
        real = vlm_extract.extract_master

        def counting(tr, **_kw):
            calls.append(1)
            data = {c: None for c in mp.MASTER_COLS}
            data.update(AMS)
            return data

        vlm_extract.extract_master = counting
        try:
            c = _login()
            c.get("/company/mawb", params={"mawb": "176-62400004"})
            c.get("/company/mawb", params={"mawb": "176-62400004"})
            assert len(calls) == 1, "资料没变就该复用缓存"
            c.get("/company/mawb", params={"mawb": "176-62400004", "force": "1"})
            assert len(calls) == 2, "换了模型要能强制重跑一次"
        finally:
            vlm_extract.extract_master = real
            undo()
    finally:
        _restore(old, tmp)


def test_search_does_not_pretend_a_stale_cache_is_fresh():
    """缓存过期（改了提示词/换了模型）时，检索响应不能把旧结果当"已解析"回给页面——
    前端只在 parsing 时轮询，报 done 就等于让它一直显示过期解析，直到用户再搜一次。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mhttp_"))
    old = _isolate(tmp)
    try:
        auth.ensure_seed()
        undo = _stub_live()
        try:
            c = _login()
            c.get("/company/mawb", params={"mawb": "176-62400004"})
            p = config.MASTER_DIR / "17662400004.json"
            rec = json.loads(p.read_text(encoding="utf-8"))
            rec["face_md5"] = "过期"                # 装作提示词/列面变了
            p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            j = c.get("/company/mawb", params={"mawb": "176-62400004"}).json()
            assert j["master"]["state"] == "parsing", \
                f"缓存过期却报 {j['master']['state']}：前端不会轮询，页面就停在旧结果上"
            assert c.get("/master/176-62400004").json()["state"] == "done", "后台要真的重解析完"
        finally:
            undo()
    finally:
        _restore(old, tmp)


def test_master_submit_blocks_flags_and_passes_after_ack():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mhttp_"))
    old = _isolate(tmp)
    try:
        auth.ensure_seed()
        sent = []
        undo = _stub_live(sent=sent)
        try:
            c = _login()
            bad = dict(AMS, MAWB_NO="176-6240004", SHIPPER_INFO_POSTAL="x" * 60)
            r = c.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "admin", "ams": bad})
            assert r.status_code == 400, r.text
            assert "MAWB_NO" in r.text and "SHIPPER_INFO_POSTAL" in r.text
            assert sorted(r.json()["detail"]["flags"]) == sorted(mp.master_flags(bad)), \
                "400 要带结构化红旗：前端据此逐条出「确认无误」，不用解析文案"
            assert sent == [], "被门拦下时一次公司请求都不该发"
            flags = mp.master_flags(bad)
            r2 = c.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "admin",
                                                "ams": bad, "acked_flags": flags})
            assert r2.status_code == 200, r2.text
            assert any(s["path"].endswith("/mawb2/") for s in sent), "确认无误后才真正回传"
            led = json.loads((tmp / "master_submitted.json").read_text(encoding="utf-8"))
            assert led["17662400004"]["acked_flags"] == flags
        finally:
            undo()
    finally:
        _restore(old, tmp)


def test_master_submit_maps_locked_record_to_409_and_config_to_502():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mhttp_"))
    old = _isolate(tmp)
    try:
        auth.ensure_seed()
        locked = {"JOB_ID": 1, "MASTER_NO": "176-62400004",
                  "AMS_RECORD": dict(AMS, SEND_STATUS=1)}
        undo = _stub_live(master_rows=[locked])
        try:
            c = _login()
            r = c.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "admin", "ams": dict(AMS)})
            assert r.status_code == 409, r.text
            assert "SEND_STATUS" in r.text
        finally:
            undo()
        old_mode = config.COMPANY_API_MODE
        config.COMPANY_API_MODE = "live"
        try:
            c2 = _login()
            r2 = c2.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "admin", "ams": dict(AMS)})
            assert r2.status_code == 502, f"live 缺端点要 502 而不是静默成功：{r2.status_code} {r2.text[:120]}"
        finally:
            config.COMPANY_API_MODE = old_mode
    finally:
        _restore(old, tmp)
