# -*- coding: utf-8 -*-
"""今日台账（设计文档 §3.3 的整页视图）数据源：某一天经手过哪些票、谁提交的、还剩几张没回公司。

这一页要回答的是制单员每天被问的那句"这张你发出去了没有"——以前只能翻 journalctl 或在两台机器
各看各的台账（§0 事实：台账是本机的，不共享）。
"""
import contextlib
import json
import tempfile
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import server
import store
import web_src

TODAY = date.today().isoformat()


@contextlib.contextmanager
def _sandbox():
    """输出目录、暂存目录、台账与账号表全挪进临时目录：这一页读的是本机磁盘，不能碰真 output/，
    更不能依赖运维机上那份可能已改过 admin123 的 users.json。
    主单那一半（缓存 + 提交台账 + 作废本）同理——台账现在两种票都出。"""
    old = (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
           config.ARCHIVE_DIR, config.SUBMIT_LEDGER, config.STAGED_DIR, auth.USERS_FILE,
           config.MASTER_DIR, config.MASTER_LEDGER, config.VOID_LEDGER, config.MAWB_SOURCE_DIR)
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
             config.MAWB_SOURCE_DIR) = old



def _ticket(stem, mawb="999-95764373", hawb="JRT28643", when=TODAY, flags=None, error=""):
    store.save_result(stem, {"MAWB_NO": mawb, "HAWB_NO": hawb},
                      {"MAWB_NO": mawb, "HAWB_NO": hawb, "DEST_NAME": "AMS"})
    store.save_qc(stem, {"source_name": stem + ".pdf", "channel": "vlm", "elapsed": 8.4,
                         "processed_at": when + "T09:15:00", "needs_review": bool(flags),
                         "flags": flags or [], "error": error})


def test_rows_cover_only_that_day_and_join_the_three_sources():
    with _sandbox():
        _ticket("999-95764373JRT28643", when=TODAY, flags=["SHIPPER_INFO_TEL 缺区号"])
        _ticket("上月的票", when="2026-08-02")
        store.mark_submitted("999-95764373JRT28643", "999-95764373", "JRT28643", "马殿齐",
                             receipt={"accepted": 1}, company_action="updated")
        (config.ARCHIVE_DIR / "999-95764373JRT28643").mkdir()
        rows = {r["stem"]: r for r in store.day_rows(TODAY)}
        assert list(rows) == ["999-95764373JRT28643"], "别的日子经手的票不该混进来"
        r = rows["999-95764373JRT28643"]
        assert r["mawb"] == "999-95764373" and r["hawb"] == "JRT28643"
        assert r["flags"] == 1 and r["needs_review"] and not r["failed"]
        assert r["submitted"]["reviewer"] == "马殿齐" and r["submitted"]["action"] == "updated"
        assert r["has_original"], "原件在归档目录里就该报有，否则这页的「看原件」是死链"


def test_unsubmitted_and_failed_tickets_are_still_rows():
    """解析了但没提交 = 还没回公司；解析失败 = 得更醒目地摆出来。两种都不能从这一页消失。"""
    with _sandbox():
        _ticket("没提交的", when=TODAY)
        _ticket("坏票", when=TODAY, error="模型无响应")
        rows = {r["stem"]: r for r in store.day_rows(TODAY)}
        assert rows["没提交的"]["submitted"] is None and rows["没提交的"]["has_original"] is False
        assert rows["坏票"]["failed"] and rows["坏票"]["flags"] == 0


def test_broken_qc_record_does_not_take_down_the_page():
    with _sandbox():
        _ticket("好票", when=TODAY)
        (config.OUTPUT_QC_DIR / "坏记录.json").write_text("{不是 json", encoding="utf-8")
        rows = store.day_rows(TODAY)
        assert [r["stem"] for r in rows] == ["好票"], "一条坏记录把整页拖挂，等于这页没法用"


def _master(mawb="999-90273120", when=TODAY, state="done", flags=None, ams=None):
    """写一条主单解析记录（缓存形态与 master_pipeline 真写的一致：按主单号一份、覆盖式）。"""
    import master_pipeline
    ams = ams if ams is not None else {"MAWB_NO": mawb}
    return master_pipeline._write({
        "mawb": mawb, "state": state, "started_at": when + "T10:05:00",
        "updated_at": when + "T10:05:12", "elapsed": 7.2, "model": "qwen3.8-flash",
        "ams": ams, "ams_model": ams, "transcript": {"full_text": "主单资料"},
        "qc": {"flags": flags or [], "needs_review": bool(flags or [])},
        "error": "" if state != "failed" else "ReadTimeout: 主单解析超时"})


def test_voiding_a_row_marks_it_and_touches_nothing_else():
    """作废只是打一个戳。提交台账、归档原件、索引一律不动——
    「这张票回传过公司」的凭据不能从台账上点一下就没了，否则以后出了争议连查都没处查。"""
    with _sandbox():
        _ticket("V1", mawb="999-95764373", hawb="HV1")
        (config.ARCHIVE_DIR / "V1").mkdir()
        store.mark_submitted("V1", "999-95764373", "HV1", "马殿齐", receipt={"accepted": 1})
        e = store.void_row("hawb", "V1", "admin")
        assert e["by"] == "admin" and e["at"], e
        row = [r for r in store.day_rows(TODAY) if r["stem"] == "V1"][0]
        assert row["voided"]["by"] == "admin", "戳要回到行上，前端才知道这行该收起来、还能恢复"
        assert row["submitted"]["reviewer"] == "马殿齐", "作废不该把提交记录一起抹了"
        assert store.ledger().get("V1"), "提交台账是唯一的回传凭据，作废不许动它"
        assert (config.ARCHIVE_DIR / "V1").is_dir(), "票面原件归档也不该动"
        assert store.submitted_by_mawb("999-95764373"), "作废的票在主单检索里仍要查得到"
        assert store.lookup_stem("999-95764373", "HV1") == "V1", "索引也照旧：作废≠这张票没存在过"


def test_void_is_idempotent_and_reversible():
    """同一行反复作废只留最后一次（谁、何时）；恢复要真的干净退出，不留半条记录。"""
    with _sandbox():
        _ticket("V2")
        first = store.void_row("hawb", "V2", "张三")
        again = store.void_row("hawb", "V2", "李四")
        assert list(store.voided_map()).count("hawb|V2") == 1, "同一行只该有一条作废记录"
        assert store.voided_map()["hawb|V2"]["by"] == "李四" and again["at"] >= first["at"]
        assert store.unvoid_row("hawb", "V2") is True
        assert "hawb|V2" not in store.voided_map()
        assert store.unvoid_row("hawb", "V2") is False, "没作废过的行，恢复要如实说没有"
        assert [r for r in store.day_rows(TODAY) if r["stem"] == "V2"][0]["voided"] is None


def test_master_tickets_join_the_day_ledger_with_their_own_columns():
    """主单在这台机器上也是「检索即解析、能暂存能提交」的一条票，以前台账里完全看不见它。
    现在两种行合一张表、按时间排，靠 kind 分列；主单行没有分单号，那几列老实给空而不是硬凑。"""
    with _sandbox():
        _ticket("H1", mawb="999-90273120", hawb="HM1", when=TODAY)
        _master("999-90273120", when=TODAY, flags=["航班日期没填"])
        rows = {r["kind"]: r for r in store.day_rows(TODAY)}
        assert set(rows) == {"hawb", "mawb"}, "台账现在两种票都该出"
        m = rows["mawb"]
        assert m["mawb"] == "999-90273120" and m["hawb"] == "", "主单行不该凭空造一个分单号"
        assert m["cols_total"] == 36 and m["filled"] == 1, m
        assert m["flags"] == 1 and m["state"] == "parsed" and m["failed"] is False
        assert m["model"] == "qwen3.8-flash" and m["stem"] == ""
        assert rows["hawb"]["voided"] is None and rows["hawb"]["kind"] == "hawb"


def test_master_row_state_follows_stage_then_submit():
    """主单行的状态要和分单一样说得清走到哪一步：解析过 → 暂存过 → 提交过（提交以台账为准）。"""
    import master_pipeline
    with _sandbox():
        _master("999-90273120")
        assert {r["kind"]: r for r in store.day_rows(TODAY)}["mawb"]["state"] == "parsed"
        master_pipeline.stage_master("999-90273120", {"MAWB_NO": "999-90273120", "ORIGIN": "SHA"},
                                     "刘明", "inputter")
        m = {r["kind"]: r for r in store.day_rows(TODAY)}["mawb"]
        assert m["state"] == "staged" and m["stager"] == "刘明" and m["edited"] == 1, m
        store.mark_master_submitted("999-90273120", "刘明",
                                    receipt={"accepted": 1, "action": "updated"})
        m = {r["kind"]: r for r in store.day_rows(TODAY)}["mawb"]
        assert m["state"] == "submitted" and m["submitted"]["reviewer"] == "刘明", m
        assert m["submitted"]["action"] == "updated"


def test_failed_master_is_a_row_too():
    """解析失败的主单同样要出现在台账上（分单失败一直有行）——不然「今天哪条没出来」看不出来。"""
    with _sandbox():
        _master("999-90273120", state="failed")
        m = {r["kind"]: r for r in store.day_rows(TODAY)}["mawb"]
        assert m["failed"] is True and m["state"] == "failed" and "超时" in m["error"]


def test_void_route_is_open_to_any_logged_in_role_and_validates():
    """作废是软闸门：登录就能点（弹二次确认、留痕），不做成"仅管理员"——
    掐掉制单员手边的清理入口，他们会改成不删、把测试数据一直留在台账里。"""
    with _sandbox():
        _ticket("E1"); _master("999-90273120")
        anon = TestClient(server.app)
        assert anon.post("/void", json={"kind": "hawb", "key": "E1"}).status_code == 401, \
            "作废没要登录"
        admin = TestClient(server.app)
        admin.post("/login", json={"name": "admin", "password": "admin123"})
        r = admin.post("/void", json={"kind": "hawb", "key": "E1"})
        assert r.status_code == 200 and r.json()["voided"]["by"] == "admin", r.text
        assert admin.post("/void", json={"kind": "别的", "key": "E1"}).status_code == 400
        assert admin.post("/void", json={"kind": "hawb", "key": "  "}).status_code == 400
        assert admin.post("/void", json={"kind": "hawb", "key": "带\n换行"}).status_code == 400, \
            "key 会写进 json 台账，控制字符要挡在门口"
        back = admin.post("/void", json={"kind": "hawb", "key": "E1", "void": False})
        assert back.status_code == 200 and back.json()["voided"] is None
        assert "hawb|E1" not in store.voided_map()
        auth.set_password("马殿齐", "pw-12345", "reviewer")
        rev = TestClient(server.app)
        assert rev.post("/login", json={"name": "马殿齐", "password": "pw-12345"}).status_code == 200
        assert rev.post("/void", json={"kind": "mawb", "key": "999-90273120"}).status_code == 200, \
            "制单员也能作废（留痕记的是他）"
        assert store.voided_map()["mawb|99990273120"]["by"] == "马殿齐"


def test_desk_day_table_shows_the_type_and_lets_anyone_void_a_row():
    """接线：类型列、作废与恢复按钮、「只看作废」开关，一个都不能少——
    只加后端不加显示，用户看到的还是那张删不掉的表。"""
    page, js = web_src.part("index.html"), web_src.part("js/desk.js")
    assert 'id="dayOnlyVoid"' in page, "缺「只看作废」开关"
    assert 'data-void="' in js and 'data-undo="' in js, "作废/恢复按钮没接上"
    assert '"/void"' in js, "作废没打后端"
    assert "L.dayVisible" in js, "筛作废要交给 logic 一处算，别在渲染里再判一遍"
    assert "kind" in js and ("主单" in js and "分单" in js), "类型列要说人话，不能显示 hawb/mawb"
    # 二次确认：点第一下只把这一行变成「确认作废？」，不直接发请求（防误点，也不弹浏览器原生框）
    assert "confirmVoid" in js or "confirm" in js, "作废要有二次确认，一键就删等于没有确认"
    assert "作废" in js and "恢复" in js


def test_day_route_requires_login_and_checks_the_date():
    with _sandbox():
        _ticket("票一", when=TODAY)
        anon = TestClient(server.app)
        assert anon.get("/day").status_code == 401, "未登录也给出经手记录与复核人姓名"
        login = TestClient(server.app)
        login.post("/login", json={"name": "admin", "password": "admin123"})
        j = login.get("/day").json()
        assert j["date"] == TODAY and [r["stem"] for r in j["rows"]] == ["票一"]
        assert j["reviewers"] == [], "今天没人提交，就不该凭空冒出复核人"
        assert login.get("/day?date=昨天").status_code == 400, "日期不合法要拒绝，不能悄悄回今天"
        y = login.get("/day?date=2026-08-02").json()
        assert y["rows"] == [] and y["date"] == "2026-08-02"


def test_day_counts_are_one_arithmetic_source():
    """「今天 N 张 · 已提交 M · 待提交 K · 失败 F」这句是制单员每天报数的口径，
    算错一次就没人再信这一页；所以放 logic.js 由 node 断输入输出。
    作废的行不算进 N/M/K/F（否则清完测试数据数还是老的），单独报一个 voided 供「只看作废」用。"""
    rows = [{"submitted": {"reviewer": "马殿齐"}, "failed": False, "flags": 2},
            {"submitted": None, "failed": False, "flags": 0},
            {"submitted": None, "failed": True, "flags": 0},
            {"submitted": {"reviewer": "宛平"}, "failed": False, "flags": 1},
            {"submitted": {"reviewer": "宛平"}, "failed": False, "flags": 9,
             "voided": {"by": "admin", "at": "x"}}]
    assert web_src.logic("L.daySummary(rs)", rs=rows) == {
        "total": 4, "submitted": 2, "pending": 2, "failed": 1, "flags": 3, "voided": 1}
    assert web_src.logic("L.daySummary(rs)", rs=[]) == {
        "total": 0, "submitted": 0, "pending": 0, "failed": 0, "flags": 0, "voided": 0}
    # 台账只显示没作废的那些（前端一处筛，别在渲染里再判一遍 truthy）
    assert web_src.logic("L.dayVisible(rs, onlyVoided)", rs=rows, onlyVoided=True) == [rows[4]]
    assert [r["flags"] for r in
            web_src.logic("L.dayVisible(rs, onlyVoided)", rs=rows, onlyVoided=False)] == [2, 0, 0, 1]



def test_desk_has_a_day_ledger_view_switching_without_leaving_the_workbench():
    """两个视图共用一页（§3.3）：切到今日台账要能一眼看到还差哪几张没回公司，
    点「打开」回到核对工作台接着核那张票——不能让人重新上传原件。"""
    page, js = web_src.part("index.html"), web_src.part("js/desk.js")
    assert 'id="viewDay"' in page and 'id="dayCard"' in page, "缺少今日台账入口或整页卡"
    assert 'role="tab"' in page and 'aria-selected' in page, "视图切换要用 tab 语义，读屏器才知道现在在哪一页"
    assert '"/day"' in js and "renderDay" in js, "前端没接 /day"
    assert "L.daySummary" in js, "计数要走 logic，别在渲染里再算一遍"
    op = js[js.index("function openDay("):js.index("function ", js.index("function openDay(") + 1)] \
        if "function openDay(" in js else ""
    assert "openHouse(" in js and 'data-open="' in js, "台账行的「打开」要复用 openHouse，不另开一套加载逻辑"
    assert "主单检索" in js, "公司发送状态不在这一页查，要指路去主单检索，不能让空列看着像『没发』"



def test_tickets_endpoint_reports_state_from_disk_not_from_a_browser():
    """需求一与需求二共用的一张表：这张票在这台服务器上走到哪一步了。

    从前"解析了但没提交"只活在某个人的浏览器里（desk.js 的 pendingUnder 读 S.tickets +
    localStorage），换个人、换台机器就什么都没留下——制单员上午传的票，录入员下午查主单看不见，
    问"到底传上来了没有"没人答得了。状态一律从盘上现算，不看任何前端状态：
    有 qc=parsed，另有暂存=staged，进了提交台账=submitted，qc 带着 error=failed。
    """
    with _sandbox():
        _ticket("A1", mawb="999-95764373", hawb="HA1")                        # 只解析
        _ticket("B2", mawb="999-95764373", hawb="HB2")                        # 解析 + 暂存
        _ticket("C3", mawb="999-95764373", hawb="HC3")                        # 已提交
        _ticket("D4", mawb="888-88888888", hawb="HD4", error="RuntimeError: 模型无响应")
        _ticket("E5", mawb="777-77777777", hawb="HE5", when="2026-08-02")     # 别天的票
        store.stage_ticket("B2", {"MAWB_NO": "999-95764373", "HAWB_NO": "HB2",
                                  "DEST_NAME": "ORD"}, "马殿齐", "reviewer")
        store.mark_submitted("C3", "999-95764373", "HC3", "刘明", {"mode": "mock"})

        anon = TestClient(server.app)
        assert anon.get("/tickets").status_code == 401, "未登录就能读全桌经手记录与人名"
        login = TestClient(server.app)
        login.post("/login", json={"name": "admin", "password": "admin123"})

        r = login.get("/tickets")
        assert r.status_code == 200, r.text
        rows = {x["stem"]: x for x in r.json()["rows"]}
        got = {k: rows[k]["state"] for k in ("A1", "B2", "C3", "D4")}
        assert got == {"A1": "parsed", "B2": "staged", "C3": "submitted", "D4": "failed"}, got
        assert "E5" in rows, "不填日期就该是全表：录入员要看的待办不止今天的"
        assert rows["B2"]["stager"] == "马殿齐" and rows["B2"]["edited"] == 1, rows["B2"]
        assert rows["B2"]["reviewed_by_inputter"] is False, "录入员没碰过却报已复核"
        assert rows["C3"]["submitted"]["reviewer"] == "刘明", rows["C3"]
        assert rows["A1"]["stager"] == "", "没暂存过的票凭空冒出暂存人"

        only = login.get("/tickets", params={"state": "staged"}).json()["rows"]
        assert [x["stem"] for x in only] == ["B2"], only
        under = login.get("/tickets", params={"mawb": "99995764373"}).json()["rows"]
        assert sorted(x["stem"] for x in under) == ["A1", "B2", "C3"], \
            "主单号筛选要认归一化写法，且不能把别的主单混进来"
        today = login.get("/tickets", params={"date": TODAY}).json()["rows"]
        assert "E5" not in {x["stem"] for x in today}, "带 date 就等于今日台账，别把别天的混进来"


def test_a_staged_ticket_carries_the_uploader_and_the_model_too():
    """按人、按模型分账（需求三）的读数面：一行里要同时看得见
    谁传的、哪个模型读的、谁暂存的、谁提交的——少一样就得回去翻日志，而日志会滚掉。"""
    with _sandbox():
        _ticket("F6", mawb="999-95764373", hawb="HF6")
        qc = json.loads((config.OUTPUT_QC_DIR / "F6.json").read_text(encoding="utf-8"))
        qc.update({"uploader": "马殿齐", "uploader_role": "reviewer",
                   "model": "qwen3.8-flash", "model_choice": "qwen38-flash-bailian"})
        store.save_qc("F6", qc)
        store.stage_ticket("F6", {"MAWB_NO": "999-95764373", "HAWB_NO": "HF6"}, "刘明", "inputter")
        login = TestClient(server.app)
        login.post("/login", json={"name": "admin", "password": "admin123"})
        row = [x for x in login.get("/tickets").json()["rows"] if x["stem"] == "F6"][0]
        assert row["uploader"] == "马殿齐" and row["uploader_role"] == "reviewer", row
        assert row["model"] == "qwen3.8-flash", row
        assert row["reviewed_by_inputter"] is True and row["stager"] == "刘明", row
