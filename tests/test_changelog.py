# -*- coding: utf-8 -*-
"""设置里的「更新说明」窗（2026-10-19 用户：在管理员的界面加一个每次更新的说明，按钮）。

三个定死的口径：① 入口收进「设置」（顶栏 v1.17 刚从 13 项收成 9 项，不再加回去）；
② 内容是一份纯静态 changelog.js——没有接口、没有 .py，两台机器（VM 与公司那台）读
同一份，改它不用重启；③ 升上新版后仅管理员自动弹一次，弹不弹是纯逻辑，接线只有
「仅管理员」这一道门。
"""
import re

import web_src


# ── 纯逻辑：找当前版本那条、要不要自动弹 ──────────────────────────────────


def test_chlog_finds_the_running_versions_row():
    assert web_src.logic("L.chlogRow([{v:'1.31.1',items:['x']}], '1.31.1').items") == ["x"], \
        "当前版本那条没找到：弹出来的窗会是空的"
    assert web_src.logic("L.chlogRow([{v:'1.31.0'}], '1.31.1')") is None
    assert web_src.logic("L.chlogRow(null, '1.31.1')") is None
    assert web_src.logic("L.chlogRow(bad, '1.31.1')", bad="坏数据") is None, \
        "清单读坏了当没有，不许抛错把启动链拖死"
    assert web_src.logic("L.chlogRow([{v:'1.31.1'}], '')") is None, \
        "版本号还没到（/health 没回）时不挑任何一条"


def test_auto_open_only_when_new_and_described():
    rows = "[{v:'1.31.2'},{v:'1.31.1'},{v:'1.31.0'}]"
    assert web_src.logic("L.chlogShouldOpen('1.31.2', '1.31.1', " + rows + ")") is True
    assert web_src.logic("L.chlogShouldOpen('1.31.1', '1.31.1', " + rows + ")") is False, \
        "上次看过的版本不再弹：自动弹一次说的是每个版本一次"
    assert web_src.logic("L.chlogShouldOpen('1.30.9', '1.31.1', " + rows + ")") is False, \
        "回滚到没写说明的旧版＝没话可说，不打扰"
    assert web_src.logic("L.chlogShouldOpen('', '1.31.1', " + rows + ")") is False, \
        "版本号没拿到就别弹"
    assert web_src.logic("L.chlogShouldOpen('1.31.2', '', " + rows + ")") is True, \
        "新浏览器没看过任何版本：当前版本那条该弹"
    assert web_src.logic("L.chlogShouldOpen('1.31.2', '1.31.1', null)") is False
    assert web_src.logic("L.chlogShouldOpen('1.31.2', '1.31.1', bad)", bad="坏数据") is False


# ── 接线：入口、浮层、管理员门、自动弹那一发 ──────────────────────────────


def test_the_notes_entry_lives_inside_settings():
    page = web_src.part("index.html")
    setwin = page[page.index('id="setWin"'):page.index('id="wrap"')]
    assert 'id="btnChlog"' in setwin, "入口要收进「设置」窗口：顶栏不再加项（v1.17 收纳的约定）"
    assert 'id="chlogWin"' in page and 'id="chlogList"' in page and 'id="chlogClose"' in page, \
        "说明窗要是一张真的浮层：有列表容器、有能点的那颗关闭"
    assert page.index('id="setWin"') < page.index('id="chlogWin"') < page.index('id="wrap"'), \
        "说明窗排在设置之后、工作台之前：.gate 浮层都住这一段"
    assert 0 < page.index('web/js/changelog.js') < page.index('web/js/desk.js'), \
        "changelog.js 要在 desk.js 之前进页：开机自动弹那一下读的就是它"


def test_the_button_and_the_auto_open_are_admin_only():
    js = web_src.part("js/desk.js")
    ar = web_src.fn(js, "function applyRole(")
    assert '$("#btnChlog").hidden = !admin;' in ar, "入口没挂管理员那道门：制单员也能看见设置里的这颗"
    ao = web_src.fn(js, "function chlogAutoOpen(")
    assert 'ME.role !== "admin"' in ao, "自动弹没认角色：升完版每次进页都往制单员脸上糊一张说明窗"
    assert "L.chlogShouldOpen(version, chlogSeenGet(), chlogRows())" in ao, \
        "弹不弹要过纯逻辑那份判据（整行钉住：版本、seen、清单三样都得在）"
    pr = web_src.fn(js, "async function probe(")
    assert 'chlogAutoOpen(r.version || "");' in pr, \
        "/health 回来才弹：版本号从服务器那份拿，不从顶栏文字里抠"
    op = web_src.fn(js, "function chlogOpen(")
    assert "chlogMarkSeen(cur);" in op, "打开就算看过：不然关掉之后每次进页都要再弹一回"
    assert "chlogRender(" in op, "开窗要现画一遍：changelog.js 换了不该靠刷新页面才看见"
    ls = re.search(r"const LS = \{.*?\};", js, re.S)
    assert ls and "chlogSeen:" in ls.group(0), "LS 表里没登记 chlogSeen 这个键"


def test_the_notes_window_joins_the_esc_ring():
    js = web_src.part("js/desk.js")
    assert 'chlogWin: "#btnChlog"' in js, "Esc 关掉之后焦点要回到「更新说明」那颗入口上"
    assert '$("#chlogClose").addEventListener("click"' in js, "关闭那颗要真的关"
    assert '$("#btnChlog").addEventListener("click"' in js, "设置里那颗入口要真的开"


def test_the_changelog_lists_the_recent_releases():
    src = web_src.part("js/changelog.js")
    assert "const CHANGELOG = [" in src, "desk.js 按这个名字读清单：改名等于整份说明静默失踪"
    vs = re.findall(r'v:"([0-9.]+)"', src)
    need = ["1.27.1", "1.28.0", "1.28.1", "1.29.1", "1.30.0", "1.31.0", "1.31.1", "1.31.2"]
    miss = [v for v in need if v not in vs]
    assert vs and not miss, f"缺这些版本的说明：{miss}（有清单但没内容也等于没写）"
    assert vs.index("1.31.2") < vs.index("1.31.1"), "最新那条要在最上面"
    assert src.count("items:[") == len(vs), f"每条都要有 items：{src.count('items:[')} 对 {len(vs)}"
    assert "items:[]" not in src, "空条目不算写说明"
    assert "fetch(" not in src and "document." not in src, \
        "这份文件只放数据：接口与界面的事归 desk.js，混进来它就不能在两台机器上各读各的了"
