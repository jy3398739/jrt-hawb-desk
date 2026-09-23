# -*- coding: utf-8 -*-
"""提交回传链路（mock 回传 + 提交台账 + 主/分单号复合键索引）回归。
不产生任何外部调用：_company_submit 本就是 mock，测试也不碰网络。
覆盖：单号归一化 / 台账原子写 / 索引派生 / /submit 门禁（缺号 401→登录、缺号 400、干净进台账）/ 前端提交门存在。
"""
import json
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import server
import store

WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


def _clean_ticket(stem="CLA26090022", mawb="235-96146363", hawb="CLA26090022"):
    return {"stem": stem, "filename": stem + ".pdf", "reviewer": "马殿齐",
            "air_reviewed": {"MAWB_NO": mawb, "HAWB_NO": hawb, "SHIPPET": ""}}


def test_norm_no_and_composite_key():
    assert store.norm_no("235-96146363") == "23596146363"
    assert store.norm_no(" cla 26090022 ") == "CLA26090022"
    # 连字符/空格/大小写差异归一后同键 → 公司与票面写法不一致也能对上
    assert store.number_key("235-96146363", "cla26090022") == "23596146363|CLA26090022"
    assert store.number_key("235 96146363", "CLA-26090022") == store.number_key("23596146363", "cla26090022")


def test_mark_submitted_writes_ledger_and_index_resolves_stem():
    old_ledger = config.SUBMIT_LEDGER
    tmp = Path(tempfile.mkdtemp(prefix="hawb_ledger_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    try:
        assert store.ledger() == {}
        store.mark_submitted("CLA26090022", "235-96146363", "CLA26090022", "马殿齐", {"mode": "mock"})
        led = store.ledger()
        assert "CLA26090022" in led
        assert led["CLA26090022"]["key"] == "23596146363|CLA26090022"
        assert led["CLA26090022"]["reviewer"] == "马殿齐"
        # 复合键 → stem；号写法差异归一后照样命中
        assert store.lookup_stem("23596146363", "cla26090022") == "CLA26090022"
        # 未提交的号查不到
        assert store.lookup_stem("000-00000000", "NOPE") is None
    finally:
        config.SUBMIT_LEDGER = old_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def test_remark_submitted_overwrites_same_stem():
    old_ledger = config.SUBMIT_LEDGER
    tmp = Path(tempfile.mkdtemp(prefix="hawb_ledger_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    try:
        store.mark_submitted("TAO1", "176-11111111", "H1", "宛平", {"mode": "mock"})
        store.mark_submitted("TAO1", "176-11111111", "H1", "陈新", {"mode": "mock"})   # 重提交换复核人
        led = store.ledger()
        assert led["TAO1"]["reviewer"] == "陈新", "同 stem 再提交以最新为准"
        assert len(led) == 1
    finally:
        config.SUBMIT_LEDGER = old_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def _logged_client(tmp_users: Path):
    """临时 users.json（种子 admin）+ 已登录的 TestClient；调用方负责清理 tmp_users。"""
    auth.USERS_FILE = tmp_users / "users.json"
    auth.ensure_seed()
    client = TestClient(server.app)
    r = client.post("/login", json={"name": "admin", "password": "admin123"})
    assert r.status_code == 200, f"默认管理员登录失败：{r.status_code} {r.text}"
    return client


def test_submit_requires_login():
    old_ledger = config.SUBMIT_LEDGER
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    old_users = auth.USERS_FILE
    auth.USERS_FILE = tmp / "users.json"
    try:
        r = TestClient(server.app).post("/submit", json={"tickets": [_clean_ticket()]})
        assert r.status_code == 401, "提交要登录会话"
    finally:
        auth.USERS_FILE = old_users
        config.SUBMIT_LEDGER = old_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def test_submit_clean_writes_ledger_mock_receipt():
    old_ledger = config.SUBMIT_LEDGER
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    old_users = auth.USERS_FILE
    try:
        client = _logged_client(tmp)
        r = client.post("/submit", json={"tickets": [_clean_ticket()]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["results"][0]["mode"] == "mock"
        assert store.ledger()["CLA26090022"]["key"] == "23596146363|CLA26090022"
        assert store.lookup_stem("235-96146363", "CLA26090022") == "CLA26090022"
    finally:
        auth.USERS_FILE = old_users
        config.SUBMIT_LEDGER = old_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def test_submit_rejects_missing_mawb_or_hawb():
    old_ledger = config.SUBMIT_LEDGER
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    old_users = auth.USERS_FILE
    try:
        client = _logged_client(tmp)
        for bad in (_clean_ticket(mawb=""), _clean_ticket(hawb=""), _clean_ticket(mawb="  -- ")):
            r = client.post("/submit", json={"tickets": [bad]})
            assert r.status_code == 400, "主单号或分单号为空应拒绝提交"
        assert store.ledger() == {}, "被拒的票绝不能进台账/索引"
    finally:
        auth.USERS_FILE = old_users
        config.SUBMIT_LEDGER = old_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def test_submit_records_acked_flags_in_ledger():
    """/submit 收到 acked_flags（复核员点『确认无误』放行的旗）要原样进台账留痕；不传则为空表。"""
    old_ledger = config.SUBMIT_LEDGER
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    old_users = auth.USERS_FILE
    try:
        client = _logged_client(tmp)
        acked = ['CONSIGNEE 电话区号 +39(IT) 与国家 IS 不一致，疑似电话串边']
        r = client.post("/submit", json={"tickets": [dict(_clean_ticket(), acked_flags=acked)]})
        assert r.status_code == 200, r.text
        led = store.ledger()["CLA26090022"]
        assert led["acked_flags"] == acked, "确认过的红旗原文要进台账备查"
        # 再提交一票不带 acked_flags → 空表，不出错
        r = client.post("/submit", json={"tickets": [_clean_ticket(stem="TAO2", hawb="TAO2")]})
        assert r.status_code == 200, r.text
        assert store.ledger()["TAO2"]["acked_flags"] == []
    finally:
        auth.USERS_FILE = old_users
        config.SUBMIT_LEDGER = old_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def test_desk_has_submit_gate_on_flags_and_numbers():
    """/submit 之外，前端也得有提交门：缺号或未清红旗时拦住，别让人点了才吃 400。"""
    html = WEB.read_text(encoding="utf-8")
    assert "主单号、分单号都得填上才能提交回传公司" in html
    assert "还有未清的红旗" in html


def test_desk_has_flag_ack_button_and_payload():
    """存疑类红旗的出口是人工确认：chip 上有『确认无误』按钮，payload 带 acked_flags。"""
    html = WEB.read_text(encoding="utf-8")
    assert "确认无误" in html
    assert "acked_flags" in html
