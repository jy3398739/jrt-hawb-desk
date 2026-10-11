# -*- coding: utf-8 -*-
"""顶栏瘦身：管理员专属的六样收进一颗「设置」，浮层能用键盘出来。

三条底线（用例就是围着它们写的）：
  1. **收纳不是删除**：六个控件（分单模型/主单模型/思考/统计/数据清理/账号管理）一个都不许丢，
     三张窗口各自的门禁与状态一律不动——服务端 require_admin 才是门，藏按钮只是遮眼睛。
  2. **告警与徽标不许被藏进二级页就看不见**：口令未改那条改成设置按钮上的小圆点 +
     面板「账号」节里的完整一句话；数据清理的"几张到期"徽标跟着搬到那颗按钮上，有到期才亮。
  3. **顶栏不许再把主导航挤出视野**（v1.16.0 刚为这个翻过一次车）。
"""
import re

import web_src

# 管理员专属：这些从顶栏搬进设置浮层
MOVED = ("fModel", "fModelM", "fThink", "btnStats", "btnPurge", "btnAcct")


def _page():
    return web_src.part("index.html")


def _hdr(page=None):
    page = page or _page()
    i, j = page.index("<header class=\"hdr\""), page.index("</header>")
    return page[i:j]


def _seg(page, start):
    """从 start 那个标记切到它自己那段结束（浮层骨架是 <div class="gate" id="x"> … </div>）。"""
    i = page.index(start)
    j = page.index("\n</div>", i)
    return page[i:j]


def _fn(js, head):
    """切出一个函数的源码：单行写法就取那一行，多行取到第一个顶格 }。
    别拿「下一个 function」当边界——后面还有几十个函数，切过头之后断言里那些
    .focus() / setAttribute 会从隔壁函数里蒙对，变异就杀不回了（本轮实测）。"""
    i = js.index(head)
    eol = js.index("\n", i)
    return js[i:eol] if js[eol - 1] == "}" else js[i:js.index("\n}", i) + 2]


# ── 一、结构：顶栏瘦下来，控件一个不丢 ──────────────────────────────────────────


def test_header_no_longer_carries_the_admin_only_controls():
    hdr = _hdr()
    for i in MOVED:
        assert 'id="%s"' % i not in hdr, "%s 还在顶栏里：这一轮就是要把它收进设置" % i
    assert 'id="btnSet"' in hdr, "顶栏要有那颗「设置」入口"
    # 主导航必须还在顶栏（v1.16.0 就是把它挤到 2036px 处，用户找不到台账）
    assert 'id="viewDay"' in hdr and 'id="viewDesk"' in hdr, "视图切换不许跟着一起收进去"


def test_every_moved_control_lives_inside_the_settings_popover():
    """搬走 ≠ 还在页面上：断言它们确实落在 #setWin 那一段里，一个都不许丢在别处或删掉。"""
    page = _page()
    seg = _seg(page, '<div class="gate" id="setWin"')
    for i in MOVED:
        assert 'id="%s"' % i in seg, "%s 没落在设置浮层里" % i
    assert "提取模型" in seg and "数据与统计" in seg and "账号" in seg, "三节标题：入口要有归类，不是堆一堆按钮"


def test_the_settings_entry_is_a_real_button_that_announces_a_dialog():
    hdr = _hdr()
    line = [l for l in hdr.splitlines() if 'id="btnSet"' in l]
    assert line and "<button" in line[0], "设置入口必须是真 button（键盘 Tab 到得了）"
    assert 'aria-haspopup="dialog"' in line[0] and 'aria-expanded=' in line[0], \
        "点开的是浮层就得这么报：读屏器才知道按下去会出来一层"
    # 报了就得跟着改：只在 HTML 里写个初值、开合时不动，读屏器会一直以为它是展开的
    js = web_src.part("js/desk.js")
    for fn, val in (("openSet", "true"), ("closeSet", "false")):
        body = _fn(js, "function " + fn + "(")
        assert 'setAttribute("aria-expanded", "%s")' % val in body, \
            "%s 没把 aria-expanded 改成 %s：浮层关了按钮还报着展开" % (fn, val)


# ── 二、门禁：一条都不许多，也一条都不许少 ──────────────────────────────────────


def test_role_gating_still_covers_every_admin_only_thing():
    """收纳之后 applyRole 仍要逐个按角色收（多一道保险；真正的门在服务端 require_admin）。
    数代码里的条数而不是抄清单：加一个仅管理员的控件，这里就该跟着多一条。"""
    js = web_src.part("js/desk.js")
    body = js[js.index("function applyRole()"):]
    body = body[:body.index("\nfunction ", 1)]
    gated = re.findall(r'\$\("#(\w+)"\)\.hidden = !admin', body)
    for i in MOVED:
        assert i in gated, "%s 不再按角色收：非管理员能看见不该看见的入口" % i
    assert "btnSet" in gated, "「设置」本身当然也只对管理员可见"
    assert len(gated) >= 7, "按角色收的入口比预期少，八成是搬东西时漏改了一条"


# ── 三、徽标与告警：不能被藏进二级页 ────────────────────────────────────────────


def test_the_expiry_badge_moves_onto_the_settings_button():
    """v1.15.0 把"还有几张到期"做成开门就看得见的徽标，收进菜单后它得跟着搬到那颗按钮上，
    否则"管理员今天要不要清理"这件事只能靠记得点进设置才知道。"""
    js, page = web_src.part("js/desk.js"), _page()
    assert "btnPurgeCnt" not in js and 'id="btnPurgeCnt"' not in page, "旧徽标还留着：两处都会亮就是两处数字"
    line = [l for l in page.splitlines() if 'id="btnSetCnt"' in l]
    assert line, "设置按钮上要有徽标那格"
    assert '$("#btnSetCnt")' in js, "候选数没写进那颗徽标"


def test_the_default_password_warning_is_still_unmissable():
    """告警不能因为"收进设置"就变成没人看见：顶栏留一个小圆点，面板里那句完整的话照旧在。
    点那句告警要直接落到账号管理（它本来就是干这个的），所以先把浮层关掉再聚焦。"""
    js, page = web_src.part("js/desk.js"), _page()
    assert 'id="btnSetDot"' in page, "顶栏那颗小圆点"
    assert '$("#btnSetDot").hidden' in js, "圆点要跟着「口令未改」这件事开合"
    seg = _seg(page, '<div class="gate" id="setWin"')
    assert 'id="pwWarn"' in seg, "完整那句告警要在面板的账号节里"
    body = js[js.index('$("#pwWarn").addEventListener'):]
    body = body[:body.index("});") + 3]
    assert '$("#btnAcct").click()' in body, \
        "点告警要直接落到账号管理：只把话写在那儿、不给入口，等于没提醒"


def test_opening_a_window_from_settings_does_not_stack_two_masks():
    """浏览器里量到的：从设置里点「解析正确率」，两张浮层同时开着且是同一个层级——
    设置那张在 DOM 里更靠后，于是它盖在统计上面，人点了统计却还看着设置，比不点更像坏了。
    所以从设置里开任何一张窗口，都要先把设置收掉（同一时刻只留一层）。"""
    js = web_src.part("js/desk.js")
    body = _fn(js, "function openGate(")
    assert "closeSet()" in body and ".hidden = false" in body, "openGate 没收设置、或没开目标窗口"
    for entry in ("#btnStats", "#btnPurge", "#btnAcct"):
        h = js[js.index('"%s").addEventListener' % entry):]
        h = h[:h.index("});") + 3]
        assert "openGate(" in h, "%s 还在直接摆 .hidden：从设置里点它就是叠两层" % entry
    # Esc 关掉子窗口后，焦点别丢进不可见的控件里（入口此刻藏在已经收起来的设置面板里）
    esc = _fn(js, "function closeTopGate(")
    assert "offsetParent" in esc or "#btnSet" in esc, "焦点要落到一个真看得见的元素上"


# ── 四、键盘出不去：四张浮层都要 Esc 能关，焦点要还给入口 ─────────────────────────


def test_esc_closes_every_overlay_and_gives_focus_back():
    """浮层从前只有「关闭」那颗按钮：键盘按 Esc 没反应，人以为页面卡住。
    清单从 index.html 里捞（有几个 gate 就管几个），不抄一份手写的。
    唯一例外是登录遮罩 #gate：真正的门在服务端（未登录什么接口都调不动），但"Esc 就把登录层
    掀开"看着像门禁失效，这个错觉不给。"""
    page, js = _page(), web_src.part("js/desk.js")
    gates = [g for g in re.findall(r'<div class="gate" id="(\w+)"', page) if g != "gate"]
    assert gates, "页面里一个浮层都没找到，这条断言该改法子了"
    body = _fn(js, "function closeTopGate(")
    assert "Escape" in js and "closeTopGate()" in js, "没接 Esc"
    assert '.gate:not(#gate)' in body, \
        "登录遮罩 #gate 被算进来了：真正的门在服务端，但「Esc 一掀就露出页面」这个错觉不给"
    for g in gates:
        assert g in js, "Esc 那段没管到 #%s" % g
    assert ".focus()" in body, "关掉之后焦点要还给打开它的按钮，不然键盘用户从头 Tab 一遍"
