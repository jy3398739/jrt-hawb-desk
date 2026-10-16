# -*- coding: utf-8 -*-
"""重开已归档分单：录入员在「名下分单」里点分单号 → 后端按 stem 交回落盘结果 → 审核台进核对页。
全部本地文件，不碰公司接口也不调模型。
"""
import contextlib
import json
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import server
import web_src
import store


@contextlib.contextmanager
def _sandbox():
    """输出目录与账号/台账都挪进临时目录，测试不碰真 output/ 与真 users.json。"""
    old = (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
           config.TRANSCRIPT_DIR, config.ARCHIVE_DIR, config.SUBMIT_LEDGER, auth.USERS_FILE)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_tk_"))
    try:
        config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR = tmp / "raw", tmp / "air"
        config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR = tmp / "qc", tmp / "transcript"
        config.ARCHIVE_DIR = tmp / "archive"
        config.SUBMIT_LEDGER = tmp / "submitted.json"
        auth.USERS_FILE = tmp / "users.json"
        for p in (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
                  config.TRANSCRIPT_DIR):
            p.mkdir()
        yield tmp
    finally:
        (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
         config.TRANSCRIPT_DIR, config.ARCHIVE_DIR, config.SUBMIT_LEDGER,
         auth.USERS_FILE) = old
        shutil.rmtree(tmp, ignore_errors=True)


RAW = {"MAWB_NO": "999-95764373", "HAWB_NO": "VCE4373",
       "SHIPPER_INFO": "QINGDAO TEXTILE CO LTD", "CONSIGNEE_INFO_CITTY": "MILAN"}
AIR = {"MAWB_NO": "999-95764373", "HAWB_NO": "VCE4373", "ORIGIN_NAME": "TAO", "DEST_NAME": "MXP"}
QC = {"source_name": "999-95764373分 - 副本.pdf", "channel": "vlm", "elapsed": 8.0,
      "needs_review": False, "flags": [], "fidelity": {"checked": 4, "passed": 4, "failed": []}}


def _web():
    auth.ensure_seed()
    u = auth.set_password("宛平", "tk-123456", "reviewer")[0]
    c = TestClient(server.app)
    r = c.post("/login", json={"name": u["name"], "password": "tk-123456"})
    assert r.status_code == 200, f"登录失败：{r.status_code} {r.text}"
    return c


def _put(stem: str):
    (config.OUTPUT_RAW_DIR / f"{stem}.json").write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    (config.OUTPUT_AIR_DIR / f"{stem}.json").write_text(json.dumps(AIR, ensure_ascii=False), encoding="utf-8")
    (config.OUTPUT_QC_DIR / f"{stem}.json").write_text(json.dumps(QC, ensure_ascii=False), encoding="utf-8")


def test_ticket_route_returns_the_archived_ticket():
    """按 stem 拿回一张票：L2 原文口径 + L3 航空口径 + 质检记录，字段名与落盘一致（前端直接塞进票据模型）。"""
    with _sandbox():
        _put("999-95764373分 - 副本")
        j = _web().get("/ticket/999-95764373分 - 副本")
        assert j.status_code == 200, j.text
        d = j.json()
        assert d["stem"] == "999-95764373分 - 副本"
        assert d["raw"] == RAW and d["air"] == AIR, "两套口径原样交回，不在路由里重算"
        assert d["qc"]["flags"] == [] and d["qc"]["channel"] == "vlm"
        assert d["filename"] == "999-95764373分 - 副本.pdf", "文件名取自 QC，列表里认得出是哪张"
        assert d["submitted"] is None, "没提交过的票不该带提交态"


def test_ticket_route_carries_the_submit_record():
    """提交过的票要把台账条目带回去：前端据此标「已提交」，否则重开一张已提交的票会被当成新票再点一次。"""
    with _sandbox():
        stem = "999-95764373分 - 副本"
        _put(stem)
        store.mark_submitted(stem, "999-95764373", "VCE4373", "马殿齐", {"mode": "live"})
        r = _web().get("/ticket/" + stem)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["submitted"], f"已提交的票该带提交记录: {d}"
        assert d["submitted"]["reviewer"] == "马殿齐"


GOOD_RAW = dict(RAW, PIECES=12, WEIGHT=345.5, CREATE_TIME="2026-09-20")
SENT_OK = {"MAWB_NO": "999-95764373", "HAWB_NO": "VCE4373", "ORIGIN_NAME": "TAO", "DEST_NAME": "MXP",
           "TO1": "PVG", "PIECES": 12, "WEIGHT": 345.5, "CREATE_TIME": "2026-09-20"}


def _put_sent(stem, air_sent, stale):
    """落一张票：解析时那批旗是 `stale`（对着模型那一版算的），台账里存的是发出去的那一版终值。"""
    (config.OUTPUT_RAW_DIR / f"{stem}.json").write_text(json.dumps(GOOD_RAW, ensure_ascii=False), encoding="utf-8")
    (config.OUTPUT_AIR_DIR / f"{stem}.json").write_text(json.dumps(AIR, ensure_ascii=False), encoding="utf-8")
    qc = dict(QC, flags=stale, needs_review=bool(stale))
    (config.OUTPUT_QC_DIR / f"{stem}.json").write_text(json.dumps(qc, ensure_ascii=False), encoding="utf-8")
    store.mark_submitted(stem, "999-95764373", "VCE4373", "马殿齐", {"mode": "live"}, air_sent=air_sent)


def test_a_submitted_ticket_gets_flags_recomputed_on_the_company_values():
    """交出去的票第二次点进去还催红旗，是因为屏上显示的是**解析那一刻**对着模型值算的那批旗
    （用户 2026-10-16："已经提交的分单和主单为什么我第二次点进去还是提示我有红旗？我的理解是
    已经提交的票应该从公司里提取内容"）。值早在 v1.26.0 就按台账那份还原了，旗没有——
    这一条把旗也按公司现值现算，判据仍用提交门那一个函数（不另起一处算法）。"""
    with _sandbox():
        stem = "999-95764373分 - 副本"
        stale = ["L3 TO1 未转成 IATA 三字码: 'MILAN'（码表缺项，需补 CITY_IATA）",
                 "保真 TO1: 未在L1逐字转录中找到原文（可能被改写或转录遗漏）（值='MILAN'）"]
        _put_sent(stem, SENT_OK, stale)
        d = _web().get("/ticket/" + stem).json()
        assert d["qc"]["flags"] == stale, "落盘那份不改：解析质量分还是按解析时算，两处不许混"
        assert d["flags_now"] == [], d["flags_now"]
        assert d["flags_now_at"], f"要说清这批旗是按哪一版算的: {d}"


def test_a_company_value_problem_still_raises_its_flag_after_submit():
    """重算不是「交了就闭嘴」：公司那一版终值里真有形态问题（TO1 还写着城市名）就照旧报红。"""
    with _sandbox():
        stem = "999-95764373分 - 副本"
        _put_sent(stem, dict(SENT_OK, TO1="MANAUS"), ["PIECES 缺失或为0"])
        d = _web().get("/ticket/" + stem).json()
        assert any("TO1" in f for f in d["flags_now"]), d["flags_now"]
        assert not any(str(f).startswith("保真") for f in d["flags_now"]), \
            "逐字保真查的是「这次解析抄得对不对」，人改过的值不该拿它回查——它只对解析负责"


def test_an_unsubmitted_ticket_has_no_recomputed_flags():
    """没提交过的票不许有这份：那时"公司现值"还不存在，屏上就该看解析时那批旗。"""
    with _sandbox():
        stem = "999-95764373分 - 副本"
        _put(stem)
        d = _web().get("/ticket/" + stem).json()
        assert d["submitted"] is None and d["flags_now"] is None, d


def test_reopening_a_submitted_ticket_shows_the_companys_flags():
    """后端算好了不算数——前端必须真的用它。这条钉的是"重开一张交出去的票，屏上那批旗
    来自公司现值那份"，以及顶栏要写清是按哪一版算的（同一条改动的浏览器半边）。"""
    html = web_src.desk()
    op = _html_fn(html, "openHouse")
    assert "if (Array.isArray(d.flags_now)){" in op, "openHouse 没接住后端现算的那份旗"
    assert "t.qc = Object.assign({}, t.qc || {}, {flags: d.flags_now, needs_review: d.flags_now.length > 0});" in op
    assert 't.flagsNow = d.flags_now_at || "";' in op
    head = _html_fn(html, "renderMain")
    assert "红旗按公司现值算（提交于 " in head, "顶栏要说清这批旗是按哪一版算的，别让人以为还是解析时那份"


def test_ticket_route_rejects_unknown_and_bad_names():
    with _sandbox():
        c = _web()
        assert c.get("/ticket/没这张票").status_code == 404, "本机没落过结果就是 404，别拼路径去找"
        assert c.get("/ticket/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404), "带路径分隔的 stem 一律不认"


def test_ticket_route_requires_login():
    with _sandbox():
        _put("999-95764373分 - 副本")
        assert TestClient(server.app).get("/ticket/999-95764373分 - 副本").status_code == 401, "只认登录会话"


def _html_fn(html, name):
    start = html.index("function " + name + "(")
    nxt = html.find("\nfunction ", start + 1)
    return html[start:nxt if nxt > 0 else len(html)]


def test_desk_strip_hawb_number_opens_that_ticket():
    """名下分单行的分单号要能点：点了就拉 /ticket/{stem} 进核对页（与「送入主单核对」同一套落位），
    已经在列表里的票直接选中，不重复拉。没归档的行不给链接。"""
    html = web_src.desk()
    assert "function openHouse(" in html, "要有 openHouse(stem) 这条装载路径"
    tab = _html_fn(html, "mstTable")
    assert "data-open" in tab, "分单号要挂 data-open=stem"
    assert "本机无归档" in tab, "没有 stem 的行不给可点的号"
    op = _html_fn(html, "openHouse")
    assert "/ticket/" in op, "要向后端取落盘结果，不是只在本地暂存里翻"
    assert "S.tickets.push" in op and "S.sel" in op, "取回应进票据列表并选中"
    assert "mvOff()" in op, "打开分单要退出主单视图，并停掉那张主单的轮询"
    assert "loadPreview" in op or "render()" in op, "落位后要把票面与核对区渲染出来"
    assert '$("#mstHawbBox").addEventListener' in html, "整宽条上要有 openHouse 的点击委托"


def test_reopening_a_ticket_takes_values_and_acks_from_the_server():
    """重开一张交出去的票：值认「人最后定下来的那一版」（提交台账 air_sent / 暂存 air_final），
    确认也认那一份。只盯模型那一版本机草稿，等于把人改过的值和点过的确认当没发生过——
    2026-10-15 现场：已提交的分单重开，发货人整串退回模型那一版，按铮写「仍有 13 项待核对」。"""
    op = web_src.fn(web_src.desk(), "async function openHouse(")
    assert "L.serverState" in op, "要走「服务器那份赢」那一条算式，不是只翻 /staged"
    assert "skipE:sf.skippedEmpty" in op, "提交时跳过的空栏单独带回去：它不算待办，也不算已确认"
    assert "airE:mg.airE" in op and "ackF:uni(dr.ackF, sf.acked)" in op, "改动与确认都要合并回来"


def test_a_row_without_a_number_says_so_instead_of_showing_nothing():
    """号没解析出来的那一行不许画成一片空白（2026-10-15 用户："分单号是空的为什么会是空的"）。
    空白看着像"这行没内容"，可它其实是一个能点开的按钮——人该看见的是"这张票缺号、点进去补"，
    不是一块看不见的热点。有 stem 给可点的那句，没 stem 给同一句话但不做链接。"""
    tab = _html_fn(web_src.desk(), "mstTable")
    assert 'r.hawb || "没解析出分单号"' in tab, "空号那一格要有字，且可点与不可点两处都用它"
    assert "点开这张票" in tab, "没号的按钮上那句提示要说清点进去干什么（补号），别只写核对页"
