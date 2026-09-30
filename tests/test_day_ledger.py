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
    """输出目录、台账与账号表全挪进临时目录：这一页读的是本机磁盘，不能碰真 output/，
    更不能依赖运维机上那份可能已改过 admin123 的 users.json。"""
    old = (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
           config.ARCHIVE_DIR, config.SUBMIT_LEDGER, auth.USERS_FILE)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR = tmp / "raw", tmp / "air"
        config.OUTPUT_QC_DIR, config.ARCHIVE_DIR = tmp / "qc", tmp / "archive"
        config.SUBMIT_LEDGER = tmp / "submitted.json"
        auth.USERS_FILE = tmp / "users.json"
        for p in (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
                  config.ARCHIVE_DIR):
            p.mkdir()
        try:
            yield tmp
        finally:
            (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
             config.ARCHIVE_DIR, config.SUBMIT_LEDGER, auth.USERS_FILE) = old


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
    算错一次就没人再信这一页；所以放 logic.js 由 node 断输入输出。"""
    rows = [{"submitted": {"reviewer": "马殿齐"}, "failed": False, "flags": 2},
            {"submitted": None, "failed": False, "flags": 0},
            {"submitted": None, "failed": True, "flags": 0},
            {"submitted": {"reviewer": "宛平"}, "failed": False, "flags": 1}]
    assert web_src.logic("L.daySummary(rs)", rs=rows) == {
        "total": 4, "submitted": 2, "pending": 2, "failed": 1, "flags": 3}
    assert web_src.logic("L.daySummary(rs)", rs=[]) == {
        "total": 0, "submitted": 0, "pending": 0, "failed": 0, "flags": 0}


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

