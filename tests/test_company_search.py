# -*- coding: utf-8 -*-
"""录入员检索链路回归：角色种子 / 本地 mock 检索 / 复合键按主单号过滤 / /company/mawb 角色门禁 / 前端页存在。
不产生任何外部调用：company_api 只有 mock 实现，live 一律抛 CompanyNotConfigured。
"""
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import company_api
import config
import server
import store

WEB_INPUTTER = Path(__file__).resolve().parent.parent / "web" / "inputter.html"


def _isolate(tmp: Path):
    """把账号表与提交台账指到临时目录，返回旧值供恢复。"""
    old_users, old_ledger = auth.USERS_FILE, config.SUBMIT_LEDGER
    auth.USERS_FILE = tmp / "users.json"
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    return old_users, old_ledger


def _restore(old_users, old_ledger, tmp):
    auth.USERS_FILE, config.SUBMIT_LEDGER = old_users, old_ledger
    shutil.rmtree(tmp, ignore_errors=True)


def _login(client, name, password):
    r = client.post("/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"{name} 登录失败：{r.status_code} {r.text}"


def test_seed_has_three_inputters_without_password():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        users = auth.seed_data()["users"]
        for n in auth.INPUTTERS:
            assert users[n]["role"] == "inputter", f"{n} 应为录入员角色"
            assert not users[n]["pw"], "录入员种子口令应为空，待管理员下发"
        assert set(auth.INPUTTERS) == {"刘明", "郭健康", "郭旭"}
    finally:
        _restore(ou, ol, tmp)


def test_admin_can_issue_password_for_inputter():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        auth.ensure_seed()
        u, err = auth.set_password("刘明", "pw-123456", "inputter")
        assert err is None and u["pw"], "管理员给录入员下发口令应成功"
        r = TestClient(server.app).post("/login", json={"name": "刘明", "password": "pw-123456"})
        assert r.status_code == 200, "下发口令后录入员应能登录"
        assert r.json()["user"]["role"] == "inputter"
    finally:
        _restore(ou, ol, tmp)


def test_submitted_by_mawb_filters_and_normalizes():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        store.mark_submitted("CLA1", "235-96146363", "CLA001", "马殿齐", {"mode": "mock"})
        store.mark_submitted("CLA2", "235 96146363", "CLA002", "宛平", {"mode": "mock"})   # 同主单，写法不同
        store.mark_submitted("OTHER", "176-11111111", "HZ001", "陈新", {"mode": "mock"})   # 别的主单
        hit = {e["hawb"] for e in store.submitted_by_mawb("23596146363")}
        assert hit == {"CLA001", "CLA002"}, "连字符/空格差异归一后应同属一个主单"
        assert store.submitted_by_mawb("") == [], "空主单号不返回任何票"
        assert store.submitted_by_mawb("999-00000000") == []
    finally:
        _restore(ou, ol, tmp)


def test_company_api_mock_returns_orders_with_stem():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        store.mark_submitted("CLA1", "235-96146363", "CLA001", "马殿齐", {"mode": "mock"})
        j = company_api.search_mawb("235-96146363")
        assert j["mode"] == "mock" and j["mawb"] == "235-96146363"
        assert j["mawb_order"] == {}, "主单业务字段留空壳，等公司真接口"
        assert len(j["hawb_orders"]) == 1
        o = j["hawb_orders"][0]
        assert o["hawb"] == "CLA001" and o["stem"] == "CLA1", "分单带上本机原件 stem 供 /source 打开"
    finally:
        _restore(ou, ol, tmp)


def test_company_api_live_mode_raises():
    old = config.COMPANY_API_MODE
    config.COMPANY_API_MODE = "live"
    try:
        try:
            company_api.search_mawb("235-96146363")
            assert False, "live 模式无真实现应抛 CompanyNotConfigured，绝不静默返回假数据"
        except company_api.CompanyNotConfigured:
            pass
        try:
            company_api.submit_order({"mawb": "1", "hawb": "2"})
            assert False, "提交走 live 也应抛错"
        except company_api.CompanyNotConfigured:
            pass
    finally:
        config.COMPANY_API_MODE = old


def test_company_mawb_requires_login():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        auth.ensure_seed()
        r = TestClient(server.app).get("/company/mawb", params={"mawb": "235-96146363"})
        assert r.status_code == 401, "检索要登录会话"
    finally:
        _restore(ou, ol, tmp)


def test_company_mawb_reviewer_forbidden_inputter_ok():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        auth.ensure_seed()
        auth.set_password("马殿齐", "rv-123456", "reviewer")
        auth.set_password("郭健康", "in-123456", "inputter")
        store.mark_submitted("CLA1", "235-96146363", "CLA001", "马殿齐", {"mode": "mock"})

        rv = TestClient(server.app)
        _login(rv, "马殿齐", "rv-123456")
        r = rv.get("/company/mawb", params={"mawb": "235-96146363"})
        assert r.status_code == 403, "制单员不是录入员，应被挡在检索门外"

        inp = TestClient(server.app)
        _login(inp, "郭健康", "in-123456")
        r2 = inp.get("/company/mawb", params={"mawb": "23596146363"})   # 不带连字符也命中
        assert r2.status_code == 200, r2.text
        assert r2.json()["hawb_orders"][0]["stem"] == "CLA1"

        empty = inp.get("/company/mawb", params={"mawb": "  - "})
        assert empty.status_code == 400, "空主单号应 400"
    finally:
        _restore(ou, ol, tmp)


def test_company_mawb_admin_allowed():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        auth.ensure_seed()
        c = TestClient(server.app)
        _login(c, "admin", "admin123")
        r = c.get("/company/mawb", params={"mawb": "235-96146363"})
        assert r.status_code == 200, "管理员可代为排查，允许检索"
        assert r.json()["mode"] == "mock"
    finally:
        _restore(ou, ol, tmp)


def test_inputter_page_is_served():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        auth.ensure_seed()
        r = TestClient(server.app).get("/inputter")
        assert r.status_code == 200, "录入员检索台页面应能打开（数据接口才要登录）"
        assert "录入员检索" in r.text
    finally:
        html = WEB_INPUTTER.read_text(encoding="utf-8")
        assert "/company/mawb" in html and "查看原件" in html
        _restore(ou, ol, tmp)


# === 主单原件口子（/mawb/source）：不依赖公司接口，先把"录入员能打开主单原件对票面"跑通 ===

def _use_mawb_source(tmp: Path):
    """把主单原件目录指到临时目录，返回旧值。"""
    old = config.MAWB_SOURCE_DIR
    config.MAWB_SOURCE_DIR = tmp / "mawb_source"
    return old


def _put_mawb_pdf(mawb: str) -> Path:
    d = config.MAWB_SOURCE_DIR / store.norm_no(mawb)
    d.mkdir(parents=True, exist_ok=True)
    (d / "mawb.pdf").write_bytes(b"%PDF-1.4 test")
    return d


def test_mawb_source_dir_normalizes_and_blocks_traversal():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_msrc_"))
    old = _use_mawb_source(tmp)
    try:
        _put_mawb_pdf("235-96146363")
        assert store.mawb_source_dir("235 96146363") == config.MAWB_SOURCE_DIR / "23596146363", \
            "写法差异（连字符/空格）归一后应指向同一目录"
        assert store.mawb_source_dir("235-00000000") is None, "没放过原件的主单应报没有"
        assert store.mawb_source_dir("") is None
        assert store.mawb_source_key("  - ") == "", "纯符号主单号归一后为空，不该当目录名用"
        for bad in ("../..", "a/../../etc", "a/../../../../etc/passwd"):
            d = store.mawb_source_dir(bad)
            assert d is None or d.parent == config.MAWB_SOURCE_DIR, \
                "归一化会把符号剥光，剩什么字母数字都只可能落在主单原件目录下面"
    finally:
        config.MAWB_SOURCE_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_mawb_source_route_guards_roles_and_serves_pdf():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_msrc_"))
    ou, ol = _isolate(tmp)
    old_src, old_prev = _use_mawb_source(tmp), config.PREVIEW_DIR
    config.PREVIEW_DIR = tmp / "preview"
    try:
        auth.ensure_seed()
        auth.set_password("郭旭", "in-123456", "inputter")
        auth.set_password("宛平", "rv-123456", "reviewer")
        assert TestClient(server.app).get("/mawb/source/235-96146363").status_code == 401, "要登录会话"
        rv = TestClient(server.app)
        _login(rv, "宛平", "rv-123456")
        assert rv.get("/mawb/source/235-96146363").status_code == 403, "制单员没有主单原件权限"
        inp = TestClient(server.app)
        _login(inp, "郭旭", "in-123456")
        miss = inp.get("/mawb/source/235-96146363")
        assert miss.status_code == 404, "本机没放过这张主单的原件应 404"
        _put_mawb_pdf("235-96146363")
        r = inp.get("/mawb/source/235-96146363")
        assert r.status_code == 200 and "application/pdf" in r.headers["content-type"], "PDF 原件直接发回预览"
        assert inp.get("/mawb/source/12345").status_code == 400, "位数不够的主单号应 400，不去拼路径"
        store.mark_submitted("CLA1", "235-96146363", "CLA001", "马殿齐", {"mode": "mock"})
        assert company_api.search_mawb("235-96146363")["source_available"] is True
        assert company_api.search_mawb("176-22222222")["source_available"] is False
    finally:
        config.PREVIEW_DIR, config.MAWB_SOURCE_DIR = old_prev, old_src
        _restore(ou, ol, tmp)


def test_inputter_page_has_mawb_source_entry_and_generic_fields():
    html = WEB_INPUTTER.read_text(encoding="utf-8")
    assert "查看主单原件" in html and "/mawb/source/" in html
    assert "mawb_order" in html and "Object.keys" in html, \
        "主单业务字段按接口返回的 key 通用渲染：真接口一通就自动出字段，不用改前端"


def test_desk_topbar_has_mawb_search_entry_for_admin_only():
    """主单入口此前只有 URL：录入员登录后 index 会自动跳去 /inputter，管理员却没有任何按钮指过去。
    入口只给管理员——制单员点了也是被 require_inputter 拒掉，摆出来是坑人。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="btnMawb" hidden' in html, "顶栏要有主单检索台按钮，且未登录时不得先闪出来"
    assert 'location.href = BASE + "/inputter"' in html, \
        "跳转要拼 BASE：反代前缀（/hawb/）下硬写 /inputter 会跳到站点根路径"
    assert '$("#btnMawb").hidden = !admin' in html, "按钮可见性要跟管理员判定同一条"
