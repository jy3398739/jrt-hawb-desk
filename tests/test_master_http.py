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
import j9_fake
import master_pipeline as mp
import server
import store
import vlm_extract
import web_src

AMS = {"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901", "SLAC": 10,
       "SHIPPER_INFO_COMP_NAME": "METSO (TIANJIN) INVESTMENT CO., LTD.",
       "SHIPPER_INFO_COUNTRY": "CN"}
MASTER_ROW = {"JOB_ID": 1, "MASTER_NO": "176-62400004",
              "SHIPPER_INFO": "METSO (TIANJIN) INVESTMENT CO., LTD. CHINA",
              "GOODS_NAME": "PISTON ROD", "AMS_RECORD": dict(AMS, HMY_ID=12, SEND_STATUS=0)}


def _isolate(tmp: Path):
    """主单这条链要指开的目录：账号表、两张台账、主单缓存，以及检索现在会读的落盘目录。

    `/company/mawb` 现在会带 pending（`store.ticket_rows` 扫 qc/staged/archive）——
    不指开的话，这条用例就是在读那台机器真 output 里的票，换台机器结果不一样。
    """
    old = (auth.USERS_FILE, config.SUBMIT_LEDGER, config.MASTER_DIR, config.MASTER_LEDGER,
           config.OUTPUT_QC_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR,
           config.TRANSCRIPT_DIR, config.STAGED_DIR, config.ARCHIVE_DIR)
    auth.USERS_FILE = tmp / "users.json"
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.MASTER_DIR = tmp / "master"
    config.MASTER_LEDGER = tmp / "master_submitted.json"
    config.OUTPUT_QC_DIR, config.OUTPUT_AIR_DIR = tmp / "qc", tmp / "air"
    config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR = tmp / "raw", tmp / "transcript"
    config.STAGED_DIR, config.ARCHIVE_DIR = tmp / "staged", tmp / "archive"
    for p in (config.OUTPUT_QC_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR,
              config.TRANSCRIPT_DIR, config.STAGED_DIR, config.ARCHIVE_DIR):
        p.mkdir()
    return old


def _restore(old, tmp):
    (auth.USERS_FILE, config.SUBMIT_LEDGER,
     config.MASTER_DIR, config.MASTER_LEDGER,
     config.OUTPUT_QC_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR,
     config.TRANSCRIPT_DIR, config.STAGED_DIR, config.ARCHIVE_DIR) = old
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
        if path.endswith("/hawb"):
            return {"code": 0, "data": []}
        if j9_fake.is_write(path):
            return j9_fake.write_ok(path)
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
                                                "ams": bad, "acked_flags": flags,
                                                "acked_no_review": True})
            assert r2.status_code == 200, r2.text
            assert any(j9_fake.is_send(s["path"]) for s in sent), "确认无误后才真正回传（走 mawb3）"
            led = json.loads((tmp / "master_submitted.json").read_text(encoding="utf-8"))
            assert led["17662400004"]["acked_flags"] == flags
            assert led["17662400004"]["no_inputter_review"] is True, \
                "没录入员复核过就发的，主单台账也要留下这一档（与分单同构）"
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
            r = c.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "admin",
                                               "ams": dict(AMS), "acked_no_review": True})
            assert r.status_code == 409, r.text
            assert "SEND_STATUS" in r.text
        finally:
            undo()
        old_mode = config.COMPANY_API_MODE
        config.COMPANY_API_MODE = "live"
        try:
            c2 = _login()
            r2 = c2.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "admin",
                                                 "ams": dict(AMS), "acked_no_review": True})
            assert r2.status_code == 502, f"live 缺端点要 502 而不是静默成功：{r2.status_code} {r2.text[:120]}"
        finally:
            config.COMPANY_API_MODE = old_mode
    finally:
        _restore(old, tmp)


def _acct(name, role, pw):
    auth.ensure_seed()
    u, err = auth.set_password(name, pw, role)
    assert err is None, err
    return _login(name, pw)


def test_master_stage_parks_a_copy_at_the_company_but_never_sends():
    """主单侧的「暂存」：录入员改的 36 列从前根本没上过服务器（MV.edit 只在浏览器里，
    换台机器、刷新一下就没了），所以分单有了暂存档，主单也必须有——需求二说的是"主单和分单的暂存内容"。

    2026-10-09 起这一档还会在公司那边占位（mawb2，落状态 0，可反复改），
    但**绝不许碰提交发送口（mawb3）**：发送永远是人工点「提交主单回公司」那一下。
    """
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mstage_"))
    old = _isolate(tmp)
    sent = []
    undo = _stub_live(sent=sent)
    try:
        rv = _acct("马殿齐", "reviewer", "rv-123456")
        assert rv.get("/company/mawb", params={"mawb": "176-62400004"}).status_code == 200
        body = {"mawb": "176-62400004", "ams": dict(AMS, GOODS_INFO_HSCODE="8412909080"),
                "acked_flags": []}
        r = rv.post("/master/stage", json=body)
        assert r.status_code == 200, r.text
        rec = json.loads((config.MASTER_DIR / "17662400004.json").read_text(encoding="utf-8"))
        assert rec["ams_final"]["GOODS_INFO_HSCODE"] == "8412909080", rec.get("ams_final")
        assert rec["ams_model"]["GOODS_INFO_HSCODE"] == "841290901", "模型那版被人工值盖掉了"
        assert list(rec["events"][0]["edits"]) == ["GOODS_INFO_HSCODE"], rec["events"]
        assert list(rec["edits"]) == ["GOODS_INFO_HSCODE"], \
            "顶层那份逐字段差异是给前端合并用的：没有它，前端只能自己比，45.0 与 45 会被算成改动"
        assert rec["events"][0]["role"] == "reviewer" and rec["stager"] == "马殿齐", rec["events"]
        assert any(j9_fake.is_stage(s["path"]) for s in sent), f"暂存要在公司那边占位：{sent}"
        assert not [s for s in sent if j9_fake.is_send(s["path"])], f"暂存不许提交发送：{sent}"
        assert r.json()["company"]["ok"] is True, "回执要告诉人公司那一发成没成"
    finally:
        undo()
        _restore(old, tmp)


def test_master_submit_requires_the_ack_when_no_inputter_reviewed():
    """与分单同一条门：没录入员复核过就要一次显式确认，台账留下 no_inputter_review 与发出去的值。

    同构不是抄样式：主单同样是整表写回，没核对过就发，出事时一样要能回答"这张当时谁看过"。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mack_"))
    old = _isolate(tmp)
    undo = _stub_live()
    try:
        rv = _acct("马殿齐", "reviewer", "rv-123456")
        assert rv.get("/company/mawb", params={"mawb": "176-62400004"}).status_code == 200
        ams = dict(AMS, GOODS_NAME="PISTON ROD ASSY")
        assert rv.post("/master/stage", json={"mawb": "176-62400004", "ams": ams}).status_code == 200
        r = rv.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "马殿齐",
                                           "ams": ams, "acked_flags": []})
        assert r.status_code == 400, f"没人复核过却直接发出去了：{r.status_code} {r.text[:150]}"
        assert r.json()["detail"].get("ack") == "no_inputter_review", r.text
        assert store.master_ledger() == {}, "被拒的提交不许进主单台账"

        r2 = rv.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "马殿齐",
                                            "ams": ams, "acked_flags": [], "acked_no_review": True})
        assert r2.status_code == 200, r2.text
        e = store.master_ledger()["17662400004"]
        assert e["no_inputter_review"] is True and e["ams_sent"]["GOODS_NAME"] == "PISTON ROD ASSY", e
        rec = json.loads((config.MASTER_DIR / "17662400004.json").read_text(encoding="utf-8"))
        assert rec["submitted_at"] and rec["submitter"] == "马殿齐", rec

        inp = _acct("刘明", "inputter", "in-123456")
        assert inp.post("/master/stage", json={"mawb": "176-62400004", "ams": ams}).status_code == 200
        r3 = inp.post("/master/submit", json={"mawb": "176-62400004", "reviewer": "刘明",
                                             "ams": ams, "acked_flags": []})
        assert r3.status_code == 200, f"录入员自己复核过就不该再要确认：{r3.text[:150]}"
        assert store.master_ledger()["17662400004"]["no_inputter_review"] is False
    finally:
        undo()
        _restore(old, tmp)


def test_desk_has_a_master_stage_button_next_to_the_submit_one():
    """主单核对页也要有那一档：只给分单做暂存，录入员就没法把主单的核对结果交给同事。"""
    js = web_src.desk()
    assert 'id="mvStage"' in js, "主单核对页没有「暂存」按钮"
    i = js.index("async function mvStage(")
    body = js[i:js.index("\n}", i)]
    assert 'BASE + "/master/stage"' in body, "暂存没接到 /master/stage"
    assert "/master/submit" not in body, "主单暂存里混进了回传那条路：那会真写公司"
    assert 'MV.rec.ams_final' in js or "rec.ams_final" in js, "打开主单要认服务器上那份人工值"
    # 主单同样要有"未经录入员复核"的那次确认，而且不能把事件对象当成确认参数（老 listener 的坑）
    i2 = js.index("async function mvSubmit(")
    body2 = js[i2:js.index("\n}", i2)]
    assert "no_inputter_review" in body2, "主单没接未经复核的确认要求"
    assert "if (ackNoReview === true) payload.acked_no_review = true;" in body2, \
        "acked_no_review 必须只在人确认之后才带"
    assert 'addEventListener("click", () => mvSubmit())' in js, \
        "直接把 mvSubmit 当监听器会把 MouseEvent 当成 ackNoReview，等于默认替人确认"
