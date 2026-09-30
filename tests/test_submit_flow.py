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
import web_src
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


def test_server_recomputes_house_flags_and_blocks_unacked():
    """P0-2：分单提交的红旗以**服务端重算**为准。前端报 needs_review=false、flags=[] 不算数——
    绕开或直接复用前端就能把未清的旗写进公司库，台账上的署名也是自填的（2026-09-29 审计）。"""
    old = (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR, config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.SUBMIT_GUARD_DIR = tmp / "submit_guard"
    config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR = tmp / "nol2", tmp / "notrans"
    old_users = auth.USERS_FILE
    try:
        client = _logged_client(tmp)
        tk = _clean_ticket()
        tk["air_reviewed"].update({"DEST_NAME": "WARSAW",       # L3：城市名没转成三字码
                                   "ORIGIN_NAME": "TAO"})
        tk["raw_original"] = {"MAWB_NO": "235-96146363", "HAWB_NO": "CLA26090022",
                              "CONSIGNEE_INFO_TEL": "+86 10 69479536",
                              "CONSIGNEE_INFO_COUNTRY": "US"}    # L2：区号与国家对不上
        tk.update({"needs_review": False, "flags": []})       # 前端声称干净
        r = client.post("/submit", json={"tickets": [tk]})
        assert r.status_code == 400, f"L3/L2 都有问题，服务端该拦下：{r.text}"
        flags = r.json()["detail"]["flags"]
        assert any("IATA 三字码" in f for f in flags), flags
        assert any("电话区号" in f for f in flags), flags
        assert store.ledger() == {}, "被拦下的提交不许进台账/索引"
        tk["acked_flags"] = flags
        r2 = client.post("/submit", json={"tickets": [tk]})
        assert r2.status_code == 200, r2.text
        assert store.ledger()["CLA26090022"]["acked_flags"] == flags, "人工确认要留痕"
    finally:
        auth.USERS_FILE, (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR,
                          config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR) = old_users, old
        shutil.rmtree(tmp, ignore_errors=True)


def test_submit_without_l2_on_disk_invents_no_flags():
    """服务端没有落盘的 L2/转录（比如票是在别的机器解析的）时，只能校 L3 那一层——
    不能因为拿不到 raw 就凭空报"MAWB_NO 缺失"，否则正常提交全被堵死。"""
    old = (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR, config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.SUBMIT_GUARD_DIR = tmp / "submit_guard"
    config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR = tmp / "nol2", tmp / "notrans"
    old_users = auth.USERS_FILE
    try:
        client = _logged_client(tmp)
        r = client.post("/submit", json={"tickets": [_clean_ticket()]})
        assert r.status_code == 200, r.text
    finally:
        auth.USERS_FILE, (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR,
                          config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR) = old_users, old
        shutil.rmtree(tmp, ignore_errors=True)


def test_reviewer_is_the_session_identity_not_the_payload():
    """提交留痕里"谁提的"必须取登录会话。前端传什么名字都认，等于台账可以随便署名。"""
    old = (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR, config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.SUBMIT_GUARD_DIR = tmp / "submit_guard"
    config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR = tmp / "nol2", tmp / "notrans"
    old_users = auth.USERS_FILE
    try:
        auth.USERS_FILE = tmp / "users.json"
        auth.ensure_seed()
        auth.set_password("宛平", "sf-123456", "reviewer")
        c = TestClient(server.app)
        assert c.post("/login", json={"name": "宛平", "password": "sf-123456"}).status_code == 200
        tk = _clean_ticket()
        tk["reviewer"] = "马殿齐"                      # 冒名：会话里明明是宛平
        assert c.post("/submit", json={"tickets": [tk]}).status_code == 200
        assert store.ledger()["CLA26090022"]["reviewer"] == "宛平", "留痕要取会话身份"
    finally:
        auth.USERS_FILE, (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR,
                          config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR) = old_users, old
        shutil.rmtree(tmp, ignore_errors=True)


def test_ledger_records_what_was_actually_sent():
    """台账要能回答"到底把什么发给了公司"：改了哪几列、提交体指纹、公司回的 action、提交前的原行。"""
    old = (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR, config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.SUBMIT_GUARD_DIR = tmp / "submit_guard"
    config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR = tmp / "nol2", tmp / "notrans"
    old_users = auth.USERS_FILE
    real_submit = server._company_submit
    try:
        # 公司回执形状照 live 那样（回归默认 mock，mock 没有 action/before，不能拿它验留痕字段）
        server._company_submit = lambda payload: {"mode": "live", "accepted": True,
                                                  "action": "insert", "before": {"PIECES": 5}}
        client = _logged_client(tmp)
        tk = _clean_ticket()
        tk["edited_fields"] = ["air.DEST_NAME"]
        assert client.post("/submit", json={"tickets": [tk]}).status_code == 200
        e = store.ledger()["CLA26090022"]
        assert e["edited_fields"] == ["air.DEST_NAME"]
        assert len(e["sent_fingerprint"]) == 64, "指纹要能对着上，事后能重算校验"
        assert e["company_action"] == "insert", e
        assert e["before"] == {"PIECES": 5}, "提交前读到的公司原行要留一份（整表写回出事时靠它看抹掉了什么）"
    finally:
        server._company_submit = real_submit
        auth.USERS_FILE, (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR,
                          config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR) = old_users, old
        shutil.rmtree(tmp, ignore_errors=True)


def test_submit_is_logged_with_who_and_result():
    """一次成功提交要在服务日志里留下一行（谁、哪张、公司怎么回的）。
    只有 HTTP 访问码的日志回答不了"这列 NULL 是谁写进去的"（2026-09-29 现场只能靠猜）。"""
    import logging

    old = (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR, config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_log_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.SUBMIT_GUARD_DIR = tmp / "submit_guard"
    config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR = tmp / "nol2", tmp / "notrans"
    old_users = auth.USERS_FILE
    got = []

    class _Cap(logging.Handler):
        def emit(self, record):
            got.append(record.getMessage())

    handler = _Cap()
    server.LOG.addHandler(handler)
    lvl = server.LOG.level
    server.LOG.setLevel(logging.INFO)
    try:
        client = _logged_client(tmp)
        assert client.post("/submit", json={"tickets": [_clean_ticket()]}).status_code == 200
    finally:
        server.LOG.removeHandler(handler)
        server.LOG.setLevel(lvl)
        auth.USERS_FILE, (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR,
                          config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR) = old_users, old
        shutil.rmtree(tmp, ignore_errors=True)
    assert any("admin" in m and "CLA26090022" in m for m in got), f"提交要留下谁与哪张：{got}"


def test_desk_has_submit_gate_on_flags_and_numbers():
    """/submit 之外，前端也得有提交门：缺号或未清红旗时拦住，别让人点了才吃 400。"""
    html = web_src.desk()
    assert "主单号、分单号都得填上才能提交回传公司" in html
    assert "还有未清的红旗" in html
    assert "d.message" in html, "服务端 400 的 detail.message 要挖出来给人看，别只剩光秃秃 HTTP 400"


def test_desk_has_flag_ack_button_and_payload():
    """存疑类红旗的出口是人工确认：chip 上有『确认无误』按钮，payload 带 acked_flags。"""
    html = web_src.desk()
    assert "确认无误" in html
    assert "acked_flags" in html


def test_desk_has_retry_for_failed_parse():
    """解析失败不能逼用户重新上传：失败面板要有『重试解析』按钮和对应逻辑。"""
    html = web_src.desk()
    assert "重试解析" in html
    assert "async function retryParse" in html


def test_company_submit_failure_is_logged_with_the_numbers():
    """用户在服务器上连点两次都是 502，journalctl 里只有访问码，公司拒的原因哪都没落
    （2026-09-29 实测 G26090906：502、502、然后 200）。回传失败要记一条 warning，
    带主单/分单号与公司原话，运维才有的可查。"""
    import logging

    import company_api
    old_submit, old_ledger, old_users = server._company_submit, config.SUBMIT_LEDGER, auth.USERS_FILE
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER, auth.USERS_FILE = tmp / "submitted.json", tmp / "users.json"
    got = []

    class _Cap(logging.Handler):
        def emit(self, record):
            got.append(record.getMessage())

    def boom(payload):
        raise company_api.CompanyApiError("公司返回 400：SHIPPER_INFO 超长")

    handler = _Cap()
    server.LOG.addHandler(handler)
    lvl = server.LOG.level
    server.LOG.setLevel(logging.WARNING)
    try:
        server._company_submit = boom
        client = _logged_client(tmp)
        r = client.post("/submit", json={"tickets": [_clean_ticket()]})
        assert r.status_code == 502, r.text
        assert store.ledger() == {}, "公司没收下的票不能进台账"
    finally:
        server.LOG.removeHandler(handler)
        server.LOG.setLevel(lvl)
        server._company_submit, config.SUBMIT_LEDGER, auth.USERS_FILE = old_submit, old_ledger, old_users
        shutil.rmtree(tmp, ignore_errors=True)
    assert any("96146363" in m and "CLA26090022" in m and "超长" in m for m in got), \
        f"日志要同时有号和原因：{got}"


def test_desk_master_search_explains_unsubmitted_house_orders():
    """用户 09:13 上传解析、09:15 检索主单看到"没有分单"，其实那张票 09:35 提交后公司才有行
    ——解析≠提交。名下分单为空时要说清这条，并把本台还没提交的同主单票数报出来。"""
    html = web_src.desk()
    assert "提交回公司" in html and "这里才会有记录" in html, "空表要说明白：先提交，公司才有这条分单"
    assert "function pendingUnder(" in html, "要能按主单号找出本台已解析未提交的分单"
    body = html[html.index("function pendingUnder("):]
    body = body[:min(x for x in (body.find("\nfunction "), body.find("\nasync function ")) if x > 0)]
    assert "S.tickets" in body and "drafts()" in body, "两处都要看：本次列表里的 + 浏览器暂存里的"
    assert "pendingUnder(mawb)" in html, "检索结果落位时要用上"
