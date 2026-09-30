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
