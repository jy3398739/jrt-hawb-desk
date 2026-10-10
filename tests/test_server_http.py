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
import web_src

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity" / "gl26090330_hawb.json"


@contextlib.contextmanager
def _stubbed():
    """替换 server 用到的重依赖，返回记录下来的调用参数。
    同时把 users.json 指到临时目录：main() 会 ensure_seed，各类登录用例也别在包里落下真账号表。"""
    calls = []
    case = json.loads(FIX.read_text(encoding="utf-8"))
    real_hf, real_store = server.handle_file, server.store

    def fake_handle_file(path, save=True, meta=None):
        calls.append({"path": Path(path), "save": save, "meta": meta,
                      "exists": Path(path).exists()})
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


def test_desk_waits_for_the_parse_job_instead_of_blocking_on_upload():
    """上传接口改成"入队 + 轮询"后，前端必须跟着改：位次要说得出口、排队已满要说人话、
    服务重启把作业弄丢了要自己重传一次（不能让人以为票丢了）。"""
    html = web_src.desk()
    assert '"/job/"' in html, "没有轮询 /job：上传又会在 HTTP 上干等十几分钟"
    assert "排队中 · 前面" in html, "排队要给出位次，不然和卡死看不出区别"
    assert "r.status === 429" in html, "排队已满要单独说，别混成一句 HTTP 错误"
    assert "t._requeued" in html, "作业 404（服务重启）要自动重传一次"
    assert 'if (!j.queued) return j' in html, "老后端同步返回时前端要能照旧用（不把结果当作业丢进轮询）"
    busy = html[html.index("function statusOf("):]
    busy = busy[:busy.index("\nfunction ")]
    assert "t.hint" in busy, "busy 状态要能显示队列给的位置，只显示「解析中…」等于没说"


def test_desk_polls_through_one_scheduler_that_backs_off_and_pauses():
    """三处"等结果"从前各自 setTimeout 递归：固定节奏、失败也照打、页面切走还在打，
    登录一过期就变成 401 死循环——制单员看着像卡死，其实是客户端在敲一扇不再开的门。"""
    js = web_src.part("js/desk.js")
    assert "function poller(" in js and "POLLERS" in js, "要有一个统一调度器，而不是三处各自递归"
    assert js.count("L.pollDelay(") == 1, "退避算法只该有一处：两处算间隔就会长成两个节奏"
    assert 'document.addEventListener("visibilitychange"' in js, \
        "页面切走要停手：制单员切去 Excel 的那几分钟不该继续打服务器"
    assert "visibilitychange" in js and "p.step" in js, "切回前台要立刻补一次，而不是等下一个退避周期"
    for a, b, tag in (("function mstPoll(", "/* ── 主单核对与提交", "mstPoll"),
                      ("function mvPoll(", "async function mvSubmit(", "mvPoll"),
                      ("async function waitJob(", "async function runQueue(", "waitJob")):
        body = js[js.index(a):js.index(b, js.index(a))]
        assert "poller(" in body, tag + "：还在自己排期，没走调度器"
        assert "401" in body, tag + "：拿回 401 要停下并把登录遮罩亮出来，否则就是 401 死循环"
        assert "setTimeout(async" not in body, tag + "：递归排期没清干净"
    assert "MV_TIMER" not in js, "主单核对的轮询句柄要交给调度器管，留着裸 timer 就会漏停"


def test_web_assets_are_served_no_store_and_traversal_free():
    """拆成 css/ js/ 之后，页面靠 /web/{路径} 取资源：免登录（登录页也得有样式），
    但只能拿这两个目录下的一段文件名，且一律 no-store——缓存里留半份旧脚本就是"点了没反应"。"""
    client = TestClient(server.app)
    for path, kind in (("/web/css/tokens.css", "text/css"), ("/web/js/desk.js", "javascript")):
        r = client.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        assert kind in r.headers["content-type"], r.headers["content-type"]
        assert r.headers.get("cache-control") == "no-store", f"{path} 不能进缓存"
    # 注意用 %2e%2e：写成 ".." 会被 httpx 在客户端就把路径归一掉，测不到服务端那道判断
    for bad in ("/web/../.env", "/web/css/%2e%2e/js/desk.js", "/web/css/notes.html",
                "/web/css/", "/web/data/x.css", "/web/css/%2e%2e%2f%2e%2e%2f.env"):
        assert client.get(bad).status_code in (400, 404), f"这个路径不该放行：{bad}"


def test_desk_page_links_assets_by_relative_path_and_keeps_no_inline_bodies():
    """页面只留骨架：样式与脚本各自成文件，且用相对路径引用——挂在 /hawb/ 下也能取到。"""
    page = web_src.part("index.html")
    assert "<style>" not in page, "页面里不该再嵌样式正文"
    assert page.count("<script") == 2 and 'src="web/js/logic.js"' in page and 'src="web/js/desk.js"' in page, \
        "脚本只留外链（logic 纯逻辑 + desk 接线），不该再有内联正文"
    assert 'href="web/css/tokens.css"' in page and 'src="web/js/desk.js"' in page, \
        "引用要相对路径（绝对 /css/... 在子路径挂载下会打到域名根）"


def test_every_css_token_is_defined():
    """令牌层立起来之后，最怕的是"用了没定义的颜色"——浏览器静默回退，肉眼在浅底上看不出来。"""
    tokens = web_src.part("web/css/tokens.css")
    defined = set(re.findall(r"(--[\w-]+)\s*:", tokens))
    used = set()
    for rel in ("web/css/desk.css", "web/js/desk.js"):
        used |= set(re.findall(r"var\((--[\w-]+)[),]", web_src.part(rel)))
    assert not (used - defined), f"这些令牌用了却没人定义：{sorted(used - defined)}"


def _lum(h: str) -> float:
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(fg: str, bg: str) -> float:
    hi, lo = sorted([_lum(fg), _lum(bg)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_tokens_pass_wcag_aa_on_their_real_backgrounds():
    """对比度按「这个字实际会压在什么底上」算，不是一句「所有文字对所有底色」。
    2026-10-03 换控制塔色板时，外部方案那套值里有四个过不了这一关：--c-text-3 用 #64748b 在页面底
    只有 4.32、#d97706 2.89、#dc2626 4.38、#059669 3.42——因为它把「状态点」和「状态文字」当成
    同一个颜色用了。点 ≥3.0 就够，小字必须 ≥4.5，所以绿橙红各配两个值，这张表就是盯这件事的。
    去卡片化会把文字从白底搬到页面底，所以每个文字角色都要在它真实会出现的每种底上过。
    分隔线（--c-line*）故意不过 AA：它的作用就是轻，别被人「顺手修」成看得见的粗边框。"""
    raw = dict(re.findall(r"(--[\w-]+)\s*:\s*#([0-9a-fA-F]{3,6})", web_src.part("web/css/tokens.css")))
    tok = {k: ("".join(c * 2 for c in v) if len(v) == 3 else v) for k, v in raw.items()}

    def ratio(fg, bg, floor, tag):
        assert fg in tok, f"{tag}：令牌 {fg} 没定义成十六进制色值，这张表算不了它"
        assert bg in tok, f"{tag}：令牌 {bg} 没定义成十六进制色值，这张表算不了它"
        r = _ratio(tok[fg], tok[bg])
        assert r >= floor, f"{fg}(#{tok[fg]}) 在 {bg}(#{tok[bg]}) 上只有 {r:.2f}:1，{tag} 要 ≥{floor}:1"

    LIGHT = ["--c-surface", "--c-bg", "--c-surface-2", "--c-surface-3", "--c-canvas",
             "--c-neutral", "--c-accent-sel", "--c-accent-glow", "--c-edited"]
    for ink in ("--c-text-1", "--c-text-2", "--c-text-3"):
        for bg in LIGHT:
            ratio(ink, bg, 4.5, "正文/说明文字")
    for fg, bgs in (("--c-accent", ["--c-surface", "--c-bg", "--c-surface-2", "--c-accent-soft"]),
                    ("--c-ok-text", ["--c-surface", "--c-bg", "--c-ok-bg", "--c-accent-sel"]),
                    ("--c-warn-text", ["--c-surface", "--c-bg", "--c-warn-bg", "--c-warn-strip", "--c-edited"]),
                    ("--c-bad-text", ["--c-surface", "--c-bg", "--c-bad-bg", "--c-accent-sel"])):
        for bg in bgs:
            ratio(fg, bg, 4.5, "带语义的文字")
    for dark in ("--c-accent", "--c-toast", "--c-toast-ok", "--c-toast-bad"):
        ratio("--c-inverse", dark, 4.5, "深色底上的白字（主按钮/toast）")
    for fill in ("--c-ok", "--c-warn", "--c-bad", "--c-accent"):
        for bg in ("--c-surface", "--c-bg", "--c-surface-2", "--c-canvas", "--c-accent-sel"):
            ratio(fill, bg, 3.0, "状态点/竖条这类图形")


def test_state_fill_tokens_are_never_used_as_text():
    """「绿橙红各配两个值」这件事写在注释里一定会被忘，所以立一条静态规矩：
    --c-ok / --c-warn / --c-bad 是"看得见的点"（≥3.0 就够），带 -text 的那支才是"读得清的字"
    （≥4.5）。谁把前者写进 color:，浅底上的 11px 小字就退回 4.38:1——正是这轮换色板要修的错。
    主色 --c-accent 例外：它两个角色都用，本身 6.7:1 够。"""
    css = _code(web_src.part("css/desk.css"))
    bad = [ln.strip()[:56] for ln in css.splitlines() if re.search(r"(?<!-)color:var\(--c-(ok|warn|bad)\)", ln)]
    assert not bad, "状态填充色被当成文字色用了，改 --c-*-text：" + " / ".join(bad)


""" ── 设计令牌的三条守卫（2026-10-03 立，先红后绿）────────────────────────────
   外部方案说 desk.css 有 50 处硬编码色值——实测 69 处 hex + 6 处 rgba；它还说内联样式只有
   index.html 的 8 处，漏了 desk.js 模板字符串里的 32 处（比 html 多一倍）。守卫扫的是三处，
   不立起来的话「归口令牌」这件事每一轮都会重做一遍。"""

# 十六进制色：3/4/6/8 位才算，长度 5 的一律不是色值（否则 $("#acAdd") 这种选择器会被当成颜色）
_COLOR = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b|rgba?\(")


def _code(text: str) -> str:
    """去掉块注释但保住行数：注释里举的色值不生效，别被自己的注释判成违规，
    而报错给的行号要能直接跳到那一行（注释经常跨行，整段删掉就全偏了）。"""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)


def test_color_literals_only_live_in_tokens():
    """颜色只有 tokens.css 一个来源。写死一处，换色板那天就得全文搜索，而「浅底小字糊成一片」
    这类问题（§3.2）正是会在几十个写死值里被漏掉的那一类。tokens.css 里允许出现色值，
    但只能出现在变量定义上——否则等于又开了第二个来源。"""
    offenders = []
    for rel in ("web/css/desk.css", "web/index.html", "web/js/desk.js"):
        for n, line in enumerate(_code(web_src.part(rel)).splitlines(), 1):
            if _COLOR.search(line):
                offenders.append("%s:%d" % (rel, n))
    assert not offenders, "色值写死了，该走 var(--…)：" + "、".join(offenders[:14]) + \
        ("…（共 %d 处）" % len(offenders) if len(offenders) > 14 else "")
    residue = re.sub(r"--[\w-]+\s*:[^;}]+", "", _code(web_src.part("web/css/tokens.css")))
    assert not _COLOR.search(residue), "tokens.css 里有色值没挂在变量定义上"


def test_no_inline_style_except_runtime_forms():
    """内联 style 是第二份样式表：不吃令牌、desk.css 里搜不到，去卡片化/换色板必然漏。
    只有「值当场算出来」的才留在行内——票面 bbox 的百分比、台账的列宽比例，它们是数据不是装饰。"""
    allowed = ('style="left:', '<col style="width:')
    offenders = []
    for rel in ("web/index.html", "web/js/desk.js"):
        for n, line in enumerate(web_src.part(rel).splitlines(), 1):
            if "style=" in line and not any(a in line for a in allowed):
                offenders.append("%s:%d %s" % (rel, n, line.strip()[:56]))
    assert not offenders, "这些内联样式该收成 class：\n" + "\n".join(offenders[:24])


def test_measured_tokens_are_re_measured():
    """吸顶高度是量出来的，不是抄下来的：--todoH 从前写死，待办条一换行就把第一行字段压住，
    后来改成 JS 实测；--hdh 却还留在 tokens.css 里当常量，而顶栏在 1020–1460px 会折成两排，
    于是样式里那三处 64px（窄轨/票面栏/字段栏的 sticky top）在折行那天全都对不上。
    所以两个令牌都要 JS 写入 + 尺寸变化后重测，样式里的魔法数换成派生令牌。"""
    js, css = web_src.part("js/desk.js"), _code(web_src.css())
    for name in ("--hdh", "--todoH"):
        assert 'setProperty("%s"' % name in js, f"{name} 没由 JS 实测写入，静态值早晚和真实高度不一致"
    assert "ResizeObserver" in js, "只测一次不算实测：窗口变窄顶栏折行后要重测"
    for magic in ("top:64px", "calc(100vh - 78px)"):
        assert magic not in css, f"「{magic}」是抄来的魔法数，该由 var(--stick)/栏高派生令牌给"
    assert "#main.has-todo th{top:var(--todoH)}" in css, "表头给待办条让位的那条引用别丢"


def test_desk_motion_is_keyframes_not_class_toggles():
    """三个动效都写成 @keyframes，不靠"加一个类触发 transition"：窄轨展开的列表和票面页图都是
    当场建、当场插的节点，同一帧里改类浏览器根本不会过渡（外部方案给的 .railList.open / img.on
    就是这么颗死按钮——而且 img 那条还只在没解码完时才挂 load，票面会永久空白）。
    减少动态效果要逐个点名：一把 *{animation:none!important} 会把"我点到哪儿了"那两个闪色也关掉。"""
    css, js = _code(web_src.part("css/desk.css")), web_src.part("js/desk.js")
    for kf, user in (("pvIn", ".pv img.pvpage{animation:pvIn"), ("railIn", ".railList{animation:railIn"),
                     ("flashb", "@keyframes flashb")):
        assert "@keyframes " + kf in css, kf + " 这个动效没了"
        assert user in css, kf + " 定义了却没接到元素上"
    rm = re.search(r"@media \(prefers-reduced-motion:reduce\)\{(.*?)\n\}", css, re.S)
    assert rm, "没有减少动态效果的降级：前庭功能敏感的人只能用浏览器缩放自救"
    assert "animation:none" in rm.group(1) and "transition:none" in rm.group(1)
    assert "!important" not in rm.group(1), "别用大锤关动画：那两个闪色是反馈，不是装饰"
    assert "scroll-behavior:smooth" not in css, \
        "不给 #pvBody 写全局 smooth：installPan 每次 pointermove 都在写 scrollLeft/Top，那会让拖动变果冻"
    assert "behavior:" in js[js.index("function locFit()"):js.index("function pvSetTools()")], \
        "定位巡航要走 scrollTo 并尊重减少动态效果"


def test_desk_clickables_are_real_buttons_with_visible_focus():
    """可点的非元素（票行、字段名、chip、还原、分单号）从前是带 cursor:pointer 的 div/span/a：
    键盘 Tab 到不了、回车不触发、读屏器念不出它是可点的（§3.4 第 2 条）。
    另外 textarea/input 的 focus 描边被 outline:none 吃掉过——改哪一格看不见光标在哪。"""
    js, css, page = web_src.part("js/desk.js"), web_src.part("css/desk.css"), web_src.part("index.html")
    for frag, why in (('class="row', "票行"), ('class="rs"', "还原"), ('data-jump', "红旗 chip"),
                      ('data-open', "分单号"), ('data-jump-field', "字段名定位")):
        hits = [l for l in js.splitlines() if frag in l and "<button" in l]
        assert hits, f"{why} 要变成真正的 <button>"
    assert "outline:none" not in css, "focus 描边被抹掉：改字段时看不见光标落在哪一格"
    assert ":focus-visible" in css, "要有键盘可见的 focus 描边（鼠标点击不必显示）"
    assert 'aria-current' in js, "选中的票行要让读屏器知道哪一条是当前"
    assert "role=\"status\"" in page or 'aria-live="polite"' in page, "错误提示与状态文字要能被读屏器念出来"
    for i in ("mstNo", "acName", "acPass"):
        assert re.search(r'id="%s"[^>]*(aria-label|aria-labelledby)' % i, page) or \
               re.search(r'<label[^>]*>[^<]*<input[^>]*id="%s"' % i, page), f"{i} 只有 placeholder：占位文字不是标签"
    sm = css[css.index(".btn.sm"):]
    assert "min-height" in sm[:200], "触屏/拖拽目标要够大（原先 .btn.sm 只有 22px 高）"



def test_health_says_which_build_is_running():
    """部署是手工 tar 推文件，中断/漏推就会新旧混跑；stale_files 只说"有些文件比进程新"，
    说不出"这台跑的是哪一版、什么时候的代码"。/health 要能一句话回答。"""
    with _stubbed():
        j = TestClient(server.app).get("/health").json()
        assert "commit" in j and isinstance(j["commit"], str), "commit：本机有 git 就报短哈希，服务器上没仓库就空串"
        assert j.get("built_at"), "built_at：源码里最新那份文件的时刻"
        assert j.get("started_at"), "started_at：进程启动时刻（和 built_at 一比就知道是不是没重启）"
        assert j["queue"]["slots"] >= 1 and "waiting" in j["queue"], "队列实况要在 /health 里看得见"
        for k in ("built_at", "started_at"):
            assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", j[k]), f"{k} 要给人能读的本地时间：{j[k]}"


def _post(client, filename="CLA26090022\xa0HAWB.pdf", content=b"%PDF-1.4 fake-hawb", **kw):
    """上传一张票并取回最终结果。/extract 现在是"入队即 202 + 轮询 /job"，
    用例不该各写一遍轮询；要断言 202/位次本身的地方直接 client.post。"""
    r = client.post("/extract", files={"file": (filename, content, "application/pdf")},
                    params=kw.pop("params", {}), headers=kw.pop("headers", {}))
    if r.status_code != 202:
        return r

    class _R:
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    jid = r.json()["job"]
    for _ in range(300):
        d = client.get("/job/" + jid).json()
        if d["state"] == "done":
            r = d["result"]
            return _R(200 if r.get("ok") else 500, r)
        if d["state"] == "failed":
            return _R(500, {"ok": False, "error": d.get("error", "")})
        time.sleep(0.02)
    raise AssertionError("排队/解析没在预期时间内结束")


def test_extract_queues_the_ticket_and_the_job_carries_the_result():
    """/extract 不再让人在 HTTP 上等整段解析（最坏十几分钟，nginx 900s 先断、后端还在烧钱）。
    入队即 202，位次给前端说"排队中·前面 N 张"。"""
    client = TestClient(server.app)
    with _stubbed():
        _login_admin(client)
        r = client.post("/extract", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["job"] and isinstance(body["position"], int)
        jid = body["job"]
        for _ in range(300):
            d = client.get("/job/" + jid).json()
            if d["state"] in ("done", "failed"):
                break
            time.sleep(0.02)
        assert d["state"] == "done", d
        assert d["result"]["stem"] == "a" and d["result"]["transcript"]["lines"], \
            "结果体要和从前 /extract 直接返回的一模一样（前端只多了轮询一步）"


def test_job_endpoint_needs_login_and_unknown_jobs_are_404():
    client = TestClient(server.app)
    assert client.get("/job/nope12345678").status_code == 401, "作业结果含票面内容，要登录"
    with _stubbed():
        _login_admin(client)
        assert client.get("/job/nope12345678").status_code == 404, "没这个作业就说没有"


def test_post_is_not_blocked_by_a_stuffed_queue():
    """队列塞满要立刻回 429（前端提示稍后再传），不能继续接进来让每个人都超时。"""
    real = server.DESK_QUEUE.submit
    server.DESK_QUEUE.submit = lambda *a, **k: (_ for _ in ()).throw(
        server.desk_queue.QueueFull("排队中已有 200 张，请等一会儿再传"))
    try:
        client = TestClient(server.app)
        with _stubbed():
            _login_admin(client)
            r = _post(client)
            assert r.status_code == 429, f"{r.status_code} {getattr(r, '_body', None)}"
            assert "等一会儿" in str(r.json()), f"要告诉人是稍后再传：{r.json()}"
    finally:
        server.DESK_QUEUE.submit = real


def test_health_is_open_and_no_longer_reports_auth_flag():
    """/health 免登录；auth_required 字段随 HTTP_API_KEY 一并下线，别再回。"""
    with _stubbed():
        r = TestClient(server.app).get("/health")
        assert r.status_code == 200, r.status_code
        assert "auth_required" not in r.json(), "auth_required 已经失去含义：HTTP_API_KEY 删了就该一起走"
        j = r.json()
        assert j.get("master_model"), "/health 要报主单链用的模型：两条链默认不同渠道，只报一个会看着像配错"
        assert "master_key_configured" in j


def test_version_label_shows_on_both_ends():
    """版本要能当凭据用：一个真正的发布号（semver）+ 形态标记 + 构建时间。
    只写 "V1" 的话，"服务器跑的是哪一版" 没人答得了（2026-10-01 用户点名要版本号）。
    版本号只在 config 里写一次——页面写死一份的话，改了 config 页面还在骗人。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", config.APP_VERSION), \
        f"APP_VERSION 该是 1.0.0 这种 semver，现在是 {config.APP_VERSION!r}"
    with _stubbed():
        h = TestClient(server.app).get("/health").json()
    assert h["version"] == config.APP_VERSION
    assert h["form"] == config.FORM, "形态标记（V1）单独报：两台并存时靠它分辨，不占版本号"
    assert h["built_at"] and h["started_at"], "没有 git 的服务器上，构建时间与进程启动时间就是唯一的版本凭据"
    srv = Path(server.__file__).read_text(encoding="utf-8")
    assert '"version": config.APP_VERSION' in srv and '"form": config.FORM' in srv, \
        "健康检查里的版本要跟 config 同源，别再各处写死一遍"
    js = web_src.part("web/js/desk.js")
    assert 'id="verTag"' in web_src.part("index.html"), "页面要有版本号的位置"
    assert "r.version" in js and "r.built_at" in js and "r.form" in js, \
        "标题旁要报出 版本 + 形态 + 构建时间，缺一样就又回到猜"


def test_release_tooling_can_answer_which_version_the_server_runs():
    """发版是一条命令，且部署完能自证"服务器上跑的确实是这个号"。"""
    from pathlib import Path as P
    rel = P(__file__).resolve().parent.parent / "deploy" / "release.sh"
    assert rel.is_file(), "缺 deploy/release.sh：没有统一发版入口，版本号很快就会和代码脱节"
    t = rel.read_text(encoding="utf-8")
    for step in ("run_tests.py", "git tag", "sync.sh", "/health"):
        assert step in t, f"发版脚本少了 {step} 这一步"
    sync = (P(__file__).resolve().parent.parent / "deploy" / "sync.sh").read_text(encoding="utf-8")
    assert "version" in sync and "APP_VERSION" in sync, \
        "sync.sh 要核对服务器上报的版本与本机 config 一致，否则'部署完成'只是文件推上去了"



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
        r = _post(client)
        assert r.status_code == 200, r.text
        used = [c for c in calls if "path" in c][0]
        assert used["save"] is True
        assert used["path"].name == "CLA26090022\xa0HAWB.pdf", used["path"]  # 原名 + NBSP 保留，好对账
        assert used["exists"], "handle_file 调用时临时文件必须还在"
        assert not used["path"].parent.exists(), "上传临时目录没清理"
        assert any(c.get("rebuild") for c in calls), "落盘后要重建汇总"


def test_uploading_a_ticket_always_persists_it():
    """上传就必须落盘，而且这事不再由前端决定。

    从前 `POST /extract` 的 save 默认 False，值来自浏览器里那个「落盘留档」复选框：不勾则
    L0 归档 / L1 转录 / L2 / L3 / 质检一个字节都不写，上传的原件还被当场删掉。需求一撞的正是
    这个——录入员在主单检索里查不到制单员刚上传的票（那些票压根没进服务器），换台机器更是一张都不认识。

    所以 save 这个参数现在整个不认：不传、传 true、传 false 都一律落盘。老前端带着
    ?save=false 也不能绕过（它只会被忽略），直接 curl 调接口的人同理。
    落盘本身写不写由 jobs.handle_file 那组用例盯（save=False 一个字节都不写 / True 全写），
    这条只钉住"HTTP 这道门口径恒真"。"""
    client = TestClient(server.app)
    with _stubbed() as calls:
        _login_admin(client)
        for q, tag in (({}, "不带参数"), ({"save": "true"}, "save=true"),
                       ({"save": "false"}, "save=false")):
            r = _post(client, filename="PERSIST.pdf", params=q)
            assert r.status_code == 200, f"{tag} 上传失败：{r.json()}"
            used = [c for c in calls if "path" in c][-1]
            assert used["save"] is True, f"{tag} 上传居然没落盘，别人就检索不到这张票"
            assert any(c.get("rebuild") for c in calls), f"{tag} 落盘后没重建汇总"


def test_upload_records_the_person_who_uploaded_it():
    """登录身份要一路传到落盘那一步。

    从前 `who` 只进了两行日志文本和内存里的作业表（`server.py` 的 LOG.info / DeskQueue.submit），
    进程重启就查无此人；而提交台账记的是**复核人**，不是上传人。需求三要按人分账
    （制单员改了几张、录入员改了几张），第一步就得先把"这张票是谁传上来的"写进 qc。
    身份取会话，不信前端传来的名字——和复核人同一个口径。"""
    client = TestClient(server.app)
    with _stubbed() as calls:
        _login_admin(client)
        assert _post(client).status_code == 200
        used = [c for c in calls if "path" in c][-1]
        assert used["meta"]["uploader"] == "admin", f"上传人没传到解析这一步：{used['meta']}"
        assert used["meta"]["uploader_role"] == "admin", used["meta"]


def test_desk_stops_asking_whether_to_keep_the_ticket():
    """那个复选框描述的是一个已经不存在的用法："这次别留下痕迹"。审核台里每一张票都要
    能被别人查到，所以复选框、发给 /extract 的 save 参数、以及票对象上的 t.save 一起删掉。
    留着最坏的后果不是难看：勾掉的那张票在服务器上不存在，而制单员以为同事能看见它。"""
    page = web_src.part("index.html")
    assert "doSave" not in page, "「落盘留档」复选框还在：落不落盘不该由浏览器决定"
    js = web_src.part("js/desk.js")
    assert "extract?save=" not in js, "前端还在给 /extract 传 save"
    assert not re.search(r"\bt\.save\b", js), \
        "票对象上还带着 save：这个字段已经没有意义，留着会骗到按页渲染那条分支"
    # 复选框没了，提示语就不许再教人去勾它：照着找框的人会以为系统坏了，而真正的原因
    #（这张票早于本功能上传、或归档被清理过）一句都没说到。
    for rel in ("index.html", "js/desk.js"):
        assert "落盘留档" not in web_src.part(rel), f"{rel} 还在让人勾一个已经不存在的框"
    srv = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    assert "落盘留档" not in srv, "服务器的 404 提示还在让人去勾那个框"


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
    """同步的 7-20s 提取必须离开事件循环，否则并发请求会排队把 /health 一起卡死。
    现在由解析队列承担这一层（自己的线程池 + 并发上限），不再直接丢 anyio 线程池。"""
    src = Path(server.__file__).read_text(encoding="utf-8")
    assert "DESK_QUEUE.submit(" in src, "handle_file 又回到事件循环里同步执行了"
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


def test_desk_page_calls_every_endpoint_under_its_mount_prefix():
    """服务器把审核台挂在 /hawb/ 子路径下（nginx 443 反代），页面里所有请求都必须带 BASE 前缀。
    分单提交吃过这个亏：`SUBMIT_URL = "/submit"` 写死根路径，POST 打到域名根 → 404 →
    前端按"接口尚未开通"本地暂存，用户在服务器上以为没开通（2026-09-29 实测）。"""
    html = web_src.desk()
    assert 'const SUBMIT_URL = BASE + "/submit"' in html, "分单提交口要跟着挂载前缀走"
    bare = [m.group(1).strip()[:46] for m in re.finditer(r"fetch\(\s*([^,)\n]+)", html)
            if not m.group(1).strip().startswith(("BASE", "LOC.base", "SUBMIT_URL", "blobUrl"))]
    # blobUrl 那条：票面字节是本页 createObjectURL 出来的 blob，读它只为扫 MediaBox，不出网
    assert not bare, f"这些 fetch 没有 BASE 前缀，挂到 /hawb/ 下会打到域名根：{bare}"


def test_desk_page_is_served_without_login():
    """登录页本身免会话（不然还没登录就看不到表单）：/ 只发静态壳，但不能漏发 charset。"""
    client = TestClient(server.app)
    with _stubbed():
        r = client.get("/")
        assert r.status_code == 200, r.status_code
        assert "主分单审核台" in r.text and 'charset="utf-8"' in r.text
        assert "text/html" in r.headers["content-type"]
        # 页面不缓存：改了 index.html 又重启了服务，浏览器还拿旧壳 → 新按钮点了没反应（2026-09-28 用户实测撞到）
        assert r.headers.get("cache-control") == "no-store", "审核台页面要禁缓存"


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
    票面只剩半个看不全。用开放参数关掉侧栏、默认适应宽度，并留一个切档按钮。
    2026-10-02 又实测一条：窄而高的栏里 view=Fit 与 FitH 是同一个缩放，那颗按钮点了不动——
    所以档位改由 L.viewCycle 按页面尺寸与栏位算（重合的丢掉、补一档真放大的），这里就核到"参数从算出来的档位来"。"""
    html = web_src.desk()
    frame = re.search(r"function pdfFrame\(\)\{(.*?)\n\}", html, re.S)
    assert frame, "pdfFrame 没了：换成别的方式加载 PDF 时，这条断言要跟着改"
    body = frame.group(1)
    assert "navpanes=0" in body, "没关阅读器侧栏，票面又被挤掉一半"
    assert "L.viewCycle(" in body and "cur.frag" in body, "档位没走 logic 或没接到 iframe 地址上"
    assert "cycle.length < 2" in body, "只剩一档时要把按钮藏掉，别留一颗点了不动的死按钮"
    assert 'id="pvPage"' in html and '$("#pvPage").addEventListener' in html, "切档按钮或它的接线没了"


def test_desk_hidden_toggles_beat_class_display_rules():
    """浏览器实测踩过两次：① .pvtools{display:flex} 的优先级压过 UA 的 [hidden]{display:none}，
    没有票面时票面工具栏照样亮着；② 2026-10-10 同一族——.wrap{display:grid} 让今日台账里
    `$("#wrap").hidden = true` 白设，核对工作台照旧占着 956px 高，把台账表整张顶到首屏之外
    （用户的原话是"只保留底部的台账内容，不需要分单解析上半部分"）。
    这条测试原来的洞恰恰是**清单手抄**：只查 .pvtools/#pvZoom/#pvLoc，没人往清单里加 #wrap
    就永远查不出来。⇒ 元素从 desk.js 里捞，且只认两种满足方式：该元素自己有 [hidden] 兜底，
    或样式表有一条全局 [hidden] 兜底。"""
    css = web_src.css()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)     # 注释里的规则不生效，别被自己的注释骗过
    html = web_src.part("index.html")
    js = web_src.part("js/desk.js")
    guarded = " ".join(m.group(1) for m in re.finditer(r"([^{}]*\[hidden\][^{}]*)\{([^}]*)\}", css)
                       if "none" in m.group(2))
    global_guard = re.search(r"(^|[,\s])\[hidden\](?![^\s{])[^{}]*\{[^}]*display\s*:\s*none", css)
    ids = sorted(set(re.findall(r'\$\("#([A-Za-z0-9_]+)"\)\.hidden', js)))
    assert "wrap" in ids, "视图切换不再藏 #wrap 了：这条断言该跟着改，别让它以为台账还叠在工作台下面"
    for i in ids:
        tag = re.search(r"<\w+[^>]*id=\"" + re.escape(i) + r"\"[^>]*>", html)
        if not tag:
            continue                                    # 动态建的元素，样式表管不到
        cls = (re.findall(r'class="([^"]*)"', tag.group(0)) or [""])[0]
        sels = ["#" + i] + ["." + c for c in cls.split()]
        shown = [s for s in sels if re.search(re.escape(s) + r"[^{}]*\{[^}]*display", css)]
        assert global_guard or all(s in guarded for s in shown), \
            i + " 用 .hidden 开合，但 " + "/".join(s for s in shown if s not in guarded) + \
            " 的 display 没有 [hidden] 兜底——JS 里的 .hidden 会失效（元素照样占位或照样亮着）"
    assert global_guard, "样式表里没有全局 [hidden] 兜底：靠逐个元素补规则迟早漏一个（#wrap 就漏过）"


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
    html = web_src.desk()
    probe = re.search(r"async function probe\(\)\{(.*?)\n\}", html, re.S)
    assert probe, "probe 没了：健康检查的提示逻辑挪了地方，这条断言要跟着改"
    body = probe.group(1)
    assert "stale_files" in body, "页面不看 stale_files，旧进程照样静默"
    assert "4_启动HTTP服务.bat" in body, "要直接说清怎么重启，别只说「请重启服务」这种没着落的话"


def test_desk_clear_drafts_is_bound_to_the_button_itself():
    """真事：制单员点「本地暂存 → 清空」没反应。按钮在卡片标题栏里，而监听挂在 #drafts 列表上，
    点击根本不经过列表——委托要挂在会收到事件的那个元素上。"""
    html = web_src.desk()
    assert 'id="clearDrafts"' in html, "清空按钮没了：这条断言该退休"
    assert '$("#clearDrafts").addEventListener' in html, "清空按钮没有自己的监听，点了还是没反应"
    drafts_handler = re.search(r'\$\("#drafts"\)\.addEventListener\("click",.*?\n\}\);', html, re.S)
    assert drafts_handler, "#drafts 的点击处理挪了地方，这条断言要跟着改"
    assert "clearDrafts" not in drafts_handler.group(0), \
        "又把清空塞回 #drafts 的委托里了——它不在列表里，收不到点击"


def test_desk_preview_pane_fills_the_viewport_height():
    """浏览器实测：.wrap 是 align-items:start，网格不会把票面那一格拉高，靠内容撑只有半屏
    （票面看一半、下面空着）。给死高度让它铺满可视高度；窄屏一屏放不下，必须改回 auto。"""
    html = web_src.desk()
    css = re.sub(r"/\*.*?\*/", "", web_src.css(), flags=re.S)
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
        assert r.status_code == 404 and "归档" in r.json()["detail"], r.text


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


def test_master_chain_model_is_visible_and_switchable_separately():
    """两条链各用哪个模型要在界面上看得见、管理员能各切各的：
    切主单不能把分单那行改掉（反之亦然），否则下拉框就成了互相打架的开关。"""
    client = TestClient(server.app)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_env_"))
    old_env_file, old_state = config.ENV_FILE, config.MASTER_VLM_MODEL
    old_hawb = (config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION)
    config.ENV_FILE = tmp / ".env"
    # 两条链一开始都指 S2，然后把**主单**切到 qwen：这样"切主单却把分单也换了"一定会露出来
    # （两边解析出的 model 不同）。渠道删到只剩两个以后，用同一个键当开关是测不出串台的。
    config.MASTER_VLM_MODEL = "intern-s2-official"     # 回归入口把它钉成空（=跟分单同渠道），这里显式设
    config.ENV_FILE.write_text("INTERNLM_API_KEY=sk-x\nQWEN_API_KEY=sk-y\n"
                               "VLM_MODEL=intern-s2-official\nMASTER_VLM_MODEL=intern-s2-official\n",
                               encoding="utf-8")
    # 密钥状态读的是进程环境（config 里 os.getenv），写临时 .env 并不会改变它——
    # 以前这条断言其实是靠"开发机真 .env 里配了这两把 key"才过的，换 key 的当天就会假失败。
    # 显式设再还原，让它在任何机器上都是真的在测"配了就该报 True"。
    old_keys = {k: os.environ.get(k) for k in ("INTERNLM_API_KEY", "QWEN_API_KEY")}
    os.environ["QWEN_API_KEY"] = "sk-y-test-only"
    os.environ["INTERNLM_API_KEY"] = "sk-x-test-only"
    try:
        with _stubbed():
            _login_admin(client)
            j = client.get("/models").json()
            assert j["master"]["model"] == "intern-s2-preview", "/models 没报主单链在用什么模型"
            assert j["master"]["key_configured"] is True, "主单链密钥状态没报出来"
            r = client.post("/model", json={"model": "qwen38-flash-bailian", "chain": "master"})
            assert r.status_code == 200 and r.json()["ok"], r.text
            assert config.MASTER_VLM_MODEL == "qwen38-flash-bailian", "主单链选择没生效"
            assert config.VLM_MODEL == old_hawb[1], "切主单模型把分单模型一起换了"
            text = config.ENV_FILE.read_text(encoding="utf-8")
            assert "MASTER_VLM_MODEL=qwen38-flash-bailian" in text and "VLM_MODEL=intern-s2-official" in text, text
            assert client.get("/health").json()["master_model"] == "qwen3.8-flash", "健康检查没跟上主单链新模型"
            assert client.post("/model", json={"model": "intern-s2-official", "chain": "nope"}).status_code == 400, \
                "乱填 chain 居然放行了"
    finally:
        for k, v in old_keys.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.ENV_FILE, config.MASTER_VLM_MODEL = old_env_file, old_state
        config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION = old_hawb
        shutil.rmtree(tmp, ignore_errors=True)


def test_desk_thinking_selector_is_wired():
    """思考档下拉三档齐全（跟随预设/关/开），值就是 .env 里那个模式串；
    档位显示必须来自 /models 的回报，不能让浏览器自己记一份——重启后 .env 说了算，
    界面停在旧档就成了说谎的下拉。仅管理员可见（与模型下拉同一道门）。"""
    html = web_src.desk()
    assert 'id="thinkSel"' in html, "思考档下拉没了"
    assert 'id="fThink"' in html and '$("#fThink").hidden = !admin' in html, "思考档下拉没跟着管理员门走"
    load = re.search(r"async function loadModels\(\)\{(.*?)\n\}", html, re.S)
    assert load and '$("#thinkSel")' in load.group(1), "loadModels 没填思考档下拉（永远停在空选项）"
    assert re.search(r'\[\s*"",\s*"0",\s*"1"\s*\]\s*\.map', html), \
        "三档缺一个：只能开、不能回到「跟随预设」的开关没人敢碰"
    assert "跟随预设" in html, "档位要说人话：0/1 这种值让人猜不到「空」是什么意思"
    switch = re.search(r"async function setThinking\(sel\)\{(.*?)\n\}", html, re.S)
    assert switch and '"/model/thinking"' in switch.group(1), "改了档却不 POST：看着切了，其实没切"
    opts = re.search(r"function thinkOpts\(th\)\{(.*?)\n\}", html, re.S)
    assert opts and re.search(r"sel\.disabled\s*=\s*!th\.supported", opts.group(1)), \
        "渠道不支持思考时下拉要置灰：能拨却什么都不改的开关，只会被人当成坏了"
    assert opts and "th.supported" in opts.group(1) and "旧进程" in opts.group(1), \
        "服务端没报档位（旧进程）时也要说清楚，不能留一个空下拉让人以为没这个功能"


def test_desk_shows_both_chain_models():
    """顶栏要同时看得见两条链的模型（所有人可见状态，仅管理员能切）——
    只报一个的话，主单用了别的模型这件事在界面上完全隐形。"""
    html = web_src.desk()
    assert 'id="modelSelM"' in html, "主单链的模型下拉没了"
    assert 'switchModel(e.target, "master")' in html and 'switchModel(e.target, "hawb")' in html, \
        "两个下拉要各自带 chain 参数，否则切主单会把分单模型一起换掉"
    assert 'id="fModelM"' in html and '$("#fModelM").hidden = !admin' in html, "主单下拉也要仅管理员"
    assert "主单模型" in html and "分单模型" in html, "顶栏没把两条链分开标"


def test_switch_model_rejects_injection_and_empty():
    """选择会拼进 .env：换行/等号/空格能伪造出别的配置行，值必须在门口就挡住。"""
    client = TestClient(server.app)
    with _stubbed():
        _login_admin(client)
        for bad in ("", "   ", "deepseek\nSOME_INJECTED_LINE=evil", "a=b", "模型 带空格"):
            r = client.post("/model", json={"model": bad})
            assert r.status_code == 400, f"{bad!r} -> {r.status_code}"
        assert client.post("/model", json={"nope": 1}).status_code == 400


def test_thinking_switch_is_three_way_admin_only_and_persists():
    """思考档三档（跟随预设/关/开）：换模型是管理员的事，开关思考也是——它同样改变提取结果
    （开思考要多烧几千 reasoning token、也更慢）。只改内存不写 .env 的话重启就回默认，等于没开关。"""
    client = TestClient(server.app)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_env_"))
    old_env_file = config.ENV_FILE
    old_state = (config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION, config.MODEL_THINKING)
    config.ENV_FILE = tmp / ".env"
    config.ENV_FILE.write_text("QWEN_API_KEY=sk-x\nVLM_MODEL=qwen38-flash-bailian\n", encoding="utf-8")
    try:
        with _stubbed():
            config.set_model("qwen38-flash-bailian")
            assert client.post("/model/thinking", json={"thinking": "1"}).status_code == 401, \
                "开思考没要登录"
            _login_admin(client)
            r = client.post("/model/thinking", json={"thinking": "1"})
            assert r.status_code == 200 and r.json()["ok"], r.text
            assert config.thinking()["on"] is True, "本进程没立即生效（还要重启才算切）"
            assert config.model_extra_body() == {"enable_thinking": True}, "开关没落到真正发出去的参数上"
            assert "MODEL_THINKING=1" in config.ENV_FILE.read_text(encoding="utf-8"), "档位没写回 .env，重启就丢"
            assert client.get("/health").json()["thinking"]["on"] is True, "健康检查没报出思考档"
            assert client.post("/model/thinking", json={"thinking": "0"}).json()["thinking"]["on"] is False
            assert client.post("/model/thinking", json={"thinking": ""}).json()["thinking"] == \
                {"supported": True, "param": "enable_thinking", "on": False, "mode": ""}, \
                "跟随预设 = 回到各家用自己的默认值（qwen 那档是关）"
            for bad in ("2", "yes", "开"):
                assert client.post("/model/thinking", json={"thinking": bad}).status_code == 400, \
                    f"{bad!r} 这种值居然放行了"
    finally:
        config.ENV_FILE = old_env_file
        (config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION,
         config.MODEL_THINKING) = old_state
        shutil.rmtree(tmp, ignore_errors=True)


def test_models_list_shows_which_channels_can_toggle_thinking():
    """下拉要在免登录阶段就填得出"这家能不能开关思考"：官方书生压根不接受 extra_body，
    界面不点名会让人以为开关坏了（其实参数发过去直接 400）。"""
    client = TestClient(server.app)
    with _stubbed():
        j = client.get("/models").json()
        by = {p["key"]: p for p in j["presets"]}
        assert by["qwen38-flash-bailian"]["thinking"]["supported"] is True, "能关能开的渠道没标出来"
        assert by["qwen38-flash-bailian"]["thinking"]["param"] == "enable_thinking"
        assert by["intern-s2-official"]["thinking"]["supported"] is False, "不支持的要显式说不支持"
        assert "thinking" in j and "master" in j and "thinking" in j["master"], \
            "当前档与主单链档都要报：顶栏显示的模型名不许和实际发的参数不一致"


def test_thinking_on_an_unsupported_channel_sends_nothing_extra():
    """在不支持思考的渠道上把开关拨到"开"：请求里一个字都不许多（官方书生多收一个参数就回 400），
    而且接口要当场说清"这家不支持"——全局一个开关的代价就在这儿，不能靠界面自觉。
    .env 指到临时目录：这条会真的切模型，不能让回归把开发机的渠道改掉。"""
    client = TestClient(server.app)
    tmp = Path(tempfile.mkdtemp(prefix="hawb_env_"))
    old_env_file = config.ENV_FILE
    old = (config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION, config.MODEL_THINKING)
    config.ENV_FILE = tmp / ".env"
    config.ENV_FILE.write_text("INTERNLM_API_KEY=sk-x\nQWEN_API_KEY=sk-y\n"
                               "MODEL_THINKING=\n", encoding="utf-8")
    try:
        with _stubbed():
            _login_admin(client)
            assert client.post("/model", json={"model": "intern-s2-official"}).status_code == 200
            j = client.post("/model/thinking", json={"thinking": "1"}).json()
            assert j["ok"] and j["thinking"]["supported"] is False, "不支持的渠道要如实说不支持"
            assert config.model_extra_body() == {}, "不支持的渠道居然被塞了参数：真请求会 400"
            m = client.get("/models").json()
            assert m["thinking"]["supported"] is False
            assert m["thinking"]["mode"] == "1", "档位本身要如实报回来：界面靠它区分「没拨」和「拨了但这家不支持」"
            assert m["master"]["thinking"]["supported"] is False, "主单链此刻也是 S2，开关同样不该生效"
            assert "思考" not in {p["key"]: p for p in m["presets"]}["intern-s2-official"]["label"], \
                "预设名不跟着档位改写：思考档只由那一个下拉说，两处都说就会互相打脸"
    finally:
        config.ENV_FILE = old_env_file
        (config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION,
         config.MODEL_THINKING) = old
        shutil.rmtree(tmp, ignore_errors=True)



def test_desk_model_selector_is_wired_to_models_and_model_endpoints():
    """模型切换要在制单员手边（限管理员）：顶栏下拉从 /models 取数、切换 POST /model；
    纯文本模型还得在顶栏当场说明扫描件会失败，不然他只会看到一堆失败任务。
    会话 Cookie 由浏览器同源自动带上，前端不再发 X-API-Key。"""
    html = web_src.desk()
    assert 'id="modelSel"' in html, "模型下拉没了"
    load = re.search(r"async function loadModels\(\)\{(.*?)\n\}", html, re.S)
    assert load and re.search(r'fetch\((?:BASE \+ )?"/models"\)', load.group(1)), "下拉没有从 /models 取数"
    assert "probe(); loadModels();" in html, "启动时没填下拉"
    handler = re.search(r"async function switchModel\(sel, chain\)\{(.*?)\n\}", html, re.S)
    assert handler, "下拉的切换处理没了：选中新模型也不会切换"
    assert '"/model"' in handler.group(1), "切换请求没打到 /model"
    assert "chain: chain" in handler.group(1), "切换请求没带 chain：两条链会互相换掉对方的模型"
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
        assert r.status_code == 404 and "转录" in r.json()["detail"], r.text
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
    html = web_src.desk()
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
    assert fit and "L.locateZoom(" in fit.group(1) and "L.autoZoomPct(" in fit.group(1), \
        "放大倍数不再由 locFit 自己算：要走偏好（L.locateZoom）与那一档的算式（L.autoZoomPct）"
    assert fit and re.search(r"page\.style\.width = \(z \|\| 100\)", fit.group(1)), \
        "「不放大」那一档要回到适应宽度：留着上一次放大后的倍数，开关就只挡得住第一次"
    assert fit and "pane.clientHeight" in fit.group(1) and "im.naturalWidth" in fit.group(1), \
        "算倍数要的两个量（窗格高、页图真实宽）没传给 logic.js 那套算式"
    assert re.search(r'normFace = |function normFace', html), "匹配归一（NFKC+空白压合）没了"
    assert ".normalize(\"NFKC\")" in html, "全角归一走 NFKC，和 fidelity 的 _FW 同口径"
    assert 'id="pvLocExit"' in html and '$("#pvLocExit").addEventListener("click", exitLocate)' in html, \
        "没有「返回原视图」，制单员就困在定位模式里了"
    rp = re.search(r"function renderPreview\(\)\{(.*?)\n\}", html, re.S)
    assert rp and "exitLocate()" in rp.group(1), "切票不退出定位模式，正常预览会被页图卡住"


def test_locate_zoom_has_a_switch_and_a_custom_ratio_per_browser():
    """有人点字段后要放大才看得清，有人嫌每次都被拽走 —— 2026-10-09 用户要的是给选择权，
    而且比例能自己填。这份偏好是"手感"，所以存浏览器不存账号：换人不用替别人决定。

    盯四件事：三档都在定位那条工具条里（跟着它一起显隐）、偏好真的读写 localStorage、
    上限走页图真实像素那条判据、改一下当前这张票立刻重画（不用重新点字段）。"""
    page = web_src.part("index.html")
    js = web_src.part("js/desk.js")
    bar = page[page.index('id="pvLoc"'):page.index("返回原视图")]
    assert 'id="pvLocZoom"' in bar and 'id="pvLocPct"' in bar, \
        "档位与比例那两项不在定位工具条里（放别处就跟着票面一起看不见）"
    for v in ('value="auto"', 'value="fixed"', 'value="off"'):
        assert v in bar, f"定位放大少一档：{v}"
    assert 'type="number"' in bar and 'aria-label="定位放大比例"' in bar, "比例得是能填的数，还要有可读名字"
    assert "LS.locateZoom" in js and 'locateZoom:"hawb.review.locate.zoom"' in js, "偏好键没定义"
    assert re.search(r"localStorage\.setItem\(LS\.locateZoom", js), \
        "偏好只存在内存里：刷新一次、换张票就回到默认，等于没有开关"
    assert re.search(r"localStorage\.getItem\(LS\.locateZoom", js), "存了却没人读，控件跟偏好对不上"
    assert "L.maxZoomPct(" in js[js.index("function locFit()"):js.index("function pvSetTools()")], \
        "上限没走页图真实像素那条判据，填多大就糊多大"
    assert re.search(r'\$\("#pvLocZoom"\)\.addEventListener\("change"', js), \
        "换档没接监听：改了要当前这张票立刻重画，不能等下次点字段"
    assert re.search(r'\$\("#pvLocPct"\)\.addEventListener\("(input|change)"', js), \
        "填了比例没接事件，等于只能看不能改"
    assert '$("#pvLocPct").max = cap' in js, \
        "上限没写进输入框：人要填完才知道被收，不如填之前就看得见能到多少"


def test_desk_actions_on_top_columns_scroll_apart_and_boxes_fit_content():
    """2026-09-21 用户三条 UI 意见：①提交/导出三键挪到字段表顶部（原来在 39 行下面，
    每次提交都要滚到最底）；②字段列自己滚，别带着左边上传/排队列表一起动；
    ③收发货人长文本框按内容自适应高度（原来按 "\n" 数行数，一整段没有换行就只有一行高）。"""
    html = web_src.desk()
    top = html.find('class="ft top"')
    assert top > -1, "顶部操作条没了：提交/导出又滚回表尾去了"
    seg = html[top:html.find("wireMain(t)", top)]
    for bid in ('id="submit"', 'id="expOne"', 'id="expAll"'):
        assert bid in seg, f"顶部操作条少了 {bid}"
    assert seg.find("id=") < seg.find("<table"), "提交键没在字段表上面"
    css = re.sub(r"/\*.*?\*/", "", web_src.css(), flags=re.S)
    wide = re.search(r"@media \(min-width:1461px\)\{(.*?)\n\}", css, re.S)
    assert wide, "三栏独立滚动的媒体查询没了：滚字段又会带着整页一起动"
    assert re.search(r"#main\{[^}]*position:sticky[^}]*overflow:auto", wide.group(1)), \
        "#main 没做成自己的滚动容器"
    assert "#main th{top:0}" in wide.group(1), \
        "#main 变滚动容器后表头吸顶参照要归零，否则 th 会缩到卡片中间"
    assert re.search(r"td\.v textarea\{[^}]*max-height", css), "长文本框没封顶：一条超长整串能把卡片撑爆"
    ctl = re.search(r"function control\(t, k\)\{(.*?)\n\}", html, re.S)
    assert ctl and 'rows="1"' in ctl.group(1), "值框要有斜角就得是 textarea，而它默认两行高：短字段得写死 rows=1"
    assert ctl and "rows=" not in re.sub(r'rows="1"', "", ctl.group(1)), \
        "control 又按换行数算 rows 了——整段不换行的长文本会缩成一行（只许写死那一档）"
    assert re.search(r"function autoGrow\(el\)\{", html), "autoGrow 没了"
    assert re.search(r'\$\("#main"\)\.querySelectorAll\("textarea"\)\.forEach\(autoGrow\)', html), \
        "渲染后没给文本框量高：打开时还是一行"
    wire = re.search(r"if \(inp\.tagName === \"TEXTAREA\"\)(.*?)\n", html)
    assert wire and "autoGrow(inp)" in wire.group(1), "边打字没跟着长高"


def test_field_boxes_keep_a_dragged_height_and_two_handles_resize_the_desk():
    """2026-10-09 用户三样都要：拖过的高度要算数、票面栏↔字段栏那道缝能拖、字段名列的宽能拖。

    第一条是关键：长文本格一直有原生把手，但 autoGrow 每下输入都按内容重设高度，
    拖完一打字就弹回去 —— "能拖"在用户那儿等于"不能自定义"。"""
    css = web_src.part("css/desk.css")
    js = web_src.part("js/desk.js")
    page = web_src.part("index.html")
    assert "resize:vertical" in css, "长文本格没有原生拖拽把手"
    assert re.search(r"function autoGrow\(el\)\{.*L\.fieldHeight\(", js, re.S), \
        "autoGrow 还在自己定高度：人拖完一打字就弹回去"
    assert "localStorage.setItem(LS.fieldH" in js and "localStorage.getItem(LS.fieldH" in js, \
        "拖出来的高度没存本机：换张票、刷新一次就没了"
    assert "pointerdown" in js and "pointerup" in js, \
        "原生 resize 不触发任何事件，只能靠按下/松开前后比对高度才知道人拖过了"
    assert "data-fhreset" in js, "要有一个明确的「恢复自动高度」出口（双击会和选词打架）"
    # 两道把手：一个在网格里（分栏），一个在表头里（列宽，那张表是 desk.js 现画的）
    assert 'id="gripView"' in page, "分栏那道把手没进页面"
    assert 'id="gripCol"' in js, "表头那道把手没进字段表"
    assert 'role="separator"' in page and 'role="separator"' in js, "拖把手得让键盘与读屏器也知道它是分隔条"
    assert "minmax(420px, var(--vw))" in css, "网格还在写死 1.18fr：拖了不生效"
    assert "* 1fr" not in css, "Chrome 不接受 calc(var() * 1fr)：整条 grid-template-columns 会掉回自动布局"
    assert "var(--colK" in css, "字段名列还是死宽度，表头那道把手没接到东西上"
    assert re.search(r"th\.kcol\{width:var\(--colK\)", css), \
        "列宽只写在 td 上：table-layout:fixed 只认第一行（表头）给的宽度，拖了不会变"
    # 同一格还要留着全局 th 的 sticky：给它写 position:relative 会把 sticky 顶掉，
    # 于是「字段」那一格跟着内容滚走、右边两格还粘着（2026-10-09 浏览器截图里露出来的）
    kcol = re.search(r"th\.kcol\{([^}]*)\}", css)
    assert kcol and "position" not in kcol.group(1), \
        "th.kcol 里别再写 position：它会盖掉全局 th 的 sticky，表头第一格滚走只剩两格"


def test_the_stage_button_reports_both_the_local_copy_and_the_company_one():
    """「暂存到公司」一次做两件事（本机留痕 + j9 占位），回执就得把两件事都说清，
    否则公司那一发失败时人以为整件白点了，又去点一次（2026-10-09 用户定案走这一档）。"""
    js = web_src.part("js/desk.js")
    fn = re.search(r"function stageMsg\(n, c\)\{(.*?)\n\}", js, re.S)
    assert fn, "stageMsg 没了：暂存的回执要分三档说"
    body = fn.group(1)
    assert "mock" in body, "本机 mock 模式要说清没发公司，不然人以为已经寄出去了"
    assert "状态 0" in body, "成功那一档要写明是可反复改的状态 0，不是已发送"
    assert "skipped" in body and "error" in body, "缺号与公司失败两档都得有话说"
    assert js.count(">暂存到公司</button>") == 2, "分单与主单两颗按钮要同名，只改一颗另一张表还在说旧口径"
    assert js.count("才会真的发送") >= 2, "悬停要写清只有提交才发送——这决定人会否误以为已经发出去了"


def test_the_value_box_itself_has_a_width_handle():
    """值这一格的框要能直接拖宽（2026-10-09 用户否掉了"你去拖那两道把手"的说法）。

    原生 resize 帮不上：它对 <input> 根本不生效，而值列里一半是 input——
    "长文本能拖宽、短文本不能"比不能还糟。所以把手是自己画的，贴在格子右边界。"""
    css = web_src.part("css/desk.css")
    js = web_src.part("js/desk.js")
    assert re.search(r"td\.v\{position:relative\}", css), \
        "把手要贴值格右边界：td.v 不是定位上下文，absolute 的把手会飘到整页去找最近的定位祖先"
    assert ".edg{" in css and "cursor:ew-resize" in css, "值格右边界没有横向拖把手"
    assert ".edg{display:none}" in css, "两栏堆叠时没有另一侧可以挤，这把手留着就是骗人的假抓手"
    assert "function edgeGripHtml()" in js, "把手没进模板"
    assert js.count("${edgeGripHtml()}") >= 2, "分单表与主单表是两处模板，只接一处另一张表的框还是拖不动"
    assert re.search(r'closest\("\.edg"\)', js), "把手没接 pointerdown/keydown"
    assert js.count("L.boxWidthPct(") >= 2, "宽度算式只接在拖拽上：键盘那一路（←/→）没接，读屏器用户就只用得鼠标"
    # 与分栏那道把手写的是同一份偏好：各存各的会互相盖，拖完一边另一边不知道
    assert js.count("saveDeskShare(") >= 3, "值格把手没和分栏把手共用同一份份额偏好"
    assert '"--vw"' in js, "份额没写回 CSS 变量，拖了不改变布局"
    assert 'role="separator"' in js, "把手得让键盘与读屏器知道它是分隔条"


def test_every_value_box_has_a_corner_driving_both_width_and_height():
    """2026-10-09 用户："可以自由拖拽改变大小的"——一个斜角同时管宽和高，别让人去找页面上别的把手。

    前提是所有值格都得有斜角：原生 resize 只长在 textarea 上，短字段从前是 <input>，
    所以短字段一并换成单行 textarea。两件事跟着必须做对：
    数字列换框后要留住 inputmode（不然手机上弹出全键盘），
    单行框要挡住回车（那值原样写进公司系统，换行进去就是脏数据）。"""
    css = web_src.part("css/desk.css")
    js = web_src.part("js/desk.js")
    ctl = re.search(r"function control\(t, k\)\{(.*?)\n\}", js, re.S)
    assert ctl, "control 找不到了"
    assert "<input" not in ctl.group(1), "还有值格是 input：它没有右下角斜角，「自由拖拽」就只覆盖长文本那一半"
    assert "<input data-mk" not in js, "主单表的值格还是 input：那张表要能拖的斜角同样没有"
    assert 'inputmode="decimal"' in ctl.group(1), "换成交换框后数字列的输入提示丢了"
    assert 'class="one"' in ctl.group(1), "短字段得标成单行框，否则挡回车与样式都没抓手"
    assert re.search(r"td\.v textarea\{[^}]*resize:both", css), \
        "斜角还是只管高度：宽拖不动，用户说的「自由拖拽」没成立"
    assert re.search(r"td\.v textarea\{[^}]*max-height:900px", css), \
        "CSS 那道封顶写回 320 会连人拖的一起卡住：原生 resize 尊重 max-height，拖不动就是不成立"
    # 拖完那一下：宽交回整列（票面栏让路），inline 宽必须擦掉，否则这一格从此不跟列宽走
    up = re.search(r"document\.addEventListener\(\"pointerup\", \(\) => \{(.*?)\n\}\);", js, re.S)
    assert up and 'el.style.width = ""' in up.group(1), \
        "松开那一步没擦 inline 宽：那一格从此钉在自己的宽度上，再调列宽调不到它"
    assert up and "saveDeskShare(L.boxWidthPct(" in up.group(1), "斜角拖出来的宽没换算成整列份额"
    assert "function blockEnter(" in js and js.count('addEventListener("keydown", blockEnter') >= 2, \
        "单行框没挡回车：换行会一路写进公司系统"
    # 状态列的宽从前只写在 td.s 上：fixed 表格不认（同一批坑的第 25 条），它和值列平分剩余空间，
    # 值框白白窄一半、拖 120px 只长 60px。宽要写在表头那一格上。
    assert "th.scol{width:120px}" in css, "状态列的宽没写进表头：它白占一半剩余空间，值框窄一半"
    assert js.count('class="num scol"') >= 2, "两张字段表的表头要都带上 scol，只改一张另一张还是窄的"


def test_the_word_for_stage_means_only_the_server_tier():
    """「暂存」现在只有一个意思：存到服务器上给同事看（需求二那一档）。

    本机 localStorage 那块从前叫「本地暂存」，两个含义共用一个词——人会以为点它就已经交上去了，
    而没上服务器的那张票同事根本查不到（这正是需求一要消灭的现象）。所以那块改口叫「本机未保存的编辑」。
    """
    for rel in ("index.html", "js/desk.js"):
        t = web_src.part(rel)
        assert "本地暂存" not in t, f"{rel} 还在用「本地暂存」称呼本机草稿：和服务器那一档混名"
    # 光把「本地暂存」改掉不够：无障碍名与确认框文案随后又各自冒出了别的叫法，
    # 同一个东西三个名字，人点对齐不了屏上那行字，也就想不起它没上服务器。
    html = web_src.part("index.html")
    assert 'aria-label="本机未保存的编辑"' in html, "草稿面板的可读名要跟着屏上那几个字走"
    assert "本机暂存的编辑" not in html, "无障碍名里别再把本机那份叫暂存"
    js = web_src.part("js/desk.js")
    assert "条暂存记录" not in js, "清空本机编辑的确认框不许把它说成暂存记录：那一档只在服务器上"
    assert "本机草稿" not in js, "「本机草稿」是第三个名字，只保留「本机未保存的编辑」这一个"
