# -*- coding: utf-8 -*-
"""HTTP 接口回归：登录门禁、上传落盘名、大小上限、不支持类型。
用 TestClient 在本进程里跑，handle_file 打桩，不产生任何 API 调用。
2026-09-22 起：服务端不再接受 X-API-Key，唯一门槛是 users.json 里登录后的会话 Cookie；
users.json 在 _stubbed() 里被指向临时目录，_login_admin() 用默认口令 admin/admin123 换 Cookie。
"""
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import server

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity" / "gl26090330_hawb.json"


@contextlib.contextmanager
def _stubbed():
    """替换 server 用到的重依赖，返回记录下来的调用参数。
    同时把 users.json 指到临时目录：main() 会 ensure_seed，各类登录用例也别在包里落下真账号表。"""
    calls = []
    case = json.loads(FIX.read_text(encoding="utf-8"))
    real_hf, real_store = server.handle_file, server.store

    def fake_handle_file(path, save=True):
        calls.append({"path": Path(path), "save": save, "exists": Path(path).exists()})
        return {"stem": Path(path).stem, "channel": "vlm", "elapsed": 0.1,
                "raw": case["raw"], "air": {"MAWB_NO": "235-96146363"},
                "transcript": {"lines": [{"i": 1, "text": "MAWB NO. 999-0686 5456", "bbox": [0, 0, 100, 10]},
                                          {"i": 2, "text": "HAWB CLA26090022", "bbox": [0, 12, 100, 22]}],
                               "full_text": "MAWB NO. 999-0686 5456\nHAWB CLA26090022"},
                "warns": [], "fidelity": None, "archive": None,
                "qc": {"needs_review": True, "flags": ["MAWB_NO 缺失"], "fidelity": None,
                       "error": "", "md5": "stub"},
                "error": ""}

    class _FakeStore:
        save_preview = staticmethod(real_store.save_preview)   # 票面预览要真落盘，测试才验得了缓存

        def rebuild_summary(self):
            calls.append({"rebuild": True})

    server.handle_file, server.store = fake_handle_file, _FakeStore()
    old_users = auth.USERS_FILE
    users_tmp = Path(tempfile.mkdtemp(prefix="hawb_stub_users_"))
    auth.USERS_FILE = users_tmp / "users.json"
    try:
        yield calls
    finally:
        server.handle_file, server.store = real_hf, real_store
        auth.USERS_FILE = old_users
        shutil.rmtree(users_tmp, ignore_errors=True)


def _login_admin(client):
    """用种子管理员账号登录，Cookie 会留在 client 上，之后的调用都带过去。"""
    r = client.post("/login", json={"name": "admin", "password": "admin123"})
    assert r.status_code == 200, f"默认管理员登录失败：{r.status_code} {r.text}"


def _post(client, filename="CLA26090022\xa0HAWB.pdf", content=b"%PDF-1.4 fake-hawb", **kw):
    return client.post("/extract", files={"file": (filename, content, "application/pdf")},
                       params=kw.pop("params", {}), headers=kw.pop("headers", {}))


def test_health_is_open_and_no_longer_reports_auth_flag():
    """/health 免登录；auth_required 字段随 HTTP_API_KEY 一并下线，别再回。"""
    with _stubbed():
        r = TestClient(server.app).get("/health")
        assert r.status_code == 200, r.status_code
        assert "auth_required" not in r.json(), "auth_required 已经失去含义：HTTP_API_KEY 删了就该一起走"


def test_version_label_shows_on_both_ends():
    """V1 定版（2026-09-26 用户点名）：/health 报出版本，审核台把 /health 的 version 显示到标题旁。
    版本号只在 config.APP_VERSION 写一次——页面写死一份的话，改了 config 页面还在骗人。
    V2 是另一套对接真网页的形态，两台并存时只凭端口号分不出谁是谁。"""
    with _stubbed():
        assert TestClient(server.app).get("/health").json().get("version") == config.APP_VERSION
    assert '"version": config.APP_VERSION' in (Path(server.__file__).read_text(encoding="utf-8")), \
        "健康检查里的版本号要跟 config 同源，别再各处写死一遍"
    for page in ("index.html", "inputter.html"):
        html = (server.WEB_DIR / page).read_text(encoding="utf-8")
        assert 'id="verTag"' in html and ".version" in html, f"{page} 要把版本号显示出来"


def test_endpoints_refuse_without_login():
    """没有会话就是 401；X-API-Key 请求头不再被认（HTTP_API_KEY 已从 config 删除）。"""
    with _stubbed():
        client = TestClient(server.app)
        assert _post(client).status_code == 401, "未登录却放行了"
        assert _post(client, headers={"X-API-Key": "whatever"}).status_code == 401, "还认 X-API-Key？"
        assert client.get("/results").status_code == 401


def test_login_unlocks_extract_and_reports_flags():
    client = TestClient(server.app)
    with _stubbed():
        _login_admin(client)
        ok = _post(client)
        assert ok.status_code == 200, ok.text
        assert ok.json()["ok"] and ok.json()["stem"] == "CLA26090022\xa0HAWB"
        assert ok.json()["qc"]["needs_review"] is True and ok.json()["qc"]["flags"], \
            "红旗必须随响应返回，否则业务系统拿不到复核信号"


def test_disk_name_follows_the_ticket_not_the_tempfile():
    client = TestClient(server.app)
    with _stubbed() as calls:
        _login_admin(client)
        r = _post(client, params={"save": "true"})
        assert r.status_code == 200, r.text
        used = [c for c in calls if "path" in c][0]
        assert used["save"] is True
        assert used["path"].name == "CLA26090022\xa0HAWB.pdf", used["path"]  # 原名 + NBSP 保留，好对账
        assert used["exists"], "handle_file 调用时临时文件必须还在"
        assert not used["path"].parent.exists(), "上传临时目录没清理"
        assert any(c.get("rebuild") for c in calls), "落盘后要重建汇总"


def test_traversal_and_missing_extension_are_neutralised():
    for name, want in [(r"..\..\windows\x.pdf", "x.pdf"), ("../../etc/passwd", "passwd.pdf"),
                       ("noext", "noext.pdf"), ("", "upload.pdf"), ("恶意:名<>.pdf", "恶意_名__.pdf")]:
        got = server.safe_name(name, ".pdf")
        assert got == want, f"{name!r} -> {got!r}，期望 {want!r}"
        assert not any(c in got for c in "\\/:*?\"<>|"), got
    assert server.safe_name("a.pdf", ".pdf") == "a.pdf", "别把扩展名拼成 a.pdf.pdf"


def test_site_worker_safe_name_matches_server():
    """站点取单与 HTTP 上传是两个独立入口，落盘名净化必须同规则。
    Linux 上 '\\' 不是路径分隔符：只按 os.path 取 basename 会留下 '.._.._windows_x.pdf' 这种脏名。"""
    import site_worker
    for name in (r"..\..\windows\x.pdf", "../../etc/passwd", "a.pdf", "noext", "", "恶意:名<>.pdf"):
        got = site_worker.safe_name(name)
        assert got == server.safe_name(name, ""), f"{name!r}: 站点入口 {got!r} != HTTP 入口"
        assert not any(c in got for c in "\\/:*?\"<>|"), got


def test_oversize_upload_rejected():
    client = TestClient(server.app)
    old = config.UPLOAD_MAX_MB
    config.UPLOAD_MAX_MB = 1
    try:
        with _stubbed() as calls:
            _login_admin(client)
            r = _post(client, content=b"x" * (1024 * 1024 + 2048))
            assert r.status_code == 413, r.status_code
            assert not [c for c in calls if "path" in c], "超限文件不该进入提取管道"
    finally:
        config.UPLOAD_MAX_MB = old


def test_unsupported_suffix_rejected():
    client = TestClient(server.app)
    with _stubbed() as calls:
        _login_admin(client)
        r = client.post("/extract", files={"file": ("a.txt", b"x", "text/plain")})
        assert r.status_code == 400, r.status_code
        assert not calls


def test_extraction_runs_off_the_event_loop():
    """同步的 7-20s 提取必须丢线程池，否则并发请求会排队把 /health 一起卡死。"""
    src = (server.__file__ and Path(server.__file__).read_text(encoding="utf-8"))
    assert "run_in_threadpool(handle_file" in src, "handle_file 又回到事件循环里同步执行了"
    assert "await file.read()" not in src, "整体 read() 会把超大上传一次性吃进内存，要分块写盘"


def test_main_starts_and_seeds_accounts():
    """main() 起来就该：把默认账号种到 users.json（别落到包目录），并把 uvicorn 拉起来。
    公网绑定不再靠 HTTP_API_KEY 拦，改为一句提醒改默认口令——这是内部署，同事登录即用。"""
    import sys

    old_argv, old_run = sys.argv, server.uvicorn.run
    tmp = Path(tempfile.mkdtemp(prefix="hawb_main_users_"))
    old_users = auth.USERS_FILE
    auth.USERS_FILE = tmp / "users.json"
    started = []
    server.uvicorn.run = lambda app, **kw: started.append(kw)
    try:
        sys.argv = ["server.py", "--host", "0.0.0.0"]
        server.main()
        assert started and started[0]["host"] == "0.0.0.0", "公网绑定不再被拒（唯一门槛是登录）"
        assert auth.USERS_FILE.is_file(), "main() 要把默认账号种进 users.json"
    finally:
        sys.argv = old_argv
        server.uvicorn.run = old_run
        auth.USERS_FILE = old_users
        shutil.rmtree(tmp, ignore_errors=True)


def test_desk_page_is_served_without_login():
    """登录页本身免会话（不然还没登录就看不到表单）：/ 只发静态壳，但不能漏发 charset。"""
    client = TestClient(server.app)
    with _stubbed():
        r = client.get("/")
        assert r.status_code == 200, r.status_code
        assert "主分单审核台" in r.text and 'charset="utf-8"' in r.text
        assert "text/html" in r.headers["content-type"]


def test_desk_page_missing_is_reported_not_500():
    import tempfile

    real = server.WEB_DIR
    with tempfile.TemporaryDirectory() as d:
        server.WEB_DIR = Path(d)
        try:
            assert TestClient(server.app).get("/").status_code == 404
        finally:
            server.WEB_DIR = real


def test_desk_pdf_viewer_hides_its_sidebar_and_can_show_the_whole_page():
    """浏览器实测反馈：Chrome 阅读器默认带缩略图侧栏，票面栏本来就窄，它还要占掉近一半，
    票面只剩半个看不全。用开放参数关掉侧栏、默认适应宽度，并留「整页看全 / 适应宽度」开关。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    frame = re.search(r"function pdfFrame\(\)\{(.*?)\n\}", html, re.S)
    assert frame, "pdfFrame 没了：换成别的方式加载 PDF 时，这条断言要跟着改"
    body = frame.group(1)
    assert "navpanes=0" in body, "没关阅读器侧栏，票面又被挤掉一半"
    assert "view=${PREVIEW.pdfFit}" in body, "整页/适应宽度开关没接到 iframe 上"
    assert 'id="pvPage"' in html and '$("#pvPage").addEventListener' in html, "开关按钮或它的接线没了"


def test_desk_hidden_toggles_beat_class_display_rules():
    """浏览器实测踩过：.pvtools{display:flex} 的优先级压过 UA 的 [hidden]{display:none}，
    没有票面时票面工具栏照样亮着（static 检查看不出来，只有真开页面才发现）。
    JS 用 .hidden 开合的元素，样式表里必须有 [hidden] 兜底。"""
    css = (server.WEB_DIR / "index.html").read_text(encoding="utf-8").split("<style>", 1)[1].split("</style>", 1)[0]
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)     # 注释里的规则不生效，别被自己的注释骗过
    guarded = " ".join(m.group(1) for m in re.finditer(r"([^{}]*\[hidden\][^{}]*)\{([^}]*)\}", css)
                       if "none" in m.group(2))
    for sel in (".pvtools", "#pvZoom", "#pvLoc"):
        rule = re.search(re.escape(sel) + r"[^{}]*\{[^}]*display", css)
        assert rule, f"{sel} 不再用 display 布局了：这条断言该退休，别放着误导人"
        assert sel in guarded, f"{sel} 的 display 没有 [hidden] 兜底，JS 里的 .hidden 会失效"


def test_health_reports_source_files_newer_than_the_process():
    """真事：票面浏览上线后制单员一直看不了票面——服务窗口是改动前启动的旧进程，
    uvicorn 不带 --reload 只在启动时读一次代码。服务得自己说出「我在跑旧代码」，
    否则只能靠人去猜「是不是没重启」。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stale_test_"))
    old_boot, old_pkg = server._BOOT, server._PKG_DIR
    try:
        before, after = tmp / "before.py", tmp / "after.py"
        before.write_text("# 进程启动前就在", encoding="utf-8")
        after.write_text("# 进程启动后才改", encoding="utf-8")
        os.utime(before, (old_boot - 60, old_boot - 60))
        os.utime(after, (old_boot + 60, old_boot + 60))
        server._PKG_DIR = tmp
        client = TestClient(server.app)
        with _stubbed():
            assert client.get("/health").json()["stale_files"] == ["after.py"], \
                "启动后改过的源码没被报出来，页面就没法提示重启"
            server._BOOT = time.time() + 3600        # 模拟刚重启：所有源码都比进程旧
            assert client.get("/health").json()["stale_files"] == [], "正常重启后不该再报旧代码"
        assert TestClient(server.app).get("/health").status_code == 200, "健康检查要免鉴权，改坏鉴权就探测不到了"
    finally:
        server._BOOT, server._PKG_DIR = old_boot, old_pkg
        shutil.rmtree(tmp, ignore_errors=True)


def test_desk_warns_when_the_service_is_running_stale_code():
    """旧进程这件事对制单员是透明的：页面要把「服务端代码改了没重启」写在明面上，并说清怎么重启。"""
    with _stubbed():
        assert "stale_files" in TestClient(server.app).get("/health").json(), \
            "服务端不再报 stale_files，页面的提示会永远沉默"
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    probe = re.search(r"async function probe\(\)\{(.*?)\n\}", html, re.S)
    assert probe, "probe 没了：健康检查的提示逻辑挪了地方，这条断言要跟着改"
    body = probe.group(1)
    assert "stale_files" in body, "页面不看 stale_files，旧进程照样静默"
    assert "4_启动HTTP服务.bat" in body, "要直接说清怎么重启，别只说「请重启服务」这种没着落的话"


def test_desk_clear_drafts_is_bound_to_the_button_itself():
    """真事：制单员点「本地暂存 → 清空」没反应。按钮在卡片标题栏里，而监听挂在 #drafts 列表上，
    点击根本不经过列表——委托要挂在会收到事件的那个元素上。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="clearDrafts"' in html, "清空按钮没了：这条断言该退休"
    assert '$("#clearDrafts").addEventListener' in html, "清空按钮没有自己的监听，点了还是没反应"
    drafts_handler = re.search(r'\$\("#drafts"\)\.addEventListener\("click",.*?\n\}\);', html, re.S)
    assert drafts_handler, "#drafts 的点击处理挪了地方，这条断言要跟着改"
    assert "clearDrafts" not in drafts_handler.group(0), \
        "又把清空塞回 #drafts 的委托里了——它不在列表里，收不到点击"


def test_desk_preview_pane_fills_the_viewport_height():
    """浏览器实测：.wrap 是 align-items:start，网格不会把票面那一格拉高，靠内容撑只有半屏
    （票面看一半、下面空着）。给死高度让它铺满可视高度；窄屏一屏放不下，必须改回 auto。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", html.split("<style>", 1)[1].split("</style>", 1)[0], flags=re.S)
    base = re.search(r"#view\{([^}]*position:sticky[^}]*)\}", css)
    assert base, "#view 的粘住规则没了：这条断言要跟着改"
    assert re.search(r"(?<!max-)height:calc\(100vh", base.group(1)), \
        "票面栏没有可视高度（max-height 不算，它撑不高）：又会缩回半屏"
    phone = re.search(r"@media \(max-width:1020px\)\{(.*?)\n\}", css, re.S)
    assert phone, "窄屏媒体查询没了"
    assert re.search(r"#view\{[^}]*height:auto", phone.group(1)), \
        "窄屏没把死高度改回 auto，手机上一屏塞不下"


@contextlib.contextmanager
def _archive(files: dict):
    """把归档/预览目录指到临时目录：审核台回看票面只读这两个目录，测试别去碰真实 output/。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_source_test_"))
    old_a, old_p = config.ARCHIVE_DIR, config.PREVIEW_DIR
    config.ARCHIVE_DIR, config.PREVIEW_DIR = tmp / "archive", tmp / "preview"
    for rel, data in files.items():
        p = config.ARCHIVE_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        yield config.ARCHIVE_DIR
    finally:
        config.ARCHIVE_DIR, config.PREVIEW_DIR = old_a, old_p
        shutil.rmtree(tmp, ignore_errors=True)


def test_source_serves_the_archived_ticket_inline():
    """制单员要在页面里直接看到票面：PDF 原件必须内联发出去，带上 attachment 就变成下载了。"""
    client = TestClient(server.app)
    with _stubbed(), _archive({"CLA26090022/CLA26090022.pdf": b"%PDF-1.4 ticket"}):
        _login_admin(client)
        r = client.get("/source/CLA26090022")
        assert r.status_code == 200, r.status_code
        assert r.content == b"%PDF-1.4 ticket"
        assert r.headers["content-type"].startswith("application/pdf")
        assert "attachment" not in r.headers.get("content-disposition", ""), "预览要内联"


def test_source_falls_back_to_the_converted_pdf_for_excel():
    """电子单原件是 xlsx，浏览器渲染不了：该发 LibreOffice 转出的那张 PDF——正是模型看过的那张。"""
    client = TestClient(server.app)
    with _stubbed(), _archive({"TAO10359760/TAO10359760.xlsx": b"PK\x03\x04fake-xlsx"}):
        _login_admin(client)
        config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        (config.PREVIEW_DIR / "TAO10359760.pdf").write_bytes(b"%PDF-1.4 converted")
        r = client.get("/source/TAO10359760")
        assert r.status_code == 200, r.text
        assert r.content == b"%PDF-1.4 converted"
        assert r.headers["content-type"].startswith("application/pdf")


def test_source_converts_an_old_excel_ticket_on_demand():
    """本功能上线前归档的电子单里只有 xlsx、没有转出的 PDF：看的时候现转，转完缓存下来，
    老票也能在审核台里浏览，不必等谁重跑一遍提取。"""
    client = TestClient(server.app)
    calls, tmp = [], Path(tempfile.mkdtemp(prefix="hawb_conv_test_"))
    src = tmp / "converted.pdf"
    src.write_bytes(b"%PDF-1.4 late")
    real = server.xlsx2pdf.convert_excel_to_pdf
    server.xlsx2pdf.convert_excel_to_pdf = lambda p, **kw: (calls.append(Path(p)), src)[1]
    try:
        with _stubbed(), _archive({"OLD-EXCEL-1/OLD-EXCEL-1.xls": b"\xd0\xcf\x11\xe0fake-xls"}):
            _login_admin(client)
            r = client.get("/source/OLD-EXCEL-1")
            assert r.status_code == 200 and r.content == b"%PDF-1.4 late", r.text
            assert [c.name for c in calls] == ["OLD-EXCEL-1.xls"], "要转的必须是归档里那份原件"
            assert (config.PREVIEW_DIR / "OLD-EXCEL-1.pdf").exists(), "转出来要落缓存，别每次都等 LibreOffice"
            assert client.get("/source/OLD-EXCEL-1").status_code == 200
            assert len(calls) == 1, "第二次该走缓存"
    finally:
        server.xlsx2pdf.convert_excel_to_pdf = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_source_explains_when_excel_conversion_is_impossible():
    """服务器没装 LibreOffice（比如精简部署）时别只给个 415：说清原因，且「下载原件」照旧可用。"""
    client = TestClient(server.app)

    def boom(p, **kw):
        raise FileNotFoundError("未找到 LibreOffice，请安装或设 SOFFICE_PATH")

    real = server.xlsx2pdf.convert_excel_to_pdf
    server.xlsx2pdf.convert_excel_to_pdf = boom
    try:
        with _stubbed(), _archive({"OLD-EXCEL-2/OLD-EXCEL-2.xlsx": b"PK\x03\x04fake-xlsx"}):
            _login_admin(client)
            r = client.get("/source/OLD-EXCEL-2")
            assert r.status_code == 415 and "LibreOffice" in r.json()["detail"], r.text
            d = client.get("/source/OLD-EXCEL-2?raw=1")
            assert d.status_code == 200 and d.content == b"PK\x03\x04fake-xlsx", "转不出来也得能下载原件"
    finally:
        server.xlsx2pdf.convert_excel_to_pdf = real


def test_source_refuses_to_escape_the_archive_root():
    """stem 来自 URL：必须是单段目录名，'..' 这类一律不认。
    注意 HTTP 那一层要用百分号编码——明文的 '/source/..' 会被客户端 URL 规范化成 '/'，
    根本到不了服务端（探到的是首页，不是漏洞）。"""
    client = TestClient(server.app)
    with _stubbed(), _archive({"CLA26090022/CLA26090022.pdf": b"%PDF-1.4 ticket"}):
        _login_admin(client)
        for stem in ("..", "../..", r"..\..", ".", "", "nope", "a/b"):
            got = server._archive_dir(stem)
            assert got is None, f"{stem!r} 不该解析出目录: {got}"
        assert server._archive_dir("CLA26090022") is not None, "正常票号要能解析"
        for stem in ("%2e%2e", "..%5c..", "%2e%2e%5c%2e%2e", "..%2f..", ".", "nope"):
            r = client.get("/source/" + stem)
            assert r.status_code == 404, f"{stem!r} -> {r.status_code}"


def test_source_needs_login_and_explains_missing_archive():
    client = TestClient(server.app)
    with _stubbed(), _archive({}):
        assert client.get("/source/CLA26090022").status_code == 401, "未登录要拒绝"
        _login_admin(client)
        r = client.get("/source/CLA26090022")
        assert r.status_code == 404 and "落盘留档" in r.json()["detail"], r.text


def test_source_raw_downloads_what_the_browser_cannot_render():
    """TIFF / Excel 这类预览不了的原件，得留一条「下载原件」的路，别只报个错。"""
    client = TestClient(server.app)
    with _stubbed(), _archive({"SALCN0002822/SALCN0002822.tif": b"II*\x00fake-tiff"}):
        _login_admin(client)
        r = client.get("/source/SALCN0002822")
        assert r.status_code == 415, r.status_code
        d = client.get("/source/SALCN0002822?raw=1")
        assert d.status_code == 200 and d.content == b"II*\x00fake-tiff"
        assert "attachment" in d.headers.get("content-disposition", ""), "下载要带 attachment 与文件名"
        assert "SALCN0002822.tif" in d.headers["content-disposition"]


def test_models_list_is_open_and_marks_the_current_one():
    """审核台要先填出下拉（那会儿还没登录）：清单免鉴权，但必须如实带上视觉标记。"""
    client = TestClient(server.app)
    with _stubbed():
        r = client.get("/models")
        assert r.status_code == 200, r.status_code
        j = r.json()
        assert [p["key"] for p in j["presets"]] == list(config.MODEL_PRESETS), "预设被改了或顺序动了"
        assert j["current"] == config.VLM_MODEL_CHOICE and j["effective"] == config.VLM_MODEL
        by = {p["key"]: p for p in j["presets"]}
        assert by["intern-s2-official"]["model"] == "intern-s2-preview", "官方预设没经 /models 传下去"
        assert all(p["vision"] is True for p in j["presets"]), \
            "vision 标记错了会让纯文本模型收到图片——它不会拒答，会顺着编"


def test_switch_model_needs_admin_login_and_writes_dotenv():
    """切换要落两处：本进程立即生效（不用重启），并写回 .env（重启后、CLI 批处理照样用）。
    未登录 → 401；管理员会话 → 200。"""
    client = TestClient(server.app)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_env_"))
    old_env_file = config.ENV_FILE
    old_state = (config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION)
    config.ENV_FILE = tmp / ".env"
    config.ENV_FILE.write_text("INTERNLM_API_KEY=sk-x\n", encoding="utf-8")
    try:
        with _stubbed():
            assert client.post("/model", json={"model": "intern-s2-official"}).status_code == 401, \
                "换模型没要登录"
            _login_admin(client)
            r = client.post("/model", json={"model": "intern-s2-official"})
            assert r.status_code == 200 and r.json()["ok"], r.text
            assert config.VLM_MODEL == "intern-s2-preview" and config.MODEL_VISION is True
            assert "VLM_MODEL=intern-s2-official" in config.ENV_FILE.read_text(encoding="utf-8"), "选择没写回 .env，重启就丢"
            assert client.get("/health").json()["model"] == "intern-s2-preview", "健康检查没跟上新模型"
            r3 = client.post("/model", json={"model": "Qwen/Qwen3.5-27B"})
            assert r3.status_code == 200 and config.VLM_MODEL == "Qwen/Qwen3.5-27B", "原始模型 id 也要能直接填"
    finally:
        config.ENV_FILE = old_env_file
        config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION = old_state
        shutil.rmtree(tmp, ignore_errors=True)


def test_switch_model_rejects_injection_and_empty():
    """选择会拼进 .env：换行/等号/空格能伪造出别的配置行，值必须在门口就挡住。"""
    client = TestClient(server.app)
    with _stubbed():
        _login_admin(client)
        for bad in ("", "   ", "deepseek\nSOME_INJECTED_LINE=evil", "a=b", "模型 带空格"):
            r = client.post("/model", json={"model": bad})
            assert r.status_code == 400, f"{bad!r} -> {r.status_code}"
        assert client.post("/model", json={"nope": 1}).status_code == 400


def test_desk_model_selector_is_wired_to_models_and_model_endpoints():
    """模型切换要在制单员手边（限管理员）：顶栏下拉从 /models 取数、切换 POST /model；
    纯文本模型还得在顶栏当场说明扫描件会失败，不然他只会看到一堆失败任务。
    会话 Cookie 由浏览器同源自动带上，前端不再发 X-API-Key。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="modelSel"' in html, "模型下拉没了"
    load = re.search(r"async function loadModels\(\)\{(.*?)\n\}", html, re.S)
    assert load and re.search(r'fetch\((?:BASE \+ )?"/models"\)', load.group(1)), "下拉没有从 /models 取数"
    assert "probe(); loadModels();" in html, "启动时没填下拉"
    handler = re.search(r'\$\("#modelSel"\)\.addEventListener\("change", async e => \{(.*?)\n\}\);', html, re.S)
    assert handler, "下拉的 change 处理没了：选中新模型也不会切换"
    assert '"/model"' in handler.group(1), "切换请求没打到 /model"
    assert "X-API-Key" not in handler.group(1), "前端还在发 X-API-Key：接口密钥已删，只走会话 Cookie"
    probe = re.search(r"async function probe\(\)\{(.*?)\n\}", html, re.S)
    assert probe and "vision" in probe.group(1), "纯文本模型（vision=false）没有在顶栏提示"


def test_extract_carries_the_l1_transcript():
    """点字段定位票面要逐行 bbox：L1 转录必须随 /extract 一起回来，
    否则本次会话刚解析的票在页面上没法定位，只能等刷新走 /layout。"""
    client = TestClient(server.app)
    with _stubbed():
        _login_admin(client)
        j = _post(client).json()
        assert j["transcript"]["lines"] and all("bbox" in l for l in j["transcript"]["lines"]), \
            "转录没带上或 bbox 丢了：定位模式没有叠层依据"


@contextlib.contextmanager
def _transcripts(files: dict):
    """转录目录指到临时目录：/layout 只读 output/transcript/，测试别碰真实落盘。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_tr_test_"))
    old = config.TRANSCRIPT_DIR
    config.TRANSCRIPT_DIR = tmp
    for rel, data in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes)
                      else json.dumps(data, ensure_ascii=False).encode("utf-8"))
    try:
        yield tmp
    finally:
        config.TRANSCRIPT_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_layout_serves_stored_transcript_with_page_count():
    """本地暂存载入的老票要靠 /layout 补转录；pages 从行里的 page 取，前端据此出翻页按钮。"""
    client = TestClient(server.app)
    tr = {"lines": [{"i": 1, "text": "SHIPPER ALPHA", "bbox": [0, 0, 100, 10]},
                    {"i": 2, "text": "页面二货物", "bbox": [1, 2, 3, 4], "page": 2}]}
    with _stubbed(), _transcripts({"CLA26090022.json": tr}):
        _login_admin(client)
        r = client.get("/layout/CLA26090022")
        assert r.status_code == 200, r.text
        j = r.json()
        assert len(j["lines"]) == 2 and j["pages"] == 2, j
        assert client.get("/layout/NOPE").status_code == 404, "登录了但没这个转录就是 404"
    anon = TestClient(server.app)
    with _stubbed(), _transcripts({"CLA26090022.json": tr}):
        assert anon.get("/layout/CLA26090022").status_code == 401, "转录接口也要登录"


def test_layout_explains_missing_and_refuses_traversal():
    from urllib.parse import quote
    client = TestClient(server.app)
    with _stubbed(), _transcripts({"坏文件.json": b"{oops"}):
        _login_admin(client)
        r = client.get("/layout/CLA26090022")
        assert r.status_code == 404 and "落盘留档" in r.json()["detail"], r.text
        bad = client.get("/layout/" + quote("坏文件"))
        assert bad.status_code == 500 and "转录文件读坏" in bad.json()["detail"], "读坏要说清怎么修"
        for stem in ("%2e%2e", "..%5c..", "a%2fb", "."):
            assert client.get("/layout/" + stem).status_code == 404, \
                f"{stem!r} 不该穿出转录目录"


def _mkpdf(pages: int = 2) -> bytes:
    """pymupdf 现做一份真 PDF：/render 要真渲染，假字节只会被拒。"""
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"HAWB test page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def test_render_returns_page_png_and_total_pages_header():
    """定位模式底图：PDF 按页转 PNG，总页数走 X-Ticket-Pages 头给前端出翻页。"""
    client = TestClient(server.app)
    server._RENDER_CACHE.clear()
    try:
        with _stubbed(), _archive({"GLT1/GLT1.pdf": _mkpdf(2)}):
            _login_admin(client)
            r = client.get("/render/GLT1?page=2&scale=2")
            assert r.status_code == 200, r.text
            assert r.headers["content-type"] == "image/png"
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n", "发出的必须真是 PNG"
            assert r.headers["x-ticket-pages"] == "2"
            again = client.get("/render/GLT1?page=2&scale=2")
            assert again.content == r.content and again.status_code == 200, "同页重取要走缓存"
            assert client.get("/render/GLT1?page=99").status_code == 200, \
                "页码越界该钳到末页而不是报错"
            assert client.get("/render/GLT1?scale=50").status_code == 200
            assert not any(k[2] == 50 for k in server._RENDER_CACHE), "scale 没钳就被缓存，键还能被撑爆"
            assert client.get("/render/NOPE").status_code == 404
    finally:
        server._RENDER_CACHE.clear()
    anon = TestClient(server.app)
    with _stubbed(), _archive({"GLT1/GLT1.pdf": _mkpdf(2)}):
        assert anon.get("/render/GLT1").status_code == 401, "未登录要拒"


def test_render_serves_image_tickets_and_degrades_honestly():
    """图票底图就是原件本身；TIFF 浏览器看不了但 pymupdf 读得动——读不懂的坏文件要 415 说清。"""
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                           "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    client = TestClient(server.app)
    with _stubbed(), _archive({"PIC1/PIC1.png": png,
                               "BAD1/BAD1.tif": b"II*\x00not-a-real-tiff"}):
        _login_admin(client)
        r = client.get("/render/PIC1")
        assert r.status_code == 200 and r.content == png, "png 直接发原图当底图"
        assert r.headers["content-type"].startswith("image/")
        bad = client.get("/render/BAD1")
        assert bad.status_code == 415 and "下载原件" in bad.json()["detail"], bad.text


def test_desk_field_click_locates_value_on_the_ticket():
    """制单员定的用法：点右边字段名 → 左边票面切按页渲染的页图，按 L1 bbox 叠高亮并自动放大。
    Chrome 内嵌 PDF 阅读器做不了按区域高亮，这条链一断，功能整块失联。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'class="k loc"' in html, "字段名格子不再是定位入口"
    wire = re.search(r'if \(!\$\("#main"\)\._locWired\)\{(.*?)\n  \}', html, re.S)
    assert wire and "enterLocate(" in wire.group(1), "点字段名的委托没了或又能重复挂"
    enter = re.search(r"async function enterLocate\(t, k\)\{(.*?)\n\}", html, re.S)
    assert enter, "enterLocate 挪走了：这条断言要跟着改"
    body = enter.group(1)
    assert "/render/" in body, "定位底图没走服务端按页渲染"
    assert "matchLines(" in body and "fieldValCandidates(" in body, "值→转录行的匹配断了"
    layout = re.search(r"async function layoutFor\(t\)\{(.*?)\n\}", html, re.S)
    assert layout and "_llP" in layout.group(1) and "/layout/" in layout.group(1), \
        "老票补转录的 /layout 兜底断了（含在途 Promise 共享）"
    assert "ll:j.transcript" in html, "提取响应里的转录没存到票上"
    show = re.search(r"async function showLocPage\(page, seq\)\{(.*?)\n\}", html, re.S)
    assert show and 'class="bbox"' in show.group(1) and "left:${x1 / 10}%" in show.group(1), \
        "bbox 覆盖层断了：0-1000 归一坐标要换算成百分比"
    assert "X-Ticket-Pages" in show.group(1), "没读服务端报的总页数，多页票翻不动"
    fit = re.search(r"function locFit\(\)\{(.*?)\n\}", html, re.S)
    assert fit and "pane.clientHeight * 100 / (3 * bh100)" in fit.group(1) and 'style.width = z + "%"' in fit.group(1), \
        "自动放大断了：命中行要占到窗格约三分之一高"
    assert re.search(r'normFace = |function normFace', html), "匹配归一（NFKC+空白压合）没了"
    assert ".normalize(\"NFKC\")" in html, "全角归一走 NFKC，和 fidelity 的 _FW 同口径"
    assert 'id="pvLocExit"' in html and '$("#pvLocExit").addEventListener("click", exitLocate)' in html, \
        "没有「返回原视图」，制单员就困在定位模式里了"
    rp = re.search(r"function renderPreview\(\)\{(.*?)\n\}", html, re.S)
    assert rp and "exitLocate()" in rp.group(1), "切票不退出定位模式，正常预览会被页图卡住"


def test_desk_actions_on_top_columns_scroll_apart_and_boxes_fit_content():
    """2026-09-21 用户三条 UI 意见：①提交/导出三键挪到字段表顶部（原来在 39 行下面，
    每次提交都要滚到最底）；②字段列自己滚，别带着左边上传/排队列表一起动；
    ③收发货人长文本框按内容自适应高度（原来按 "\n" 数行数，一整段没有换行就只有一行高）。"""
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    top = html.find('class="ft top"')
    assert top > -1, "顶部操作条没了：提交/导出又滚回表尾去了"
    seg = html[top:html.find("wireMain(t)", top)]
    for bid in ('id="submit"', 'id="expOne"', 'id="expAll"'):
        assert bid in seg, f"顶部操作条少了 {bid}"
    assert seg.find("id=") < seg.find("<table"), "提交键没在字段表上面"
    css = re.sub(r"/\*.*?\*/", "", html.split("<style>", 1)[1].split("</style>", 1)[0], flags=re.S)
    wide = re.search(r"@media \(min-width:1461px\)\{(.*?)\n\}", css, re.S)
    assert wide, "三栏独立滚动的媒体查询没了：滚字段又会带着整页一起动"
    assert re.search(r"#main\{[^}]*position:sticky[^}]*overflow:auto", wide.group(1)), \
        "#main 没做成自己的滚动容器"
    assert "#main th{top:0}" in wide.group(1), \
        "#main 变滚动容器后表头吸顶参照要归零，否则 th 会缩到卡片中间"
    assert re.search(r"td\.v textarea\{[^}]*max-height", css), "长文本框没封顶：一条超长整串能把卡片撑爆"
    ctl = re.search(r"function control\(t, k\)\{(.*?)\n\}", html, re.S)
    assert ctl and "rows=" not in ctl.group(1), "control 又按换行数 rows 了——整段不换行的长文本会缩成一行"
    assert re.search(r"function autoGrow\(el\)\{", html), "autoGrow 没了"
    assert re.search(r'\$\("#main"\)\.querySelectorAll\("textarea"\)\.forEach\(autoGrow\)', html), \
        "渲染后没给文本框量高：打开时还是一行"
    wire = re.search(r"if \(inp\.tagName === \"TEXTAREA\"\)(.*?)\n", html)
    assert wire and "autoGrow(inp)" in wire.group(1), "边打字没跟着长高"
