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


def test_a_corrected_house_number_wins_over_the_parsed_one():
    """人照着票面把分单号改对并暂存之后，这一行报的必须是**他确认过的那个号**。
    从前取的是 `output/air` 那份模型原样（人改它不会变），暂存里的新号排在它后面——
    2026-10-14 现场：模型把 56115007026 抄成 BJS12113891，admin 改对并暂存，
    「名下分单」还按旧号列着一行"公司侧还没有"，看着就是改了没生效（主单那侧一直是
    `ams_final` 优先，分单这行是漏改的一处）。"""
    with _sandbox():
        import company_api
        stem = "131-35377731分单"
        # 模型两个号都抄错了：主单号抄成另一张、分单号抄成票面上那串数字
        _ticket(stem, mawb="131-00000000", hawb="BJS12113891")
        store.stage_ticket(stem, {"MAWB_NO": "131-35377731", "HAWB_NO": "56115007026",
                                  "DEST_NAME": "AMS"}, "admin", "admin")
        rows = store.ticket_rows(mawb="131-35377731")
        assert len(rows) == 1, f"按改对的主单号就该找得到这张票：{[(r['stem'], r['mawb']) for r in rows]}"
        assert rows[0]["hawb"] == "56115007026", f"行上还是模型抄错的那个号：{rows[0]}"
        assert rows[0]["mawb"] == "131-35377731", f"主单号同理：{rows[0]}"
        assert rows[0]["state"] == "staged"
        pend = company_api.local_pending("131-35377731")
        assert [(p["mawb"], p["hawb"]) for p in pend] == [("131-35377731", "56115007026")], \
            f"名下分单那张表读的就是这份名单：{pend}"


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


def _fn(js, head):
    """从 head 起切出那一个函数的 body。边界找顶格的 `}`，别拿 `index("return;")` 当终点——
    它会一路切进下一个函数，于是"这个函数里有某句话"的断言从隔壁蒙对（留档第 45 条那一族）。"""
    i = js.index(head)
    return js[i:js.index("\n}", i)]


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


def test_void_is_admin_only_and_validates():
    """作废与恢复**只对管理员开放**（2026-10-14 用户改口径："把今日归档里面的作废权只给管理员"）。
    v1.11.0 当初做成"登录就能点"是为了让制单员手边能清自己的测试数据，现在收到管理员一处：
    软闸门那套（二次确认 + 戳上记谁作废）照旧留着，只是点的人换了。"""
    with _sandbox():
        _ticket("E1"); _master("999-90273120")
        anon = TestClient(server.app)
        assert anon.post("/void", json={"kind": "hawb", "key": "E1"}).status_code == 401, \
            "没登录连门都进不来"
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
        for name, role in (("马殿齐", "reviewer"), ("刘明", "inputter")):
            auth.set_password(name, "pw-12345", role)
            c = TestClient(server.app)
            assert c.post("/login", json={"name": name, "password": "pw-12345"}).status_code == 200
            for body in ({"kind": "hawb", "key": "E1"}, {"kind": "mawb", "key": "999-90273120"},
                         {"kind": "hawb", "key": "E1", "void": False}):
                assert c.post("/void", json=body).status_code == 403, \
                    f"{name}({role}) 不该能{'恢复' if body.get('void') is False else '作废'}：{body}"
        assert store.voided_map() == {}, "被 403 挡下的那几次一个字都不该落进作废本"


def test_only_admins_get_a_void_button_in_the_day_view():
    """界面上也不许留入口：非管理员那一格什么都不画，但**已作废的行要能看懂为什么没按钮**——
    那句话写在「只看作废」那颗勾选的说明里，不在每一行上重复。"""
    js = web_src.part("js/desk.js")
    cell = web_src.fn(js, "function voidCell(r){")
    assert 'ME.role === "admin"' in cell, "作废按钮没认角色"
    assert 'if (!admin) return "";' in cell, "认了角色却没真的提前返回：判断成了摆设"
    gate = cell.index('ME.role === "admin"')
    for attr in ("data-void", "data-undo"):
        assert cell.index(attr) > gate, f"{attr} 排在角色判断前面，等于没挡"
    html = web_src.part("index.html")
    lab = html[html.index('id="dayOnlyVoid"') - 400: html.index('id="dayOnlyVoid"')]
    assert "只对管理员" in lab, "非管理员看不出这功能为什么不在了"


def test_desk_day_table_shows_the_type_and_has_a_void_button():
    """接线：类型列、作废与恢复按钮、「只看作废」开关，一个都不能少——
    只加后端不加显示，用户看到的还是那张删不掉的表。（谁能看见那颗按钮归
    `test_only_admins_get_a_void_button_in_the_day_view` 管。）"""
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
        _uploaded("票一", "admin", when=TODAY)
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
    assert "L.dayBar" in js, "计数要走 logic，别在渲染里再算一遍"
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


def test_an_empty_day_says_when_the_last_ticket_was():
    """空白一页最容易被当成坏了（用户："今天台账里面为什么没有内容了"）。
    空的那天要答出「最近有票的是哪天」，日期只能由服务端算：台账是本机的，
    而且台账是本机的、不共享，浏览器那台机器的"今天"未必等于这台机器的"今天"
    （2026-10-12 实测 VM 用的是本地时钟：processed_at == 文件 mtime == journal 时间，
    从前留档里"VM 跑 UTC"那句是错的）。"""
    with _sandbox():
        _uploaded("昨天的票", "admin", when="2026-09-30")
        _uploaded("更早的票", "admin", when="2026-09-21")
        assert store.day_last_date() == "2026-09-30"
        login = TestClient(server.app)
        login.post("/login", json={"name": "admin", "password": "admin123"})
        j = login.get("/day", params={"date": "2026-10-12"}).json()
        assert j["rows"] == [] and j["last_date"] == "2026-09-30", j
        # 有票的那天：last_date 仍是"最近有记录的一天"，不是被查询的那天
        assert login.get("/day", params={"date": "2026-09-30"}).json()["last_date"] == "2026-09-30"


def test_a_broken_qc_file_does_not_erase_the_last_date():
    """算"最近哪天有票"和列台账一样要逐行容错：一份坏 JSON 不该把整页问句变成"从没记录"。"""
    with _sandbox():
        _ticket("好票", when="2026-10-01")
        (config.OUTPUT_QC_DIR / "坏票.json").write_text("{不是 JSON", encoding="utf-8")
        assert store.day_last_date() == "2026-10-01"


def test_the_empty_day_offers_a_way_to_the_last_one():
    """光说"没票"不够：那一句要把人带到还有票的那天（点一下就行），而且日期只能用服务端给的那个。"""
    js = web_src.part("js/desk.js")
    seg = _fn(js, "function dayEmptyHtml(")
    assert "data-day=" in seg and "VIEW.lastDate" in seg, "空态里没有那个可点的日期"
    assert "这台机器还没有过任何台账记录" in seg, "从没记录过的那种空要照实说，别报一个假日期"
    h = js[js.index('$("#dayBox").addEventListener("click"'):]
    assert "[data-day]" in h[:900] and "loadDay(true)" in h[:900], "那颗日期没接上翻过去的动作"


# ── 五、提交那一天也算经手过（2026-10-12 用户："我刚刚用宛平的账号提交了几票，
#       但是在今日归档里并没有我提交的内容啊"）──────────────────────────────────────


def test_a_ticket_submitted_today_is_todays_row_even_if_it_was_parsed_earlier():
    """这一页从前只认 processed_at（解析那天）。宛平 07:24 提交的四张票是 admin 前几天传的，
    于是提交完在这页一张都找不到——人问的是「今天发了哪些票」，页面答的是「今天读了哪些票」。
    两个日期都算：解析那天照样有它（那是它被读出来的日子），提交那天也要有它。"""
    with _sandbox():
        _ticket("上周解析今天发", when="2026-08-02")
        store.mark_submitted("上周解析今天发", "999-95764373", "JRT28643", "宛平",
                             receipt={"accepted": 1}, company_action="updated")
        today = [r["stem"] for r in store.day_rows(TODAY)]
        assert "上周解析今天发" in today, "今天提交的票必须出现在今天，不然制单员提交完在这页找不到自己的活"
        assert "上周解析今天发" in [r["stem"] for r in store.day_rows("2026-08-02")], \
            "解析那天不该因为它后来被人提交就丢掉这一行"
        row = [r for r in store.day_rows(TODAY) if r["stem"] == "上周解析今天发"][0]
        assert row["processed_at"].startswith("2026-08-02") and row["submitted"]["at"].startswith(TODAY), \
            "两个时间都得留在行上：界面要说得出这一行为什么会出现在今天"


def test_a_day_that_only_borrowed_a_neighbour_activity_stays_empty():
    """反向闸门：把提交日算进来不等于「哪天都算」。只动过两天的票，中间那天必须是空的。"""
    with _sandbox():
        _ticket("中间没动过", when="2026-08-02")
        store.mark_submitted("中间没动过", "999-95764373", "JRT28643", "宛平", receipt={"accepted": 1})
        assert store.day_rows("2026-08-03") == [], "解析日与提交日之间的空档不该被填上"
        assert [r["stem"] for r in store.day_rows("2026-08-02")] == ["中间没动过"]


def test_a_ticket_parsed_and_submitted_the_same_day_appears_once():
    with _sandbox():
        _ticket("今天就今天", when=TODAY)
        store.mark_submitted("今天就今天", "999-95764373", "JRT28643", "宛平", receipt={"accepted": 1})
        assert [r["stem"] for r in store.day_rows(TODAY)] == ["今天就今天"], \
            "两条判据命中同一张票只出一条，别把一张票列成两行"


def test_the_last_day_with_tickets_counts_submissions_too():
    """空态那句「最近有票的是 X」要跟这一页同一口径：只有提交、没有解析的那一天同样算有票。"""
    with _sandbox():
        _ticket("旧票今天发", when="2026-08-02")
        store.mark_submitted("旧票今天发", "999-95764373", "JRT28643", "宛平", receipt={"accepted": 1})
        assert store.day_last_date() == TODAY, "今天提交了票却答最近有票的是八月，那句话就错了"


def test_the_day_page_says_which_two_dates_it_matches():
    """口径改了就得写在脸上：不然下一个人还是会把「今天没行」读成「今天的票丢了」。"""
    page = web_src.part("index.html")
    line = [l for l in page.splitlines() if 'id="dayDate"' in l]
    assert line and "解析" in line[0] and "提交" in line[0], "日期那颗没说要按哪两个日期算"


# ── 八、台账按人分（2026-10-12 需求："每个人记录每个人的，不能混在一起"）────────────


def _uploaded(stem, by, **kw):
    """把这张票标成"谁上传解析的"。v1.2.0 起 qc 里有这一格，老票没有——那正是归属的第三种情况。"""
    _ticket(stem, **kw)
    qc = json.loads((config.OUTPUT_QC_DIR / (stem + ".json")).read_text(encoding="utf-8"))
    qc["uploader"] = by
    store.save_qc(stem, qc)
    return stem


def _login(name, password):
    c = TestClient(server.app)
    r = c.post("/login", json={"name": name, "password": password})
    assert r.status_code == 200, name + " 登不进去：" + r.text
    return c


def test_a_ticket_belongs_to_everyone_who_touched_it():
    """定案口径「动过就算我的」：上传解析、暂存核对、提交发送三步里任一步是我做的，这张票就进
    我的今日台账。代价是同一张票会在两个人那里各出现一次——这是定案时说好的，换掉的是
    "我今天干的活在这页上看不见"。"""
    with _sandbox():
        _uploaded("K1", "马殿齐", hawb="HK1")
        store.stage_ticket("K1", {"MAWB_NO": "999-95764373", "HAWB_NO": "HK1"}, "刘明", "inputter")
        store.mark_submitted("K1", "999-95764373", "HK1", "宛平", receipt={"accepted": 1})
        for who in ("马殿齐", "刘明", "宛平"):
            assert [r["stem"] for r in store.day_rows(TODAY, who=who)] == ["K1"], \
                who + " 动过这一步，台账里却没有这张票"
        assert store.day_rows(TODAY, who="陈新") == [], "没动过的票不该出现在他的台账里"
        assert set(store.day_rows(TODAY)[0]["touched"]) == {"马殿齐", "刘明", "宛平"}, \
            "「全部」那一档要看得见这张票都有谁动过"


def test_a_master_row_belongs_to_everyone_in_its_events():
    """主单从前只把最后一个事件的人当经手人（uploader 取 events[-1]）。按「动过就算」的口径要扫
    全部事件——先改过一版的人，不该因为后面又有人改过，就从他的台账里消失。"""
    import master_pipeline
    with _sandbox():
        _master("999-90273121")
        master_pipeline.stage_master("999-90273121", {"MAWB_NO": "999-90273121", "ORIGIN_NAME": "SHA"},
                                     "马殿齐", "reviewer")
        master_pipeline.stage_master("999-90273121", {"MAWB_NO": "999-90273121", "ORIGIN_NAME": "PVG"},
                                     "刘明", "inputter")
        for who in ("马殿齐", "刘明"):
            assert [r["kind"] for r in store.day_rows(TODAY, who=who)] == ["mawb"], \
                who + " 暂存过这张主单，他的台账里必须有"
        assert store.day_rows(TODAY, who="宛平") == []


def test_an_unattributed_row_shows_only_in_the_all_view():
    """三个名字都空的老票（v1.2.0 之前上传不记人）不进任何人的「我的」，只在「全部」里出现。
    宁可让它只在全局视图里露着，也不猜一个归属——猜给管理员等于把所有老票都塞给他。"""
    with _sandbox():
        _ticket("老票没有经手人")
        assert store.day_rows(TODAY, who="admin") == [], "老票不许凭空算成管理员的"
        rows = store.day_rows(TODAY)
        assert [r["stem"] for r in rows] == ["老票没有经手人"], \
            "不传 who 必须仍是全机口径：统计与那颗「全部」读的都是它"
        assert rows[0]["touched"] == {}


def test_the_day_route_answers_for_the_logged_in_person_and_only_admin_widens():
    """过滤在服务端，不在前端：前端过滤等于把别人的行发到浏览器里，F12 就能看见。
    非管理员手敲 all=1 也拿不到——那一档认的是会话里的角色，不是查询串。"""
    with _sandbox():
        _uploaded("L1", "马殿齐", hawb="HL1")
        _uploaded("L2", "宛平", hawb="HL2")
        auth.set_password("马殿齐", "pw-12345", "reviewer")
        rev = _login("马殿齐", "pw-12345")
        j = rev.get("/day").json()
        assert j["scope"] == "mine" and [r["stem"] for r in j["rows"]] == ["L1"], j
        forced = rev.get("/day", params={"all": 1}).json()
        assert forced["scope"] == "mine" and [r["stem"] for r in forced["rows"]] == ["L1"], \
            "制单员敲 all=1 就看到别人的台账：这一档必须由服务端认角色"
        adm = _login("admin", "admin123")
        mine = adm.get("/day").json()
        assert mine["scope"] == "mine" and mine["rows"] == [], "管理员打开也先看到自己的，不是默认全局"
        wide = adm.get("/day", params={"all": 1}).json()
        assert wide["scope"] == "all" and sorted(r["stem"] for r in wide["rows"]) == ["L1", "L2"]


def test_my_empty_day_says_someone_else_has_tickets_today():
    """这一页空着有两种成因：这台机器今天真没票，和票都是别人经手的。只说"没内容"就会被读成
    丢了（2026-10-12 那句「我提交了几票但今日归档里没有」正是这么来的）。"""
    with _sandbox():
        _uploaded("M1", "马殿齐", hawb="HM1")                      # 今天，别人的
        _uploaded("M2", "宛平", when="2026-09-29", hawb="HM2")     # 我上一次动票那天
        auth.set_password("宛平", "pw-23456", "reviewer")
        rev = _login("宛平", "pw-23456")
        j = rev.get("/day", params={"date": TODAY}).json()
        assert j["rows"] == [] and j["others_today"] == 1, j
        assert j["last_date"] == "2026-09-29", "last_date 答的是「我」最近动过票的那天，不是全机的"
        assert [r["stem"] for r in rev.get("/day", params={"date": "2026-09-29"}).json()["rows"]] == ["M2"]
        quiet = rev.get("/day", params={"date": "2026-08-02"}).json()
        assert quiet["rows"] == [] and quiet["others_today"] == 0, "那天连别人都没动过票"


def test_the_scope_chip_is_role_based_and_never_remembered():
    """那颗档：按角色画哪几颗、每次打开回到默认那一档（录入员是「待我复核」，别人是「我的」）、
    换档要重新拉数据（不是在这批行里筛）。
    记住「全部」的下一次，人会把全局数当成自己的数去报——所以它不许进本机偏好。"""
    page, js = web_src.part("index.html"), web_src.part("js/desk.js")
    assert 'id="dayScope"' in page and 'data-ds="mine"' in page and 'data-ds="all"' in page, \
        "缺「我的 / 全部」那颗档"
    ls = js.split("const LS = {")[1].split("};")[0]
    assert "scope" not in ls, "那颗档不许写进本机偏好（LS）"
    assert 'DAY = {scope: ""}' in js, "初始值要留空，由角色当场给默认档——写死一个就等于替录入员选了档"
    assert "dayScopeDefault" in js and "inputter" in js[js.index("const dayScopeDefault"):], \
        "默认档按角色给：录入员一进来该看见活，不该先去摸那颗档"
    load = _fn(js, "async function loadDay(")
    assert '"all=1"' in load and "scope=" in load, "换档没重新拉数据：只在这批行里筛，筛不出别人的"
    assert "VIEW.scope =" in load and "j.scope" in load, "档上显示的是自己刚才点了什么，不是服务端真给了哪一份"
    sync = _fn(js, "function dayScopeSync(")
    assert 'role === "reviewer"' in sync and "hidden" in sync, "制单员不该看得见这几颗档（服务端同一判据）"
    assert 'role === "inputter"' in sync, "录入员与管理员各看得见哪几颗档，要在这儿分得开"
    assert "VIEW.scope" in sync, "服务端拒了那一档之后，档要退回它真给的那一份，不能留着亮着"
    empty = _fn(js, "function dayEmptyHtml(")
    assert "VIEW.others" in empty and "别人经手" in empty, "空态要答得出「票都在，但不是你的」这一种"
    assert "VIEW.machineLast" in empty, "自己从没动过、这台机器却有票时，两件事要分开说"
    assert 'data-ds="all"' in empty, "空态里那颗切到「全部」要能点，不该让人自己去摸那颗档"
    h = js[js.index('$("#dayBox").addEventListener("click"'):]
    assert '[data-ds]' in h[:1200] and "DAY.scope" in h[:1200], "空态里那颗切换没接上"


def test_the_empty_day_picks_its_sentence_in_the_pure_layer():
    """四类空态各说各的话，而"是哪一类"必须在纯函数里判：判据写在渲染那段里时，
    把分支条件改成 if(false) 静态用例根本看不出来（文案还挂在代码里，只是永远走不到）。"""
    base = {"scope": "mine", "others": 0, "lastDate": "", "date": "2026-10-12", "machineLast": ""}
    got = lambda **kw: web_src.logic("L.dayEmpty(x)", x=dict(base, **kw))
    assert got(others=3) == "others", "今天有票但不是我的：要说出那几张是别人经手的"
    assert got(lastDate="2026-09-29") == "last"
    assert got(machineLast="2026-10-11") == "never"
    assert got() == "none"
    assert got(others=5, scope="all") == "none", "已经在看全部了还说「别人经手」，是把人往回支"
    assert got(lastDate="2026-10-12") == "none", "最近那天就是正在看的这天，不该再给一个指向本页的链接"
    body = _fn(web_src.part("js/desk.js"), "function dayEmptyHtml(")
    for kind in ("others", "last", "never", "none"):
        assert kind + ":" in body, "渲染里少了「" + kind + "」那一支：判据分四类，界面只画得出三类"


def test_a_persons_day_lists_only_what_he_actually_did_that_day():
    """日期那一格认的是**这个人哪天动过它**，不是"这张票哪天被动过"。两者混起来会出这样的事：
    马殿齐 9-29 上传、宛平今天提交，于是马殿齐的"今天"里冒出一张他今天根本没碰过的票。
    他要的"今天"是他的工作日，不是这张票的流水。"""
    with _sandbox():
        _uploaded("N1", "马殿齐", when="2026-09-29", hawb="HN1")
        store.mark_submitted("N1", "999-95764373", "HN1", "宛平", receipt={"accepted": 1})
        assert [r["stem"] for r in store.day_rows("2026-09-29", who="马殿齐")] == ["N1"]
        assert store.day_rows(TODAY, who="马殿齐") == [], "他今天没动过这张票，今天那一页就不该有它"
        assert [r["stem"] for r in store.day_rows(TODAY, who="宛平")] == ["N1"], "今天是他提交的"
        assert store.day_rows("2026-09-29", who="宛平") == []
        assert sorted(store.day_rows(TODAY)[0]["touched"]["马殿齐"]) == ["2026-09-29"], \
            "行上要看得见每个人各在哪天动过手"


def test_a_master_row_lands_on_the_day_its_own_event_happened():
    """主单同一张可以有多个事件、各在不同日子：每个人只该在自己的那些日子那页看见它。"""
    import master_pipeline
    with _sandbox():
        _master("999-90273122")
        master_pipeline.stage_master("999-90273122", {"MAWB_NO": "999-90273122"}, "马殿齐", "reviewer")
        rec = json.loads((config.MASTER_DIR / "99990273122.json").read_text(encoding="utf-8"))
        rec["events"][0]["at"] = "2026-09-29T10:00:00"
        (config.MASTER_DIR / "99990273122.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        master_pipeline.stage_master("999-90273122", {"MAWB_NO": "999-90273122", "ORIGIN_NAME": "PVG"},
                                     "宛平", "reviewer")
        assert store.day_rows("2026-09-29", who="马殿齐") and not store.day_rows(TODAY, who="马殿齐")
        assert store.day_rows(TODAY, who="宛平") and not store.day_rows("2026-09-29", who="宛平")


# ── 九、一次请求别扫四遍（2026-10-13 效率审查 P1-1）────────────────────────


def test_the_day_route_scans_the_ledger_twice_not_four_times():
    """从前一次 /day 内部跑四遍全量扫描：day_rows(d) 两遍（一遍取行、一遍数"别人经手几张"），
    再加 day_last_date 两遍——而它自己内部又各走一遍 day_rows。今天的量看不出问题，但这份成本
    按"这台机器经手过的总票数"涨，不是按今天的票数涨。压成两遍：那一天扫一次（按人筛在内存里做，
    判据仍取自 store 那一处），两句"最近有票的是哪天"由一次不限日的扫描派生。"""
    with _sandbox():
        _uploaded("S1", "马殿齐", hawb="HS1")
        store.mark_submitted("S1", "999-95764373", "HS1", "宛平", receipt={"accepted": 1})
        n = {"rows": 0, "last": 0}
        real_rows, real_last = store.day_rows, store.day_last_date

        def spy_rows(*a, **kw):
            n["rows"] += 1
            return real_rows(*a, **kw)

        def spy_last(*a, **kw):
            n["last"] += 1
            return real_last(*a, **kw)

        store.day_rows, store.day_last_date = spy_rows, spy_last
        try:
            j = _login("admin", "admin123").get("/day", params={"date": TODAY}).json()
        finally:
            store.day_rows, store.day_last_date = real_rows, real_last
        assert j["scope"] == "mine" and j["rows"] == [] and j["others_today"] == 1, j
        assert j["machine_last_date"] == TODAY and j["last_date"] == "", j
        assert n["rows"] <= 2, f"台账页扫了 {n['rows']} 遍：只该是「那一天」+「不限日」各一遍"
        assert n["last"] == 0, "端点里调 day_last_date 等于把扫描翻倍——它内部还走一遍 day_rows"


def test_the_widest_view_does_not_pay_for_the_count_it_needs():
    """管理员看「全部」时不需要数"还有几张是别人的"，那份扫描一次都不该多付；
    而两句日期仍要答得出来（空态那一句话靠它）。"""
    with _sandbox():
        _uploaded("S2", "马殿齐", hawb="HS2")
        n = {"rows": 0}
        real_rows = store.day_rows

        def spy_rows(*a, **kw):
            n["rows"] += 1
            return real_rows(*a, **kw)

        store.day_rows = spy_rows
        try:
            j = _login("admin", "admin123").get("/day", params={"date": TODAY, "all": 1}).json()
        finally:
            store.day_rows = real_rows
        assert j["scope"] == "all" and [r["stem"] for r in j["rows"]] == ["S2"], j
        assert j["others_today"] == 0 and j["last_date"] == TODAY, j
        assert n["rows"] <= 2, f"「全部」那一档扫了 {n['rows']} 遍"


def test_latest_day_from_one_scan_answers_exactly_what_the_old_one_did():
    """端点里自己算的那句"最近有票的是哪天"必须与 store.day_last_date 一字不差：
    两处算术迟早给出两个日期，而这句话是回答"没票还是坏了"的，说错比不说更糟。"""
    with _sandbox():
        _uploaded("E1", "马殿齐", when="2026-09-29", hawb="HE1")
        _uploaded("E2", "宛平", when="2026-10-02", hawb="HE2")
        every = store.day_rows()
        for who in (None, "宛平", "马殿齐", "陈新"):
            assert store.day_latest(every, who) == store.day_last_date(who), str(who)
        assert store.day_latest(every) == "2026-10-02", every and [r["processed_at"] for r in every]
        assert store.day_latest(every, "宛平") == "2026-10-02"
        assert store.day_latest(every, "马殿齐") == "2026-09-29", \
            "他最近一次动手是 9-29，10-02 那张不是他的，不能跟着涨"
        assert store.day_latest(every, "陈新") == ""


def test_the_endpoint_and_the_scanner_share_one_ownership_rule():
    """「动过就算我的」这条判据只能有一处（store.owns）：端点为了少扫一遍而在内存里筛行，
    自己再写一遍判据的话，筛出来的和扫出来的迟早不是同一批票。"""
    with _sandbox():
        _uploaded("P1", "马殿齐", when="2026-09-29", hawb="HP1")
        store.mark_submitted("P1", "999-95764373", "HP1", "宛平", receipt={"accepted": 1})
        every = store.day_rows("2026-09-29")
        assert [r["stem"] for r in every if store.owns(r, "马殿齐", "2026-09-29")] == ["P1"]
        assert [r["stem"] for r in every if store.owns(r, "宛平", "2026-09-29")] == [], \
            "宛平是今天提交的，9-29 那一页不该有他"
        assert [r["stem"] for r in store.day_rows("2026-09-29", who="马殿齐")] == ["P1"], \
            "内存筛与逐行筛必须同一批"


# ── 录入员那一档：待我复核 / 我核过·待提交 / 跨天号检索 ─────────────────────────
def _mk(stem, mawb="999-95764373", hawb="JRT28643", when=TODAY, stage=None, review=None, sent=False):
    """在盘上摆一张票，并（按需）让它被制单员暂存过、被录入员复核过、被提交走。"""
    _ticket(stem, mawb=mawb, hawb=hawb, when=when)
    (config.ARCHIVE_DIR / stem).mkdir(exist_ok=True)
    if stage:
        store.stage_ticket(stem, {"MAWB_NO": mawb, "HAWB_NO": hawb}, stage, "reviewer")
    if review:
        store.review_ticket(stem, review, "inputter")
    if sent:
        store.mark_submitted(stem, mawb, hawb, "马殿齐", {"mode": "mock"})


def test_the_review_list_is_every_unsubmitted_ticket_no_inputter_has_touched():
    """录入员的活清单不是"今天的台账"：别人昨天暂存的票今天还得核。
    判据与提交门同源（`reviewed_by_inputter`），也不许把已经发走的票算进来。"""
    with _sandbox():
        _mk("只解析的")
        _mk("制单员暂存的", stage="马殿齐")
        _mk("已复核待提交的", stage="马殿齐", review="郭健康")
        _mk("已经发走的", stage="马殿齐", sent=True)
        auth.set_password("郭健康", "pw-12345", "inputter")
        inp = _login("郭健康", "pw-12345")
        j = inp.get("/day", params={"scope": "toreview"}).json()
        assert j["scope"] == "toreview", j["scope"]
        assert {r["stem"] for r in j["rows"]} == {"只解析的", "制单员暂存的"}, \
            f"待复核 = 未提交且没录入员碰过：{[r['stem'] for r in j['rows']]}"
        j2 = inp.get("/day", params={"scope": "reviewed"}).json()
        assert [r["stem"] for r in j2["rows"]] == ["已复核待提交的"], j2["rows"]


def test_a_reviewer_asking_for_the_review_list_still_gets_only_his_own():
    """这一档是给录入员的：制单员带 scope=toreview 不该看见全机别人的票。
    服务端退回他自己那一份并把 scope 如实报成 mine——前端认的就是这一格。"""
    with _sandbox():
        _mk("甲的票", stage="马殿齐")
        _mk("乙的票", stage="刘明")
        auth.set_password("马殿齐", "pw-12345", "reviewer")
        rev = _login("马殿齐", "pw-12345")
        j = rev.get("/day", params={"scope": "toreview"}).json()
        assert j["scope"] == "mine", f"制单员拿不到复核档：{j['scope']}"
        assert all("马殿齐" in str(r.get("stager")) or "马殿齐" in str(r.get("uploader"))
                   for r in j["rows"]), j["rows"]
        adm = _login("admin", "admin123")
        assert adm.get("/day", params={"scope": "toreview"}).json()["scope"] == "toreview", \
            "管理员要能替录入员看这一档（他平时就是这么核的）"


def test_a_number_search_finds_the_ticket_on_any_day_and_in_any_format():
    """号检索：录入员手里只有一个单号，不该先猜它是哪天经手的。
    归一化与主单侧同一处（`store.norm_no`），带不带连字符都得命中。"""
    with _sandbox():
        _mk("八月的票", mawb="235-96146363", hawb="CLA26090022", when="2026-08-02")
        _mk("今天的票", mawb="999-95764373", hawb="JRT28643")
        auth.set_password("郭健康", "pw-12345", "inputter")
        inp = _login("郭健康", "pw-12345")
        for q in ("23596146363", "235-96146363", "cla26090022"):
            j = inp.get("/day", params={"q": q}).json()
            assert [r["stem"] for r in j["rows"]] == ["八月的票"], f"q={q} 命中错了：{[r['stem'] for r in j['rows']]}"
        assert inp.get("/day", params={"q": "JRT28643"}).json()["rows"], "别只命中主单号，分单号也要"
        # 只给号尾那几个字符也要命中：整号相等的话，"查无此号"会被读成"票丢了"
        assert [r["stem"] for r in inp.get("/day", params={"q": "96146363"}).json()["rows"]] == ["八月的票"]
        assert inp.get("/day", params={"q": "99"}).json()["rows"] == [], \
            "太短的号不做截段匹配：一个「99」命中全表，这一格就废了"


def test_a_number_search_refuses_a_path_and_a_run_on():
    with _sandbox():
        auth.set_password("郭健康", "pw-12345", "inputter")
        inp = _login("郭健康", "pw-12345")
        assert inp.get("/day", params={"q": "a/b"}).status_code == 400
        assert inp.get("/day", params={"q": "x" * 300}).status_code == 400, \
            "超长不挡的话，Linux 上 stat() 会抛 ENAMETOOLONG 而不是干净地查无"


def test_the_inputter_gets_a_review_list_a_search_box_and_a_review_mark():
    """录入员那一档的三样东西都在"今日归档"这一页上：两档复核清单、按号跨天检索、
    以及票上那颗「我核过了，先不发」。筛与算都在服务端，前端只负责把点的这一档说出去
    （在浏览器里筛等于把别人的票发到他的 F12 里，而页上写的还是"只看待复核"）。"""
    js, page = web_src.part("js/desk.js"), web_src.part("index.html")
    assert 'id="dayQ"' in page, "台账页要有按号检索那一格"
    assert 'data-ds="toreview"' in page and 'data-ds="reviewed"' in page, "两档复核清单要真画出来"
    ld = web_src.fn(js, "async function loadDay(")
    assert "scope=" in ld and "q=" in ld, "点哪一档就把哪一档交给服务端，别在浏览器里筛"
    assert "VIEW.scope =" in ld and "j.scope" in ld, "档认服务端说的那一份：拿不到那一档时要能退回"
    assert 'if (kw && (s === "toreview" || s === "reviewed")) s = "all"' in ld, \
        "输了号就回到「全部」：在当前那一档里筛出来的空页会写成「查无此号」，那是假话"
    sync = web_src.fn(js, "function dayScopeSync(")
    assert "inputter" in sync, "录入员与管理员各看得见哪几档由角色决定"
    assert "markReviewed" in js and '"/review"' in js, "票上要有那颗只留痕、不暂存、不碰公司的按钮"


def test_the_empty_day_page_says_which_of_the_four_thing_it_is():
    """空页必须自己答"是没票还是坏了"。新增的两档与号检索各有一句：
    待复核为空是好消息（都核完了），查无此号是另一件事——都写成"今天没票"就把人支走了。"""
    e = lambda **v: web_src.logic("L.dayEmpty(v)", v=v)
    assert e(scope="toreview", rows=[], date="", others=0, lastDate="", machineLast="") == "clean"
    assert e(scope="all", q="CLA26090022", rows=[], date="", others=0,
             lastDate="2026-10-01", machineLast="2026-10-01") == "nohit"
    assert e(scope="mine", rows=[], date="2026-10-14", others=3,
             lastDate="2026-10-14", machineLast="2026-10-14") == "others", "老句子不许被新档挤掉"


def test_a_day_page_scans_the_directories_once_not_twice():
    """/day 按天那两档（mine/all）从前把同一份全量目录扫做两遍：第一遍 day_rows(d) 出
    「这一天」，第二遍 day_rows() 出「最近有票是哪天」——而 ticket_rows/master_rows 本来就是
    全量读、按日期内存筛，第二遍是纯白扫（2026-10-17 效率审查 B1）。
    数的是 day_rows 调用次数：一次请求一遍。语义一个字段都不许漂——行、last_date、
    others_today 与两遍时代一致，同文件里那些 /day 用例钉的就是它们。"""
    with _sandbox():
        _uploaded("D1", "admin", when="2026-09-30")
        _uploaded("D2", "admin", when="2026-09-21")
        calls = []
        real = store.day_rows

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        store.day_rows = counting
        try:
            c = _login("admin", "admin123")
            j = c.get("/day", params={"date": "2026-09-30"}).json()
            assert [r["stem"] for r in j["rows"]] == ["D1"], j
            assert j["last_date"] == "2026-09-30" and j["others_today"] == 0, j
            assert len(calls) == 1, f"按天那页把目录扫了 {len(calls)} 遍：一遍就够"
            calls.clear()
            j2 = c.get("/day", params={"q": "D1"}).json()      # 号检索那档（flat）也要是一遍
            assert [r["stem"] for r in j2["rows"]] == ["D1"], j2
            assert len(calls) == 1, f"号检索那档扫了 {len(calls)} 遍"
        finally:
            store.day_rows = real
