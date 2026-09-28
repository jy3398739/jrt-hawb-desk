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
        assert "parsed" not in j, "检索响应不再自带手拆视图：主单改走 master_pipeline 真解析"
        assert len(j["hawb_orders"]) == 1
        o = j["hawb_orders"][0]
        assert o["hawb"] == "CLA001" and o["stem"] == "CLA1", "分单带上本机原件 stem 供 /source 打开"
    finally:
        _restore(ou, ol, tmp)


def _fn(html: str, name: str) -> str:
    """截取某个 function 的函数体（到下一个顶层 function 为止），用来做结构性断言。"""
    start = html.index("function " + name + "(")
    nxt = html.index("\nfunction ", start + 1)
    return html[start:nxt]


def test_desk_master_view_parses_edits_and_submits():
    """主单视图（2026-09-28 改口径）：检索即解析 → 左栏 L1 原文、右栏 36 列可编辑 + 红旗，
    人工确认后按 mawb2 回传公司。旧版"只读、不接提交"的口径已废。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    pv, mn = _fn(html, "renderPreview"), _fn(html, "renderMain")
    assert "MV" in pv, "票面预览要有主单分支"
    assert "MV" in mn, "核对区要有主单分支"
    mv = _fn(html, "mvPreview")
    assert "transcript" in mv and "esc(" in mv, "左栏要展示 L1 原文（逐字、转义后）"
    assert "MV.rec" in mn and "parsed" not in mn, "右栏吃解析记录，不再吃手拆视图"
    assert "data-mk" in mn, "36 列必须可编辑（要提交就得能改）"
    assert "fields" in mn and "meta" in mn and "groups" in mn, \
        "列名/分组/只读列（JOB_ID 等）都由后端给，前端不抄第二份表"
    assert "确认无误" in mn, "红旗要能逐条确认（提交门认这个）"
    assert "missed_cols" in mn and "漏取" in mn, \
        "'资料里有但没取到'要单独标出来，不能和'资料里本来就没有'混成同一个空格子"
    assert 'id="mvSubmit"' in html and "/master/submit" in html, "主单要有回传公司的出口"
    assert 'id="mvReparse"' in html and "force=1" in html, "换模型后要能强制重解析"
    assert "/master/" in html, "解析在后台跑，前端要轮询状态"
    assert "只读核对，不提交" not in html, "旧口径残留会误导复核人"
    assert '$("#view").scrollIntoView' in html, \
        "窄屏（<1461px）时票面栏在核对区上方：滚动要停在票面栏，否则用户看着它空着以为没反应"


def test_company_api_live_without_endpoint_never_fakes_or_hits_network():
    """live 已有真实现（2026-09-26 j9 契约），但缺端点/密钥时必须立刻抛 CompanyNotConfigured——
    既不静默返回假数据，也不会拿着空 URL 往外发请求。（回归入口还把 URL/key 钉空做双保险。）"""
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL,
           config.COMPANY_HAWB_KEY, config.COMPANY_MAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", ""
    config.COMPANY_HAWB_KEY = config.COMPANY_MAWB_KEY = ""
    try:
        for call in (lambda: company_api.search_mawb("235-96146363"),
                     lambda: company_api.submit_order({"mawb": "235-96146363", "hawb": "X1", "air": {}})):
            try:
                call()
                raise AssertionError("live 缺配置居然没抛 CompanyNotConfigured")
            except company_api.CompanyNotConfigured:
                pass
    finally:
        (config.COMPANY_API_MODE, config.COMPANY_API_URL,
         config.COMPANY_HAWB_KEY, config.COMPANY_MAWB_KEY) = old


def test_company_mawb_requires_login():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        auth.ensure_seed()
        r = TestClient(server.app).get("/company/mawb", params={"mawb": "235-96146363"})
        assert r.status_code == 401, "检索要登录会话"
    finally:
        _restore(ou, ol, tmp)


def test_company_mawb_reviewer_and_inputter_both_allowed():
    """2026-09-26 合并：主单检索不再分角色——制单员/录入员/管理员登录即用（一个工作台）。"""
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
        assert r.status_code == 200, f"制单员合并后也能检索主单：{r.status_code} {r.text[:120]}"

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


def test_inputter_route_redirects_to_merged_desk():
    """2026-09-26 合并：录入员功能进了制单台，/inputter 不再单独成页——老书签/外链一律 302 回主页，
    避免出现两份会各自漂移的检索界面。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_in_"))
    ou, ol = _isolate(tmp)
    try:
        auth.ensure_seed()
        r = TestClient(server.app, follow_redirects=False).get("/inputter")
        assert r.status_code in (302, 307), f"/inputter 应重定向回制单台，实际 {r.status_code}"
        assert r.headers["location"] in ("/", "./", ""), r.headers.get("location")
    finally:
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
        assert rv.get("/mawb/source/235-96146363").status_code == 404, \
            "合并后制单员也能开主单原件（本机没放原件才是 404，不再是 403）"
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


def test_desk_page_carries_merged_mawb_search_and_entry_for_all():
    """2026-09-26 合并：录入员功能并进制单台——主单检索、主单/分单原件、CCSP 填表计划
    都在 index.html 一个页面里；入口对所有登录角色可见（录入员/制单员同一工作台）。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "/company/mawb" in html, "制单台要有主单检索"
    assert "查看主单原件" in html and "/mawb/source/" in html, "制单台要能开主单原件"
    assert "/webplan/" in html and "填表计划" in html, "CCSP 填表计划要在制单台上"
    assert "mawb_order" in html and "Object.keys" in html, "有没有资料要按接口返回判断"
    assert "公司侧没有这条主单的资料" in html, "查不到时给一句话说明，不再摊一表原始字段"
    assert "function mstCard" in html and _fn(html, "mstCard").count("<table") == 0, \
        "检索卡片只留入口：外层原始字段表已撤，看内容一律走主单核对页（2026-09-28 用户定案）"
    assert 'id="btnMawb"' in html, "顶栏要有「主单检索」入口"
    assert '$("#btnMawb").hidden = !ME' in html, "入口对所有登录角色可见，只在未登录时藏"
