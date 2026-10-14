# -*- coding: utf-8 -*-
"""提交回传链路（mock 回传 + 提交台账 + 主/分单号复合键索引）回归。
不产生任何外部调用：_company_submit 本就是 mock，测试也不碰网络。
覆盖：单号归一化 / 台账原子写 / 索引派生 / /submit 门禁（缺号 401→登录、缺号 400、干净进台账）/ 前端提交门存在。
"""
import contextlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import company_api
import config
import server
import web_src
import store

WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


def _clean_ticket(stem="CLA26090022", mawb="235-96146363", hawb="CLA26090022"):
    # acked_no_review：这张票没有被录入员复核过的暂存记录，发送要带这一次确认（见
    # test_sending_work_nobody_reviewed_needs_its_own_ack）。放进夹具是为了让其它用例
    # 只管自己那件事，但"必须显式带"这条由那条用例单独钉。
    return {"stem": stem, "filename": stem + ".pdf", "reviewer": "马殿齐",
            "acked_no_review": True,
            "air_reviewed": {"MAWB_NO": mawb, "HAWB_NO": hawb, "SHIPPET": ""}}


@contextlib.contextmanager
def _desk_dirs(tmp: Path, air: dict | None = None, qc_extra: dict | None = None,
               master_sent: bool = True):
    """把提交/暂存这条链路会读写的目录全指到临时目录，并备好这张票的 L3 与质检。

    一个都不能漏指：少指 OUTPUT_RAW_DIR 那次，用例往运维机的真 output/raw 落了一份假 L2，
    本机因为残留同名文件查不出来，是部署时的回归闸门在干净机器上才抓到的。

    `master_sent=True` 是 2026-10-14 那条「先提交主单再提交分单」的**前置**：分单提交门读的是
    主单台账，所以这里必须一并把 MASTER_DIR / MASTER_LEDGER 指开——不指开的话，那道门就在读
    这台机器真 output 里的主单台账，换台机器同一批用例结果就不同。要测门本身就用
    `master_sent=False`。
    """
    stem = "CLA26090022"
    mawb = "235-96146363"
    old = (config.SUBMIT_LEDGER, config.ARCHIVE_DIR, config.STAGED_DIR,
           config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR,
           config.TRANSCRIPT_DIR, config.SUBMIT_GUARD_DIR, config.MASTER_DIR,
           config.MASTER_LEDGER, auth.USERS_FILE)
    try:
        config.SUBMIT_LEDGER, config.ARCHIVE_DIR = tmp / "submitted.json", tmp / "archive"
        config.STAGED_DIR, config.SUBMIT_GUARD_DIR = tmp / "staged", tmp / "submit_guard"
        config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR = tmp / "air", tmp / "raw"
        config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR = tmp / "qc", tmp / "transcript"
        config.MASTER_DIR, config.MASTER_LEDGER = tmp / "master", tmp / "master_submitted.json"
        auth.USERS_FILE = tmp / "users.json"
        for p in (config.ARCHIVE_DIR, config.STAGED_DIR, config.OUTPUT_AIR_DIR,
                  config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR,
                  config.SUBMIT_GUARD_DIR, config.MASTER_DIR):
            p.mkdir(parents=True)
        (config.ARCHIVE_DIR / stem).mkdir()
        a = {"MAWB_NO": "235-96146363", "HAWB_NO": stem, "DEST_NAME": "LAX"}
        if air is not None:
            a = air
        (config.OUTPUT_AIR_DIR / f"{stem}.json").write_text(
            json.dumps(a, ensure_ascii=False), encoding="utf-8")
        qc = {"source_name": stem + ".pdf", "channel": "vlm", "model": "qwen3.8-flash",
              "model_choice": "qwen38-flash-bailian", "processed_at": "2026-10-08T09:00:00",
              "flags": [], "error": ""}
        qc.update(qc_extra or {})
        store.save_qc(stem, qc)
        if master_sent:
            sent_master(a.get("MAWB_NO") or mawb)
        yield
    finally:
        (config.SUBMIT_LEDGER, config.ARCHIVE_DIR, config.STAGED_DIR, config.OUTPUT_AIR_DIR,
         config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR,
         config.SUBMIT_GUARD_DIR, config.MASTER_DIR, config.MASTER_LEDGER,
         auth.USERS_FILE) = old


def _masters_in(tmp: Path, master_sent: bool = True):
    """主单那两处指进临时目录，并按需预置「这条主单已提交发送」——返回还原用的 undo。

    分单提交门读的就是主单台账，所以每个"期望提交成功"的用例都需要这条前置；让它去读这台
    机器真 output 里的主单台账，等于把用例结果绑在机器上。用 `_desk_dirs` 的用例不必再调这里。
    """
    old = (config.MASTER_DIR, config.MASTER_LEDGER)
    config.MASTER_DIR, config.MASTER_LEDGER = tmp / "master", tmp / "master_submitted.json"
    config.MASTER_DIR.mkdir(parents=True, exist_ok=True)
    if master_sent:
        sent_master()

    def undo():
        config.MASTER_DIR, config.MASTER_LEDGER = old
    return undo


def sent_master(mawb: str = "235-96146363", by: str = "马殿齐"):
    """写一条「这条主单已提交发送」进主单台账——用的就是真那条链落的同一个函数。"""
    return store.mark_master_submitted(mawb, by, {"mode": "mock"})


def test_norm_no_and_composite_key():
    assert store.norm_no("235-96146363") == "23596146363"
    assert store.norm_no(" cla 26090022 ") == "CLA26090022"
    # 连字符/空格/大小写差异归一后同键 → 公司与票面写法不一致也能对上
    assert store.number_key("235-96146363", "cla26090022") == "23596146363|CLA26090022"
    assert store.number_key("235 96146363", "CLA-26090022") == store.number_key("23596146363", "cla26090022")


def test_mark_submitted_writes_ledger_and_index_resolves_stem():
    old_ledger = config.SUBMIT_LEDGER
    old_arc = config.ARCHIVE_DIR
    tmp = Path(tempfile.mkdtemp(prefix="hawb_ledger_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.ARCHIVE_DIR = tmp / "archive"          # stem 现在意味着「原件在本机点得开」
    (config.ARCHIVE_DIR / "CLA26090022").mkdir(parents=True)
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
        config.ARCHIVE_DIR = old_arc
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


def _login_as(name: str, pw: str, role: str):
    """临时账号表里建一个指定角色的账号并登录，返回带会话的客户端。
    身份只从这里来：提交/暂存的署名必须取会话，不认请求里写的名字。"""
    auth.ensure_seed()
    u, err = auth.set_password(name, pw, role)
    assert err is None, f"{name}({role}) 建账号失败：{err}"
    client = TestClient(server.app)
    r = client.post("/login", json={"name": name, "password": pw})
    assert r.status_code == 200, f"{name} 登录失败：{r.status_code} {r.text[:120]}"
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
    old_arc = config.ARCHIVE_DIR
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.ARCHIVE_DIR = tmp / "archive"
    (config.ARCHIVE_DIR / "CLA26090022").mkdir(parents=True)   # 提交过的票，原件就在归档里
    old_users = auth.USERS_FILE
    undo_masters = _masters_in(tmp)
    try:
        client = _logged_client(tmp)
        r = client.post("/submit", json={"tickets": [_clean_ticket()]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["results"][0]["mode"] == "mock"
        assert store.ledger()["CLA26090022"]["key"] == "23596146363|CLA26090022"
        assert store.lookup_stem("235-96146363", "CLA26090022") == "CLA26090022"
    finally:
        undo_masters()
        auth.USERS_FILE = old_users
        config.SUBMIT_LEDGER = old_ledger
        config.ARCHIVE_DIR = old_arc
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
    undo_masters = _masters_in(tmp)
    try:
        client = _logged_client(tmp)
        acked = ['CONSIGNEE_INFO_TEL 的电话区号 +39(IT) 与 CONSIGNEE_INFO_COUNTRY IS 不一致，疑似电话串边']
        r = client.post("/submit", json={"tickets": [dict(_clean_ticket(), acked_flags=acked)]})
        assert r.status_code == 200, r.text
        led = store.ledger()["CLA26090022"]
        assert led["acked_flags"] == acked, "确认过的红旗原文要进台账备查"
        # 再提交一票不带 acked_flags → 空表，不出错
        r = client.post("/submit", json={"tickets": [_clean_ticket(stem="TAO2", hawb="TAO2")]})
        assert r.status_code == 200, r.text
        assert store.ledger()["TAO2"]["acked_flags"] == []
    finally:
        undo_masters()
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
    undo_masters = _masters_in(tmp)
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
        undo_masters()
        auth.USERS_FILE, (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR,
                          config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR) = old_users, old
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_number_the_human_filled_clears_the_missing_number_flag():
    """盘上 L2 没有号（模型漏抄），人在核对区照着票面补上再发——这时还报「MAWB_NO 缺失」
    就是一面看不见、一面过不去的旗：审核台按"当前值"判它已清（desk.js 的 awbFilled），
    而服务端那道门读的是盘上那份 L2，人工值只进 air_reviewed。两边说的不是同一件事，
    人只会看见"点提交→冒出一条没见过的红旗"（2026-10-12 已经为同类死旗修过一次，这处是
    2026-10-13 核对效率审查时发现的另一个漏口）。缺不缺号，看**这次真要发出去的那份值**。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_fillno_"))
    try:
        with _desk_dirs(tmp):
            stem = "CLA26090022"
            raw = {"MAWB_NO": "", "HAWB_NO": stem, "DEST_NAME": "LAX", "TO1": "AMS",
                   "PIECES": 1, "WEIGHT": 100.0, "CREATE_TIME": "2026-09-01",
                   "CONSIGNEE_INFO_TEL": "+86 10 69479536", "CONSIGNEE_INFO_COUNTRY": "CN",
                   "GOODS_NAME": "STEEL PARTS"}      # 除了漏抄主单号，这份 L2 别处都干净
            (config.OUTPUT_RAW_DIR / f"{stem}.json").write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            client = _login_as("马殿齐", "rv-123456", "reviewer")
            tk = _clean_ticket()                     # air_reviewed 里主单号是补齐了的
            r = client.post("/submit", json={"tickets": [tk]})
            assert r.status_code == 200, \
                f"号已经补齐还拦着提交（服务端看的是盘上那份漏抄的 L2）：{r.status_code} {r.text[:200]}"
            assert store.ledger()[stem]["air_sent"]["MAWB_NO"] == "235-96146363"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_number_nobody_filled_is_still_reported_as_missing():
    """反过来：谁都没补，缺号这条红旗必须在——它是"票面到底有没有印这个号"的提醒，
    不是可以默默放过的格式问题。"""
    import jobs
    raw = {"MAWB_NO": "", "HAWB_NO": "CLA26090022", "DEST_NAME": "LAX"}
    flags = jobs.recompute_flags(raw, dict(raw, MAWB_NO=""), None)
    assert any("MAWB_NO 缺失" in f for f in flags), flags


def test_the_missing_number_rule_lives_in_the_validator_only():
    """判据一处：validator 收一份"这次要发出去的值"，缺号那条按它判。
    在 jobs 或端点里再字符串匹配一遍旗面文案的话，改文案就有两处要跟着改。"""
    import validator
    raw = {"MAWB_NO": "", "HAWB_NO": "CLA26090022"}
    assert any("MAWB_NO 缺失" in w for w in validator.validate_raw(raw)), \
        "不传 sent 时必须照旧按 L2 自己判（解析那一刻还没有人工值）"
    assert not [w for w in validator.validate_raw(raw, sent={"MAWB_NO": "235-96146363"})
                if "MAWB_NO 缺失" in w], "号补齐了这条就该消"

    def awb_flags(out):
        return [w for w in out if "MAWB_NO" in w or "HAWB_NO" in w]

    both = {"MAWB_NO": "", "HAWB_NO": ""}
    assert any("均为空" in w for w in awb_flags(validator.validate_raw(both))), both
    assert awb_flags(validator.validate_raw(both, sent={"MAWB_NO": "235-96146363",
                                                         "HAWB_NO": "CLA26090022"})) == [], \
        "两格都补上了，缺号那两条都不该留"
    assert any("HAWB_NO 缺失" in w for w in awb_flags(
        validator.validate_raw(both, sent={"MAWB_NO": "235-96146363"}))), \
        "只补了一格：另一格那条要留着——两栏是各自独立判的"


def test_submit_without_l2_on_disk_invents_no_flags():
    """服务端没有落盘的 L2/转录（比如票是在别的机器解析的）时，只能校 L3 那一层——
    不能因为拿不到 raw 就凭空报"MAWB_NO 缺失"，否则正常提交全被堵死。"""
    old = (config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR, config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_submit_"))
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.SUBMIT_GUARD_DIR = tmp / "submit_guard"
    config.OUTPUT_RAW_DIR, config.TRANSCRIPT_DIR = tmp / "nol2", tmp / "notrans"
    old_users = auth.USERS_FILE
    undo_masters = _masters_in(tmp)
    try:
        client = _logged_client(tmp)
        r = client.post("/submit", json={"tickets": [_clean_ticket()]})
        assert r.status_code == 200, r.text
    finally:
        undo_masters()
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
    undo_masters = _masters_in(tmp)
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
        undo_masters()
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
    undo_masters = _masters_in(tmp)
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
        undo_masters()
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
    undo_masters = _masters_in(tmp)

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
        undo_masters()
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
    undo_masters = _masters_in(tmp)

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
        undo_masters()
        server._company_submit, config.SUBMIT_LEDGER, auth.USERS_FILE = old_submit, old_ledger, old_users
        shutil.rmtree(tmp, ignore_errors=True)
    assert any("96146363" in m and "CLA26090022" in m and "超长" in m for m in got), \
        f"日志要同时有号和原因：{got}"


def test_desk_master_search_reads_pending_from_the_server():
    """名下分单那张表的"还没提交"要来自服务器，不再由浏览器自己数。

    浏览器数那份（`pendingUnder` 读 `S.tickets` + localStorage）只对当前这个人、这台机器成立：
    制单员上午传的票，录入员下午查同一张主单仍然是"没有分单"——这正是需求一要修的。
    现在检索响应自带 `pending`（本机已解析/已暂存、公司还不认识的那些票），前端只渲染它。
    """
    html = web_src.desk()
    assert "function pendingUnder(" not in html, \
        "浏览器自己数待提交的老路子还在：换个人查同一张主单就看不见别人的票"
    i = html.index("function mstRender(")
    body = html[i:i + 900]
    assert "j.pending" in body, "检索响应里的 pending 没用上"
    assert "mstTable(rows)" in body, "表吃的是按号合并好的那一份行（见 L.hawbRows）"
    assert "drafts()" not in body, "待提交名单不许再读浏览器暂存"
    assert "提交回公司" in html, "空表要说明白：先提交，公司才有这条分单"
    assert "暂存" in html, "空表还要指出中间那一档：同事暂存过的票也在这张表里"


def test_submit_records_what_was_actually_sent_and_who_staged_it():
    """台账里要能读出"这次真发了什么值"和"发之前谁暂存过"。

    从前只存一个 sha256（`sent_fingerprint`）：整表写回把某列抹成 NULL 时，拿着哈希对不回
    到底发了什么值，只能重算；而 `edited_fields` 只有字段名、没有值，需求三的逐字段正确率
    算不出来。`sent_fingerprint` 保留（那条用例还在盯），旁边补一份全量终值。
    暂存记录同时盖章：submitted_at/submitter——这样"暂存了但没发出去"和"发出去了"一眼分得开。
    """
    tmp = Path(tempfile.mkdtemp(prefix="hawb_sent_"))
    old = (config.SUBMIT_LEDGER, config.ARCHIVE_DIR, config.STAGED_DIR,
           config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR,
           config.TRANSCRIPT_DIR, auth.USERS_FILE)
    undo_masters = _masters_in(tmp)          # 分单提交门的前置：这条主单已经发出去了
    try:
        config.SUBMIT_LEDGER = tmp / "submitted.json"
        config.ARCHIVE_DIR = tmp / "archive"
        config.STAGED_DIR = tmp / "staged"
        config.OUTPUT_AIR_DIR = tmp / "air"
        config.OUTPUT_RAW_DIR = tmp / "raw"          # 提交门会读这里：不指开就等于读这台机器的真 L2
        config.OUTPUT_QC_DIR = tmp / "qc"
        config.TRANSCRIPT_DIR = tmp / "transcript"
        auth.USERS_FILE = tmp / "users.json"
        for p in (config.ARCHIVE_DIR, config.STAGED_DIR, config.OUTPUT_AIR_DIR,
                  config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR):
            p.mkdir()
        (config.ARCHIVE_DIR / "CLA26090022").mkdir()
        (config.OUTPUT_AIR_DIR / "CLA26090022.json").write_text(
            json.dumps({"MAWB_NO": "235-96146363", "HAWB_NO": "CLA26090022",
                        "DEST_NAME": "LAX"}, ensure_ascii=False), encoding="utf-8")
        store.save_qc("CLA26090022", {"source_name": "CLA26090022.pdf", "channel": "vlm",
                                      "model": "qwen3.8-flash", "processed_at": "2026-10-08T09:00:00",
                                      "flags": [], "error": ""})
        auth.ensure_seed()
        auth.set_password("马殿齐", "rv-123456", "reviewer")
        client = TestClient(server.app)
        assert client.post("/login", json={"name": "马殿齐", "password": "rv-123456"}).status_code == 200

        tk = _clean_ticket()
        tk["air_reviewed"]["DEST_NAME"] = "ORD"
        assert client.post("/stage", json={"tickets": [tk]}).status_code == 200
        r = client.post("/submit", json={"tickets": [tk]})
        assert r.status_code == 200, r.text

        e = store.ledger()["CLA26090022"]
        assert e["air_sent"]["DEST_NAME"] == "ORD", f"发出去的值没进台账：{e.get('air_sent')}"
        assert e["sent_fingerprint"], "指纹还留着（整表写回对账要用）"
        assert e["stager"] == "马殿齐" and e["staged_at"], f"谁暂存的没带进台账：{e}"
        st = store.load_staged("CLA26090022")
        assert st["submitted_at"] and st["submitter"] == "马殿齐", st
        assert st["sent_fingerprint"] == e["sent_fingerprint"], "两边指纹对不上，盖章等于没盖"
    finally:
        undo_masters()
        (config.SUBMIT_LEDGER, config.ARCHIVE_DIR, config.STAGED_DIR, config.OUTPUT_AIR_DIR,
         config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR,
         auth.USERS_FILE) = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_submit_without_a_stage_still_records_the_sent_values():
    """没暂存过也要记下发了什么：暂存是可选的一档，不是提交的前置条件。
    这时 stager 为空，统计就按"模型值→提交值"算总改动，别把没暂存当成没改动。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_sent_"))
    try:
        with _desk_dirs(tmp):
            client = _login_as("马殿齐", "rv-123456", "reviewer")
            tk = _clean_ticket()
            tk["air_reviewed"]["DEST_NAME"] = "AMS"
            assert client.post("/submit", json={"tickets": [tk]}).status_code == 200
            e = store.ledger()["CLA26090022"]
            assert e["air_sent"]["DEST_NAME"] == "AMS", e
            assert e["stager"] == "" and e["staged_at"] == "", e
            assert store.load_staged("CLA26090022") is None, "提交不该顺手造一份暂存记录"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sending_work_nobody_reviewed_needs_its_own_ack_and_leaves_a_trace():
    """用户定案（四条里的第三条）：制单员仍可直发，但"没经录入员复核"要多点一次确认，并留痕。

    留痕不是为了拦人，是为了以后能回答"跳过复核直接发的那批票错得多不多"。
    没有这一档，需求三会把"没复核就发的"和"复核过才发的"混在一起算——那正是
    判断"能不能直接对接平台数据"最需要的对照。
    门是服务端算的：暂存记录里没有 inputter 的角色事件，就必须带 `acked_no_review`。
    """
    tmp = Path(tempfile.mkdtemp(prefix="hawb_ack_"))
    try:
        with _desk_dirs(tmp):
            rv = _login_as("马殿齐", "rv-123456", "reviewer")
            tk = _clean_ticket()
            tk["air_reviewed"]["DEST_NAME"] = "ORD"
            assert rv.post("/stage", json={"tickets": [tk]}).status_code == 200

            no_ack = dict(tk)
            no_ack.pop("acked_no_review")
            r = rv.post("/submit", json={"tickets": [no_ack]})
            assert r.status_code == 400, f"没人复核过的票居然直接发出去了：{r.status_code} {r.text[:150]}"
            d = r.json()["detail"]
            assert d.get("ack") == "no_inputter_review", f"没告诉前端要点哪一种确认：{d}"
            assert "录入员" in d["message"] and "复核" in d["message"], d
            assert "CLA26090022" not in store.ledger(), "被拒的提交不许进台账"

            r2 = rv.post("/submit", json={"tickets": [tk]})     # tk 带 acked_no_review
            assert r2.status_code == 200, r2.text
            assert store.ledger()["CLA26090022"]["no_inputter_review"] is True, store.ledger()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ticket_the_inputter_reviewed_sends_without_the_extra_ack():
    """录入员核对（暂存）过的票，直发不再要那次确认；台账也就不该留"未经复核"的记号。

    这一条是上一条的另一半：确认只欠在"没人复核过"上，不能变成每次提交都点一下的橡皮章——
    点多一次的确认人和不点一样，留着它就没有信息量了。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_ack_"))
    try:
        with _desk_dirs(tmp):
            rv = _login_as("马殿齐", "rv-123456", "reviewer")
            tk = _clean_ticket()
            tk["air_reviewed"]["DEST_NAME"] = "ORD"
            assert rv.post("/stage", json={"tickets": [tk]}).status_code == 200
            inp = _login_as("刘明", "in-123456", "inputter")
            assert inp.post("/stage", json={"tickets": [tk]}).status_code == 200

            no_ack = dict(tk)
            no_ack.pop("acked_no_review")
            r = inp.post("/submit", json={"tickets": [no_ack]})
            assert r.status_code == 200, f"录入员自己核对过并暂存过的票，不该再要确认：{r.text[:150]}"
            assert store.ledger()["CLA26090022"]["no_inputter_review"] is False, store.ledger()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_desk_asks_before_sending_unreviewed_work():
    """前端拿到那条 400 要问人，不能自己替他确认，也不能把这句话吞成"HTTP 400"。

    自动带上 acked_no_review 等于这道门形同虚设；而只回一句 HTTP 400，制单员不知道
    要点什么，最后只会骂系统坏了。"""
    js = web_src.desk()
    assert "no_inputter_review" in js, "没接服务端那条「未经录入员复核」的确认要求"
    i = js.index("async function submitTicket(")
    body = js[i:js.index("\n}", i)]
    assert "confirm(" in body, "要用一次明确的确认（本台删草稿、删账号也是走 confirm）"
    # ack 只能是"人确认过之后"才带上的：写成无条件就等于这道门由前端替他过了
    assert re.search(r"if\s*\(\s*ackNoReview\s*\)\s*p\.acked_no_review\s*=\s*true", body), \
        "acked_no_review 必须只在确认之后带上，不能默认发出去"


def test_desk_has_a_stage_button_that_cannot_reach_the_company():
    """前端也必须把两档分开：暂存发 /stage，提交发 /submit，不许复用同一段。

    这不是体验问题：点错一次就是给公司库写了一条记录，而"绝不代替人发送公司"是这台桌子的底线。
    打开别人暂存的票同样不能顺手把 /submit 那条路走完。"""
    js = web_src.desk()
    assert 'id="stage"' in js, "核对区没有「暂存本票」按钮"
    assert 'BASE + "/stage"' in js, "暂存没接到 /stage"
    i = js.index("async function stageTicket(")
    body = js[i:js.index("\n}", i)]
    assert "/submit" not in body, "暂存那段里混进了 /submit：那会真发给公司"
    assert 'BASE + "/staged/"' in js, "录入员打开别人暂存的票要读 /staged"
    assert "L.stagedMerge(" in js, "本地未存改动与服务器暂存合并要走 logic，别在渲染里再算一遍"


def test_a_batch_reports_which_tickets_made_it_when_one_does_not():
    """/submit 收的是 tickets 列表，但发送循环第一张撞错就 raise：
    前面已经发出去的几张随异常一起没了回音——调用方只看到一句失败，不知道其实有 N 张
    已经进了公司库，于是再点一次（整表写回第二次）或者以为全没发。

    现在逐张收集成败：成功的照常进台账并原样回给调用方，失败的带着公司给的原因一起回。
    整批状态取最贴切的那个码：全是锁行 409，有接口失败 502，全成功 200。
    """
    import company_api
    old_submit = server._company_submit
    tmp = Path(tempfile.mkdtemp(prefix="hawb_batch_"))
    try:
        with _desk_dirs(tmp):
            def flaky(payload):
                if payload["hawb"] == "BAD001":
                    raise company_api.CompanyApiError("公司返回 400：SHIPPER_INFO 超长")
                return {"mode": "mock", "accepted": True, "mawb": payload["mawb"],
                        "hawb": payload["hawb"]}
            server._company_submit = flaky
            client = _login_as("马殿齐", "rv-123456", "reviewer")
            t1 = _clean_ticket(stem="GOOD001", hawb="GOOD001")
            t1["air_reviewed"]["HAWB_NO"] = "GOOD001"
            t2 = _clean_ticket(stem="BAD001", hawb="BAD001")
            t2["air_reviewed"]["HAWB_NO"] = "BAD001"
            r = client.post("/submit", json={"tickets": [t1, t2]})
            assert r.status_code == 502, f"有一张没发出去要报失败码：{r.status_code} {r.text[:120]}"
            d = r.json()["detail"]
            by = {x["stem"]: x for x in d["results"]}
            assert by["GOOD001"]["ok"] is True, f"成功的那张要如实回报：{by}"
            assert by["BAD001"]["ok"] is False and "超长" in by["BAD001"]["error"], by
            led = store.ledger()
            assert "GOOD001" in led and "BAD001" not in led, \
                f"台账只该记真发出去的：{sorted(led)}"
    finally:
        server._company_submit = old_submit
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_inputter_submitting_himself_is_not_counted_as_unreviewed():
    """录入员本人直接提交：确认这一步照旧点一次（这次提交没有暂存那一步的逐字段留痕，得让人
    知道），但话要说准、数要记对。2026-10-12 现场：郭健康是录入员，提交时却被问「没有录入员
    复核过的暂存记录」，点下去这张票就进了「未经录入员复核」那一档——那一档是给"制单员跳过
    复核直发"准备的对照组，把录入员自己的提交算进去，数就说谎了。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_self_"))
    try:
        with _desk_dirs(tmp):
            inp = _login_as("刘明", "in-123456", "inputter")
            tk = _clean_ticket()
            no_ack = dict(tk)
            no_ack.pop("acked_no_review")
            r = inp.post("/submit", json={"tickets": [no_ack]})
            assert r.status_code == 400, "点一次确认这一步要留着：这次提交确实没有暂存留痕"
            d = r.json()["detail"]
            assert d.get("ack") == "no_inputter_review" and "你就是录入员" in d["message"], d
            assert "未经录入员复核" not in d["message"], "对录入员说这句是在否认他的身份"
            assert inp.post("/submit", json={"tickets": [tk]}).status_code == 200
            e = store.ledger()["CLA26090022"]
            assert e["inputter_self_submit"] is True and e["no_inputter_review"] is False, e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_confirm_wording_comes_from_the_server_only():
    """确认框那两行从前是前端把一句硬话接在服务器 message 后面：服务器换成「你就是录入员」
    之后，后面那半句还在，等于系统替自己说了一句它不该说的话。话只由服务端出，前端只画。"""
    js = web_src.part("js/desk.js")
    assert "确认「未经录入员复核，仍要回传公司」" not in js, "前端还在自己拼那句确认话"


# ── 主单先于分单（2026-10-14 用户定案：提交的时候要先提交主单，再提交分单）────────
def _live_read(sent, status=0, boom=None):
    """只把公司 HTTP 那一层换成假的：读主单状态走它，写不写分单由用例自己盯。

    `status=None` = 公司那边读回空（这条主单从没录过）；`boom` = 这次读失败。
    """
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL,
           config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY = config.COMPANY_HAWB_KEY = "k"
    real = company_api._j9_post

    def fake(path, body, key, timeout=None):
        sent.append(path)
        if boom is not None:
            raise boom
        if status is None:
            return {"code": 0, "data": []}
        return {"code": 0, "data": [{"AMS_RECORD": {"SEND_STATUS": status}}]}

    company_api._j9_post = fake

    def undo():
        company_api._j9_post = real
        (config.COMPANY_API_MODE, config.COMPANY_API_URL,
         config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY) = old
    return undo


def _no_write_outside():
    """把分单的提交发送出口打成 raise：挡下的那一下必须连一次公司写请求都不发。"""
    return lambda *a, **k: (_ for _ in ()).throw(AssertionError("主单还没发就碰了公司写口"))


def test_house_submit_is_blocked_until_its_master_is_sent():
    """主单还没提交发送时，这张分单不许走：半套数据先进公司库，比两张都没发更难收拾
    （分单那行指向的主单还不存在，外围取数取到的是缺主单的一条）。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_order_"))
    old_submit = server._company_submit
    try:
        with _desk_dirs(tmp, master_sent=False):
            server._company_submit = _no_write_outside()
            client = _logged_client(tmp)
            r = client.post("/submit", json={"tickets": [_clean_ticket()]})
            assert r.status_code == 400, f"主单没发，分单该被挡下：{r.status_code} {r.text[:160]}"
            d = r.json()["detail"]
            assert d.get("ack") == "master_first", f"要给出前端接得住的那个 ack：{d}"
            assert store.norm_no(str(d.get("mawb"))) == "23596146363", f"得点名是哪条主单：{d}"
            assert store.ledger() == {}, "被挡下的提交不许进台账/索引"
    finally:
        server._company_submit = old_submit
        shutil.rmtree(tmp, ignore_errors=True)


def test_master_sent_afterwards_lets_the_house_through():
    """门不是死路：人去把主单交了（主单那颗提交，不是系统代点），同一张分单就该放行。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_order_"))
    try:
        with _desk_dirs(tmp, master_sent=False):
            client = _logged_client(tmp)
            assert client.post("/submit", json={"tickets": [_clean_ticket()]}).status_code == 400
            sent_master()
            r = client.post("/submit", json={"tickets": [_clean_ticket()]})
            assert r.status_code == 200, f"主单已发出，分单不该再被挡：{r.status_code} {r.text[:160]}"
            assert store.ledger()["CLA26090022"]["key"] == "23596146363|CLA26090022"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_master_sent_by_somebody_else_counts_as_sent():
    """本机主单台账只记得从这台机器发出去的：主单可能是公司那边（或另一台机器）先录好、
    已经发送的。那种情况挡人就是挡错了——所以本机没记的时候要去问公司一次状态。"""
    import j9_fake
    tmp = Path(tempfile.mkdtemp(prefix="hawb_order_"))
    old_submit = server._company_submit
    try:
        with _desk_dirs(tmp, master_sent=False):
            sent = []
            undo = _live_read(sent, status=1)
            server._company_submit = lambda p: {"mode": "mock", "accepted": True}
            try:
                client = _logged_client(tmp)
                r = client.post("/submit", json={"tickets": [_clean_ticket()]})
                assert r.status_code == 200, f"公司侧状态=1 就是已发出：{r.status_code} {r.text[:160]}"
                assert any(p.endswith("/mawb/") for p in sent), f"该问过公司一次：{sent}"
                assert all(not j9_fake.is_write(p) for p in sent), \
                    f"这道门只许读，不许替人把主单发出去：{sent}"
            finally:
                undo()
    finally:
        server._company_submit = old_submit
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_master_the_company_never_had_is_still_blocked():
    """公司读回空列表＝这条主单从没录过，确实没发：照样挡。
    少了这条，上面那条"读到状态就放行"就没人证明它不是一路绿灯。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_order_"))
    old_submit = server._company_submit
    try:
        with _desk_dirs(tmp, master_sent=False):
            sent = []
            undo = _live_read(sent, status=None)
            server._company_submit = _no_write_outside()
            try:
                client = _logged_client(tmp)
                r = client.post("/submit", json={"tickets": [_clean_ticket()]})
                assert r.status_code == 400, f"公司那边没这条主单，分单不该走：{r.text[:160]}"
                assert r.json()["detail"].get("ack") == "master_first"
            finally:
                undo()
    finally:
        server._company_submit = old_submit
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_master_read_that_fails_does_not_hold_the_house_back():
    """读不到 ≠ 没发：公司接口抖动时把整条提交链卡死，人只会当作系统坏了。
    分单写口自己还要先读后写，真有问题公司会拦。但这次"没验成"要如实说出来，别让人以为查过主单。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_order_"))
    old_submit = server._company_submit
    try:
        with _desk_dirs(tmp, master_sent=False):
            sent = []
            undo = _live_read(sent, boom=company_api.CompanyApiError("j9 连不上：timed out"))
            server._company_submit = lambda p: {"mode": "mock", "accepted": True}
            try:
                client = _logged_client(tmp)
                r = client.post("/submit", json={"tickets": [_clean_ticket()]})
                assert r.status_code == 200, f"问不出主单状态不该卡住分单：{r.status_code} {r.text[:160]}"
                notes = r.json().get("notes") or []
                assert notes and "235-96146363" in " ".join(notes), f"要说明哪条没验成：{notes}"
            finally:
                undo()
    finally:
        server._company_submit = old_submit
        shutil.rmtree(tmp, ignore_errors=True)


def test_mock_mode_never_asks_the_company_about_the_master():
    """mock 模式一条请求都不许出网，这道门在 mock 下只认本机台账。
    判据本身不随模式变（有=放行、没有=挡），变的只是查得到的来源——写成两套判据的话，
    回归绿而线上挡人就是必然（这条链已经踩过一次）。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_order_"))
    real = company_api._j9_post
    try:
        with _desk_dirs(tmp, master_sent=False):
            company_api._j9_post = _no_write_outside()
            client = _logged_client(tmp)
            r = client.post("/submit", json={"tickets": [_clean_ticket()]})
            assert r.status_code == 400, f"mock 下也该按台账挡：{r.status_code} {r.text[:160]}"
            assert r.json()["detail"].get("ack") == "master_first"
    finally:
        company_api._j9_post = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_number_gate_still_answers_first():
    """主单号都没填就先报缺号：门读的就是这个号，号是空的时候报「主单还没提交发送」
    是把因果关系说反了，人还会以为去发主单就行。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_order_"))
    try:
        with _desk_dirs(tmp, master_sent=False):
            client = _logged_client(tmp)
            r = client.post("/submit", json={"tickets": [_clean_ticket(mawb="")]})
            d = r.json()["detail"]
            assert r.status_code == 400 and (isinstance(d, str) or d.get("ack") != "master_first"), \
                f"缺号要先答缺号：{d}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_batch_under_one_unsent_master_is_blocked_once():
    """整批门检的语义不能破：两张分单挂同一条没发的主单，只回一句、一个公司请求都不发。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_order_"))
    old_submit = server._company_submit
    try:
        with _desk_dirs(tmp, master_sent=False):
            server._company_submit = _no_write_outside()
            client = _logged_client(tmp)
            t1 = _clean_ticket(stem="H1", hawb="H1")
            t2 = _clean_ticket(stem="H2", hawb="H2")
            r = client.post("/submit", json={"tickets": [t1, t2]})
            assert r.status_code == 400, r.text[:160]
            d = r.json()["detail"]
            assert d.get("ack") == "master_first" and str(d.get("mawb")) == "235-96146363", d
            assert d["message"].count("235-96146363") == 1, f"同一条主单别说两遍：{d['message']}"
            assert store.ledger() == {}
    finally:
        server._company_submit = old_submit
        shutil.rmtree(tmp, ignore_errors=True)


def test_desk_walks_the_human_to_the_master_instead_of_clicking_it():
    """挡下那一下不能只是一句报错：要能把人带去主单核对页。
    但「送入主单核对」那段只许有一份——卡片那颗按钮与这道门的跳转各抄一遍，
    迟早一边改了另一边没改（这块已经为"两处抄一份"修过三次）。
    跳转里绝不许出现主单的提交口：那一发永远是人工那一下。"""
    js = web_src.part("js/desk.js")
    assert 'master_first' in js, "前端没接住这道门的 ack，人只会看见一句光秃秃的 HTTP 400"
    assert "function gotoMaster" in js, "挡下之后没有那条去路"
    assert js.count("function openMvFromMst") == 1, "开主单核对页的代码不止一份"
    # 断言只能落在 gotoMaster 这一段里：拿 split(...)[1][:1200] 会一路吃到隔壁函数，
    # 从 addEventListener 那份共用调用里蒙对（M12 变异就是这样活的）。
    jump = web_src.fn(js, "async function gotoMaster(mawb)")
    assert "openMvFromMst(" in jump, "跳转没走那份共用的开页代码，自己另抄一段迟早漂移"
    assert "/master/submit" not in jump, "跳转里不许自动提交主单：那一发只能是人工点"
