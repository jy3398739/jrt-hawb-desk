# -*- coding: utf-8 -*-
"""提交完的票二次查看只读（2026-11-13 业主："所有选项不给修改权限，全部变灰只能查看"）。

两半：
① 服务端是门——四道写入口（分单暂存/提交、主单暂存/提交）对"已提交且不是管理员"一律 409，
   管理员走 `POST /unlock` 先留一笔痕再放开本次查看；
② 前端是体验——值框 readonly、确认/优化/暂存/提交/重解析全 disabled，管理员多一颗「解锁修正」，
   刷新即重新锁（解锁状态不落 localStorage：落盘就等于把"提交完不能改"永久松了）。
"""
import contextlib
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import master_pipeline
import server
import store
import web_src

MAWB = "999-90273130"
HAWB_STEM = "RO-H1"


@contextlib.contextmanager
def _sandbox():
    old = (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
           config.ARCHIVE_DIR, config.SUBMIT_LEDGER, config.STAGED_DIR, auth.USERS_FILE,
           config.MASTER_DIR, config.MASTER_LEDGER, config.VOID_LEDGER,
           config.MAWB_SOURCE_DIR, config.UNLOCK_LEDGER)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR = tmp / "raw", tmp / "air"
        config.OUTPUT_QC_DIR, config.ARCHIVE_DIR = tmp / "qc", tmp / "archive"
        config.STAGED_DIR = tmp / "staged"
        config.SUBMIT_LEDGER = tmp / "submitted.json"
        config.MASTER_DIR = tmp / "master"
        config.MASTER_LEDGER = tmp / "master_submitted.json"
        config.VOID_LEDGER = tmp / "voided.json"
        config.MAWB_SOURCE_DIR = tmp / "mawb_source"
        config.UNLOCK_LEDGER = tmp / "unlocks.json"
        auth.USERS_FILE = tmp / "users.json"
        for p in (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
                  config.ARCHIVE_DIR, config.STAGED_DIR, config.MASTER_DIR,
                  config.MAWB_SOURCE_DIR):
            p.mkdir()
        try:
            yield tmp
        finally:
            (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
             config.ARCHIVE_DIR, config.SUBMIT_LEDGER, config.STAGED_DIR, auth.USERS_FILE,
             config.MASTER_DIR, config.MASTER_LEDGER, config.VOID_LEDGER,
             config.MAWB_SOURCE_DIR, config.UNLOCK_LEDGER) = old


def _login(c, name, pw):
    r = c.post("/login", json={"name": name, "password": pw})
    assert r.status_code == 200, r.text
    return r


def _hawb_submitted():
    """一张已提交的分单：质检/air/归档 + 提交台账里那条（台账就是"提交完"的唯一判据）。"""
    store.save_result(HAWB_STEM, {"MAWB_NO": MAWB, "HAWB_NO": "H1"},
                      {"MAWB_NO": MAWB, "HAWB_NO": "H1", "DEST_NAME": "AMS"})
    store.save_qc(HAWB_STEM, {"source_name": HAWB_STEM + ".pdf", "channel": "vlm", "elapsed": 3,
                              "processed_at": "2026-11-13T09:00:00", "uploader": "宛平",
                              "uploader_role": "reviewer", "needs_review": False,
                              "flags": [], "error": ""})
    (config.ARCHIVE_DIR / HAWB_STEM).mkdir(exist_ok=True)
    store.mark_submitted(HAWB_STEM, MAWB, "H1", "宛平", receipt={"accepted": 1},
                         air_sent={"MAWB_NO": MAWB, "HAWB_NO": "H1"})


def _master_submitted():
    master_pipeline._write({
        "mawb": MAWB, "state": "done", "started_at": "2026-11-13T08:00:00",
        "updated_at": "2026-11-13T08:00:10", "elapsed": 6.0, "model": "qwen3.8-flash",
        "ams": {"MAWB_NO": MAWB}, "ams_model": {"MAWB_NO": MAWB},
        "transcript": {"full_text": "主单资料"},
        "qc": {"flags": [], "needs_review": False}, "error": ""})
    store.mark_master_submitted(MAWB, "宛平", receipt={"action": "ok"},
                                ams_sent={"MAWB_NO": MAWB, "GOODS_NAME": "PISTON ROD"})


def _reviewer_and_admin():
    auth.set_password("宛平", "pw-readonly", "reviewer")
    c = TestClient(server.app)
    _login(c, "宛平", "pw-readonly")
    adm = TestClient(server.app)
    _login(adm, "admin", "admin123")
    return c, adm


def test_four_write_routes_refuse_a_submitted_house_ticket_for_reviewers():
    """界面上灰掉只是体验：服务端这一道才是门。制单员/录入员对已提交分单
    暂存、提交、复核留痕三样都 409；管理员放行（界面上的解锁另走 /unlock 留痕）。"""
    with _sandbox():
        _hawb_submitted()
        rev, adm = _reviewer_and_admin()
        body = {"tickets": [{"stem": HAWB_STEM,
                             "air_reviewed": {"MAWB_NO": MAWB, "HAWB_NO": "H1"}}]}
        st = rev.post("/stage", json=body)
        assert st.status_code == 409 and "只读" in st.json()["detail"], st.text
        sub = rev.post("/submit", json=body)
        assert sub.status_code == 409 and "只读" in sub.json()["detail"], sub.text
        rv = rev.post("/review", json={"kind": "hawb", "key": HAWB_STEM})
        assert rv.status_code == 409 and "只读" in rv.json()["detail"], rv.text
        assert adm.post("/stage", json=body).status_code == 200, "管理员走解锁修正这条路，服务端放行"


def test_two_master_write_routes_refuse_a_submitted_master_for_reviewers():
    with _sandbox():
        _master_submitted()
        rev, adm = _reviewer_and_admin()
        st = rev.post("/master/stage", json={"mawb": MAWB, "ams": {"MAWB_NO": MAWB}})
        assert st.status_code == 409 and "只读" in st.json()["detail"], st.text
        sub = rev.post("/master/submit", json={"mawb": MAWB, "ams": {"MAWB_NO": MAWB}})
        assert sub.status_code == 409 and "只读" in sub.json()["detail"], sub.text
        assert adm.post("/master/stage", json={"mawb": MAWB,
                                               "ams": {"MAWB_NO": MAWB}}).status_code == 200


def _stub_company_master():
    """把公司检索替换成"有主单资料"的桩：这条用例只关心会不会去调模型，不关心 j9。"""
    import company_api
    old = company_api.search_mawb
    company_api.search_mawb = lambda mawb: {
        "mode": "mock", "mawb": mawb,
        "mawb_order": {"MAWB_NO": mawb, "GOODS_NAME": "FROM-COMPANY"},
        "hawb_orders": [], "pending": [], "source_available": False}
    return lambda: setattr(company_api, "search_mawb", old)


def test_viewing_a_submitted_master_never_calls_the_model_again():
    """已提交主单的“查看”不许二次解析（2026-11-13 业主："已提交的查看主单还会二次解析吗？
    页面显示解析中"）。缓存指纹过期也不许：直接显示本机缓存，值取提交台账里发出去的那版。"""
    with _sandbox():
        _master_submitted()
        # 故意让缓存指纹全对不上：旧版本解析的 / 换过模型的那种状态
        rec = json.loads((config.MASTER_DIR / (store.norm_no(MAWB) + ".json")).read_text(encoding="utf-8"))
        rec.update({"text_md5": "stale", "face_md5": "stale", "model": "old-model",
                    "ams": {"MAWB_NO": MAWB, "GOODS_NAME": "MODEL-OLD"}})
        (config.MASTER_DIR / (store.norm_no(MAWB) + ".json")).write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        undo = _stub_company_master()
        old_ensure, old_fresh = master_pipeline.ensure, master_pipeline.fresh_record
        master_pipeline.ensure = lambda *a, **k: (_ for _ in ()).throw(AssertionError("已提交主单不许再解析"))
        master_pipeline.fresh_record = lambda *a, **k: (_ for _ in ()).throw(AssertionError("已提交主单不许判指纹"))
        try:
            rev, _ = _reviewer_and_admin()
            j = rev.get("/company/mawb", params={"mawb": MAWB}).json()
            assert j["master"]["state"] == "done", "查看已提交主单不该进「解析中」"
            assert j["master"]["ams"]["GOODS_NAME"] == "PISTON ROD", \
                "只读页显示发出去那一版（台账 ams_sent），不是缓存里模型旧值"
        finally:
            master_pipeline.ensure, master_pipeline.fresh_record = old_ensure, old_fresh
            undo()


def test_force_reparse_is_refused_for_a_submitted_master_and_empty_cache_still_opens():
    """「重新解析」对已提交主单一律 409（管理员也一样，修正请解锁改值）；
    本机缓存没有时用台账那份值拼只读页，不能退回解析中、也不能空白。"""
    with _sandbox():
        _master_submitted()
        undo = _stub_company_master()
        old_ensure = master_pipeline.ensure
        master_pipeline.ensure = lambda *a, **k: (_ for _ in ()).throw(AssertionError("已提交主单不许再解析"))
        try:
            rev, adm = _reviewer_and_admin()
            r = adm.get("/company/mawb", params={"mawb": MAWB, "force": 1})
            assert r.status_code == 409 and "只读" in r.json()["detail"], r.text
            for p in config.MASTER_DIR.glob("*.json"):
                p.unlink()
            j = rev.get("/company/mawb", params={"mawb": MAWB}).json()
            assert j["master"]["state"] == "done" and j["master"]["ams"]["MAWB_NO"] == MAWB, \
                "没有缓存也要用台账发出去那版把只读页撑起来"
        finally:
            master_pipeline.ensure = old_ensure
            undo()


def test_unlock_is_admin_only_and_always_leaves_a_trace():
    """解锁本身是一笔动作：谁、何时、哪张票。重复解锁就重复追加——别把两次修正并成一次。"""
    with _sandbox():
        _hawb_submitted()
        rev, adm = _reviewer_and_admin()
        assert rev.post("/unlock", json={"kind": "hawb", "key": HAWB_STEM}).status_code == 403
        ok = adm.post("/unlock", json={"kind": "hawb", "key": HAWB_STEM})
        assert ok.status_code == 200 and ok.json()["unlock"]["by"] == "admin", ok.text
        adm.post("/unlock", json={"kind": "hawb", "key": HAWB_STEM})
        trace = json.loads(config.UNLOCK_LEDGER.read_text(encoding="utf-8"))["events"]
        assert len(trace) == 2 and trace[0]["kind"] == "hawb" and trace[0]["key"] == HAWB_STEM
        assert trace[-1]["by"] == "admin" and trace[-1]["at"], "留痕要带谁、何时"
        assert adm.post("/unlock", json={"kind": "hawb", "key": "NOT-SUBMITTED"}).status_code == 409
        assert adm.post("/unlock", json={"kind": "x", "key": "y"}).status_code == 400


def test_master_public_reports_the_submit_time_from_the_ledger():
    """前端判主单"提交完没有"只看这一格：台账时间（缓存里的 submitted_at 只在暂存过又提交时才盖章）。"""
    with _sandbox():
        _master_submitted()
        rev, _ = _reviewer_and_admin()
        j = rev.get("/master/" + MAWB).json()
        assert j["submitted_at"] == store.master_ledger()[store.norm_no(MAWB)]["submitted_at"]


def test_view_only_is_one_rule_for_both_chains():
    """同一条判据只写一处：提交过且没过管理员解锁 = 只读；解不解锁都不许把状态存进本机偏好。"""
    assert web_src.logic("L.viewOnly(s, u)", s=True, u=False) is True
    assert web_src.logic("L.viewOnly(s, u)", s=True, u=True) is False
    assert web_src.logic("L.viewOnly(s, u)", s=False, u=False) is False
    assert web_src.logic("L.viewOnly(s, u)", s=False, u=True) is False


def test_the_master_view_locks_submitted_records_too():
    """主单这条链同一套：判据认 public 里的 submitted_at，值框 readonly，写入口 disabled。"""
    js = web_src.part("js/desk.js")
    body = web_src.fn(js, "function mvRO(")
    assert "submitted_at" in body and "L.viewOnly(" in body, "主单只读判据要认台账时间"
    render = js[js.index("function renderMain("):js.index("async function mvReparse(")]
    assert 'data-mk="${k}" rows="1"' in render and "mro ?" in render, \
        "主单值框没接只读态"
    assert 'id="mvSubmit"${mro ? " disabled" : ""}' in render, "主单提交钮在只读时没 disabled"
    assert '"重新解析" 对已提交主单一律禁用' in render or 'id="mvReparse"${mSent ? " disabled" : ""}' in render, \
        "重新解析对已提交主单（含解锁后）也必须 disabled"


def test_the_desk_renders_the_lock_and_keeps_it_out_of_local_storage():
    """值框只读、动作钮 disabled、解锁钮只给管理员、解锁要走服务端留痕；刷新必须重新锁。"""
    js = web_src.part("js/desk.js")
    assert "readonly" in js and "L.viewOnly(" in js, "只读判据要真落到值框上"
    enter = web_src.fn(js, "function blockEnter(")
    assert "tcur.ro" in enter and "preventDefault" in enter, \
        "只读票里回车还会顺手把空栏确认掉——readOnly 挡不住 keydown"
    ro = web_src.fn(js, "function roBarHtml(")
    assert "data-unlock=" in ro and "解锁修正" in ro, "管理员那颗解锁入口不在"
    assert "ME.role === \"admin\"" in ro, "解锁入口要按管理员画，不能登录就能解"
    assert 'BASE + "/unlock"' in js, "解锁要打服务端那一发（留痕在后端）"
    ls = js.split("const LS = {")[1].split("};")[0]
    assert "unlock" not in ls, "解锁状态不许写本机偏好：刷新重新锁才守得住"
