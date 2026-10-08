# -*- coding: utf-8 -*-
"""登录 / 账号 / 门禁回归：口令哈希、users.json 种子、会话 Cookie、角色边界。
全本地：TestClient 在本进程跑，users.json 指到临时目录，绝不碰真实落盘，也不产生 API 调用。
2026-09-22 起 HTTP_API_KEY / X-API-Key 已从服务端删除：登录是唯一门槛，鉴权只看会话 Cookie。
"""
import contextlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import server
import web_src


@contextlib.contextmanager
def _accounts():
    """把账号表**和 .env** 一起指到临时目录，返回一个共享 Cookie 的 TestClient。
    两个都要指：首次登录时 users.json 会自动种子；而 POST /model 会把选择写回 config.ENV_FILE ——
    只隔账号表的话，用例就在改这台机器的真 .env（2026-10-08 服务器上每次部署跑回归都把线上
    模型选择改回去了，跑的就是这条路径）。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_users_test_"))
    old_file, old_env = auth.USERS_FILE, config.ENV_FILE
    auth.USERS_FILE = tmp / "users.json"
    config.ENV_FILE = tmp / ".env"
    config.ENV_FILE.write_text("VLM_MODEL=intern-s2-official\n", encoding="utf-8")
    try:
        yield TestClient(server.app)
    finally:
        auth.USERS_FILE, config.ENV_FILE = old_file, old_env
        shutil.rmtree(tmp, ignore_errors=True)


def _login(client, name, password):
    return client.post("/login", json={"name": name, "password": password})


# === 口令哈希 ===
def test_hash_verify_roundtrip_and_rejects():
    h = auth.hash_password("s3cret-pass")
    assert h.startswith("pbkdf2_sha256$") and "s3cret-pass" not in h
    assert auth.verify_password("s3cret-pass", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("s3cret-pass", None)        # 未设口令永远进不去
    assert not auth.verify_password("s3cret-pass", "garbage")   # 结构不对也不炸


# === users.json 种子 ===
def test_seed_has_admin_and_reviewers_without_plaintext():
    with _accounts() as client:
        users = {u["name"]: u for u in auth.list_users()}
        assert users["admin"]["role"] == "admin" and users["admin"]["has_password"]
        for n in auth.REVIEWERS:
            assert users[n]["role"] == "reviewer" and not users[n]["has_password"], n
        raw = auth.USERS_FILE.read_text(encoding="utf-8")
        assert "admin123" not in raw, "默认口令绝不能明文落盘"
        assert "pbkdf2_sha256$" in raw


def test_default_admin_password_is_flagged_to_the_logged_in_admin():
    """"上线先改 admin123"这条待办只写在文档里，系统自己从不吭声——所以它一直没被做。
    登录后 /api/me 要说出现状（只说"还是种子默认口令"，不说口令是什么），改了就不再说。"""
    with _accounts() as client:
        _login(client, "admin", "admin123")
        me = client.get("/api/me").json()
        assert me["user"]["role"] == "admin"
        assert me.get("admin_pw_default") is True, "管理员还在用默认口令，页面要看得见这件事"
        r = client.post("/admin/users", json={"name": "admin", "password": "huan-diao-a-123"})
        assert r.status_code == 200, r.text
        assert client.get("/api/me").json().get("admin_pw_default") is False, \
            "口令已经改了还报默认，等于狼来了，提示会失去可信度"


def test_default_password_flag_stays_out_of_the_anonymous_endpoint():
    """`/health` 是免登录的（监控与部署脚本要能打到），口令状态绝不能放上去：
    那等于站在门外告诉对方"管理员还是 admin123"。同理也不发给制单员——改不了的人不必被推，
    少一处出现就少一处泄露面。"""
    with _accounts() as client:
        assert not [k for k in client.get("/health").json()
                    if "pw" in k.lower() or "password" in k.lower()], \
            "/health 免登录，口令状态不能出现在这里"
        _login(client, "admin", "admin123")
        client.post("/admin/users", json={"name": "马殿齐", "password": "pw-123456", "role": "reviewer"})
        client.post("/login", json={"name": "马殿齐", "password": "pw-123456"})
        me = client.get("/api/me").json()
        assert me["user"]["role"] == "reviewer"
        assert not me.get("admin_pw_default"), "这个标志只给管理员看"


def test_ensure_seed_creates_once_and_is_idempotent():
    with _accounts():
        assert auth.ensure_seed() is True and auth.USERS_FILE.is_file()
        first = auth.USERS_FILE.read_bytes()
        assert auth.ensure_seed() is False            # 已存在就不再动（不会重置管理员改过的口令）
        assert auth.USERS_FILE.read_bytes() == first


def test_backfill_seed_adds_missing_inputters_without_touching_existing():
    """老 users.json（早于录入员角色生成）升级后应自动补进 3 位录入员，且不碰已有账号。"""
    with _accounts():
        auth.ensure_seed()
        data = auth.load()
        admin_hash = data["users"]["admin"]["pw"]
        # 伪造一份"还没有录入员"的存量表
        for n in auth.INPUTTERS:
            data["users"].pop(n, None)
        auth.save(data)
        assert all(n not in auth.load()["users"] for n in auth.INPUTTERS)
        added = auth.backfill_seed()
        assert set(added) == set(auth.INPUTTERS), f"应补进 3 位录入员，实际：{added}"
        users = auth.load()["users"]
        for n in auth.INPUTTERS:
            assert users[n]["role"] == "inputter" and not users[n]["pw"]
        assert users["admin"]["pw"] == admin_hash, "补录绝不能动管理员已改的口令"
        assert auth.backfill_seed() == [], "再跑一次无缺口应原样返回空、不改盘"


def test_login_inputter_and_role():
    with _accounts() as client:
        auth.ensure_seed()
        assert _login(client, "刘明", "whatever").status_code == 401, "种子录入员未设口令应进不去"
        auth.set_password("刘明", "in-123456", "inputter")
        r = _login(client, "刘明", "in-123456")
        assert r.status_code == 200 and r.json()["user"]["role"] == "inputter"


# === 会话 Cookie 与角色边界 ===
def test_admin_login_unlocks_web_and_admin():
    with _accounts() as client:
        assert client.get("/api/me").json()["authenticated"] is False
        r = _login(client, "admin", "admin123")
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin", r.text
        assert client.get("/api/me").json()["user"]["name"] == "admin"
        assert client.get("/results").status_code == 200, "管理员会话应能用审核台数据接口"
        assert client.get("/admin/users").status_code == 200
        assert client.post("/model", json={"model": "intern-s2-official"}).status_code == 200, \
            "管理员不必再带密钥也能切模型"


def test_failed_login_is_logged_without_the_password():
    """公网可达的登录口被人（或扫描器）连着试错时，日志里要留得下"谁在试"。
    口令本身绝不能进日志——那等于把猜出来的密码写给所有能看 journal 的人。"""
    import logging

    got = []

    class _Cap(logging.Handler):
        def emit(self, record):
            got.append(record.getMessage())

    handler = _Cap()
    server.LOG.addHandler(handler)
    lvl = server.LOG.level
    server.LOG.setLevel(logging.INFO)
    try:
        with _accounts() as client:
            assert _login(client, "ghost", "x").status_code == 401
            assert _login(client, "admin", "wrong").status_code == 401
    finally:
        server.LOG.removeHandler(handler)
        server.LOG.setLevel(lvl)
    assert any("ghost" in m for m in got), f"未知账号的尝试要留一行：{got}"
    assert any("admin" in m for m in got), f"口令错的尝试要留一行：{got}"
    assert not any("wrong" in m for m in got), "日志里不许出现口令"


def test_login_rejects_bad_password_and_unknown_user():
    with _accounts() as client:
        assert _login(client, "admin", "wrong").status_code == 401
        assert _login(client, "ghost", "x").status_code == 401
        assert _login(client, "admin", "").status_code == 400, "空口令应报 400 而不是试着比"
        assert client.get("/api/me").json()["authenticated"] is False, "失败的登录不该发会话"


def test_reviewer_blocked_until_admin_sets_password_then_web_yes_admin_no():
    with _accounts() as client:
        assert _login(client, "马殿齐", "anything").status_code == 401, "没口令的制单员不能进"
        _login(client, "admin", "admin123")
        r = client.post("/admin/users", json={"name": "马殿齐", "password": "pd-123456", "role": "reviewer"})
        assert r.status_code == 200 and r.json()["user"]["has_password"], r.text
        assert _login(client, "马殿齐", "pd-123456").status_code == 200
        me = client.get("/api/me").json()
        assert me["user"] == {"name": "马殿齐", "role": "reviewer"}
        assert client.get("/results").status_code == 200, "制单员要能上传/核对/落盘"
        assert client.post("/extract").status_code != 401, "制单员对 /extract 有权限（缺文件是 422，不是未授权）"
        assert client.get("/admin/users").status_code == 403, "制单员不得管账号"
        assert client.post("/model", json={"model": "intern-s2-official"}).status_code == 403, "制单员不得切模型"


# === 未登录一律 401（不再有 X-API-Key 这条路） ===
def test_unauthenticated_requests_get_401():
    with _accounts() as client:                 # 全新客户端：没有任何会话
        assert client.get("/results").status_code == 401
        assert client.post("/model", json={"model": "intern-s2-official"}).status_code == 401
        assert client.get("/admin/users").status_code == 401
        assert client.get("/health").status_code == 200, "健康检查始终免鉴权"
        assert client.get("/models").status_code == 200, "模型清单免鉴权（下拉在登录页也要能填出来）"


# === 会话防伪与失效 ===
def test_forged_cookie_is_rejected():
    with _accounts() as client:
        client.cookies.set(auth.COOKIE, auth.make_token("admin", "admin", "attacker-secret"), path="/")
        assert client.get("/api/me").json()["authenticated"] is False, "用别的 secret 签的 token 不能认"
        assert client.get("/results").status_code == 401, "伪造会话又被拒了"


def test_deleting_a_user_voids_their_live_session():
    with _accounts() as client:                 # client 全程是管理员
        _login(client, "admin", "admin123")
        client.post("/admin/users", json={"name": "宛平", "password": "pw-123456", "role": "reviewer"})
        peer = TestClient(server.app)           # 另开一个：宛平登录后持续持有自己的会话 Cookie
        assert _login(peer, "宛平", "pw-123456").status_code == 200
        assert peer.get("/results").status_code == 200
        assert client.delete("/admin/users/宛平").status_code == 200
        assert peer.get("/api/me").json()["authenticated"] is False, "删除后老会话必须即刻失效"
        assert peer.get("/results").status_code == 401, "失效会话不能还能取数据"


# === 账号管理护栏 ===
def test_delete_guards_self_and_last_admin():
    with _accounts() as client:
        _login(client, "admin", "admin123")
        assert client.delete("/admin/users/admin").status_code == 400, "不能删唯一管理员/自己"
        assert client.delete("/admin/users/nope").status_code == 400
        client.post("/admin/users", json={"name": "陈新", "password": "pw-123456"})
        assert client.delete("/admin/users/陈新").status_code == 200


def test_admin_can_change_own_password_and_relogin():
    with _accounts() as client:
        _login(client, "admin", "admin123")
        r = client.post("/admin/users", json={"name": "admin", "password": "brand-new-pw", "role": "admin"})
        assert r.status_code == 200, r.text
        _login(client, "admin", "brand-new-pw")
        assert client.get("/api/me").json()["user"]["role"] == "admin"
        assert _login(client, "admin", "admin123").status_code == 401, "旧口令应即刻失效"


def test_short_password_rejected_and_role_must_be_known():
    with _accounts() as client:
        _login(client, "admin", "admin123")
        assert client.post("/admin/users", json={"name": "赵文宇", "password": "123"}).status_code == 400
        # 非管理员名 + 合法口令：新建为制单员
        assert client.post("/admin/users", json={"name": "赵文宇", "password": "pw-123456"}).status_code == 200
        assert {u["name"] for u in client.get("/admin/users").json()["users"]} >= {"赵文宇"}


def test_users_json_posix_permissions_600():
    if os.name == "nt":
        return                                  # Windows 的 chmod 只管只读位，断言无意义（由 VM 回归覆盖）
    with _accounts() as client:
        _login(client, "admin", "admin123")
        client.post("/admin/users", json={"name": "叶庭伸", "password": "pw-123456"})
        assert auth.USERS_FILE.stat().st_mode & 0o777 == 0o600, "账号表含哈希与签名 secret，必须 600"


def test_login_gate_makes_the_page_behind_clearly_inert():
    """遮罩只有五成透明度时，背后的按钮看着完全可用：用户点了"今日台账"却毫无反应，
    以为功能坏了（2026-10-01 实测）。未登录时要么看不见背后的控件，要么明确点不动。
    2026-10-03 色值归口到 tokens.css：判的还是同一个 alpha，只是值搬到了 --c-gate 上，
    所以这里先跟着 var() 解析一次，别把它当成"遮罩没色值了"。"""
    css = web_src.part("css/desk.css")
    i = css.index(".gate{")
    gate = css[i:css.index("}", i) + 1]
    ref = re.search(r"background:\s*var\(--([\w-]+)\)", gate)
    if ref:
        hit = re.search(r"--%s\s*:\s*([^;}]+)" % ref.group(1), web_src.part("css/tokens.css"))
        assert hit, f"遮罩底色挂在 --{ref.group(1)} 上，tokens.css 里却没这个令牌"
        alpha_at = hit.group(1)
    else:
        alpha_at = gate
    m = re.search(r"rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*([0-9.]+)\s*\)", alpha_at)
    assert m and float(m.group(1)) >= 0.8, f"登录遮罩太薄（alpha {m and m.group(1)}）：背后控件看着仍可点"
    assert "backdrop-filter" in gate, "再加一层模糊：未登录时页面要明显是停着的"
    js = web_src.part("js/desk.js")
    assert re.search(r"\.inert\s*=\s*", js), "未登录时顶栏与两个视图要标 inert：点不动、Tab 也进不去"


def test_role_name_says_who_logged_in():

    """角色中文名是给人看的身份提示，说错了比没说更糟：录入员顶栏被写成"制单员"
    （§3.4 第 6 条），他会以为自己没登录成功；未知角色宁可原样显示，也别替他猜。"""
    for role, cn in (("admin", "管理员"), ("reviewer", "制单员"), ("inputter", "录入员")):
        assert web_src.logic("L.roleCn(r)", r=role) == cn, f"{role} 的角色名不对"
    assert web_src.logic("L.roleCn(r)", r="auditor") == "auditor", "后端新加的角色不该被显示成别人的身份"
    html = web_src.desk()
    assert "L.roleCn(ME.role)" in html, "顶栏角色名要过映射，别再 admin?管理员:制单员 这样二分"
    assert 'admin ? "管理员" : "制单员"' not in html, "二分会把录入员说成制单员"
    assert "esc(L.roleCn(u.role))" in html, "账号表里的角色也要映射 + 转义：那是 users.json 里的字符串"


def test_interpolated_text_is_escaped_including_quotes():
    """账号名/单号/错误 message 都是外部字符串，插进 innerHTML 前必须过 esc。
    单引号也要转：属性值里有 ' 就能把一段文本变成一段事件处理器。"""
    js = web_src.part("js/desk.js")
    assert "const esc = L.esc;" in js, "转义算式挪进 logic.js，好让 node 断言它的输出"
    assert web_src.logic("L.esc(s)", s="""a<b>&'\"""") == "a&lt;b&gt;&amp;&#39;&quot;", "转义表缺项"
    assert "|| u.role}" not in js, "未转义的插值兜底会原样吐出 users.json 里的字符串"


def test_toast_queue_does_not_swallow_the_earlier_message():
    """一次失败往往同时冒出两条原因（"这张票没提交" + "登录已过期"），后一条把前一条覆盖掉，
    制单员就只能对着半句线索猜。提示改队列 + aria-live（§3.4 第 5 条）。"""
    html = web_src.desk()
    page = web_src.part("index.html")
    assert 'id="toasts"' in page and 'aria-live="polite"' in page, "提示容器要成组，并让读屏器知道有新消息"
    body = html[html.index("function toast("):html.index("function txt(")]
    assert "createElement" in body and "appendChild" in body, "toast 要各自成一条，不能再往同一个节点覆写"
    assert '"#toast"' not in body, "还指着单个提示节点：后一条会把前一条吃掉"
    assert "lastChild" in body or "firstChild" in body, "队列要有上限：堆几十条等于没有"



# === 前端接线（静态检查，同其它 desk 用例口径） ===
def test_desk_has_login_gate_and_role_scoped_controls():
    html = web_src.desk()
    assert 'id="gate"' in html and 'id="loginForm"' in html, "没有登录遮罩/表单"
    assert '"/login"' in html and '"/logout"' in html and '"/api/me"' in html, "登录/登出/自检接口没接上"
    assert '"/admin/users"' in html and "账号管理" in html, "管理员账号管理面板没了"
    gate = "function applyRole(){"
    i = html.find(gate)
    assert i > -1, "applyRole 没了：角色门控整体失效"
    body = html[i:html.find("\n}", i)]
    assert '$("#fModel").hidden = !admin' in body, "模型下拉没按角色收起"
    assert '$("#btnAcct").hidden = !admin' in body, "账号管理入口没限定管理员"
    # JS 设 hidden 之外，CSS 必须补 .f[hidden]{display:none}：#fModel 是 class="f"，
    # 而 .f{display:flex} 会盖过 UA 的 [hidden]{display:none}，缺这条则收起对制单员形同虚设。
    assert ".f[hidden]{display:none}" in html, "缺 .f[hidden] 规则：模型下拉/身份条收不起来（.f 的 flex 盖过 hidden）"
    assert "checkAuth()" in html, "启动没先做登录自检"
    assert "rv.readOnly = true" in body, "复核人没锁成登录身份，提交留痕可被冒名"
    assert 'id="fKey"' not in html, "接口密钥输入框已随 HTTP_API_KEY 一并删除"
    assert "apiKey" not in html and "X-API-Key" not in html, "前端还留着 X-API-Key 送密钥的路径"
