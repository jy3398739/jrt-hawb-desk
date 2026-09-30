# -*- coding: utf-8 -*-
"""核对工作台的导航（设计文档 §3.3）：48px 状态窄轨 + 置顶待办条 + 底部常驻操作条。

一天约 30 单的节奏下，换票动作不该是"回侧栏滚动找那一行"；操作条也不该跟着长表滚走
（核到最后一个字段时，提交按钮在屏幕外，人就干脆不点了）。
"""
import re

import web_src


def test_rail_is_a_real_navigation_strip():
    page = web_src.part("index.html")
    assert 'id="rail"' in page and 'aria-label="票据状态窄轨"' in page, "缺少窄轨或其无障碍名"
    for i in ("railDots", "railPrg", "railExpand"):
        assert 'id="%s"' % i in page, f"窄轨缺 {i}：状态点 / 今天进度 / 展开列表 三样都要有"
    css = web_src.part("css/desk.css")
    assert re.search(r"grid-template-columns:\s*48px 264px", css), "宽屏网格没给窄轨留出 48px"
    assert re.search(r"grid-template-columns:\s*48px 248px", css), "窄屏断点也要留窄轨，不然两种屏长得不一样"
    assert "#rail{grid-area:rail}" in css.replace(" ", "") or "grid-area:rail" in css


def test_rail_selects_the_ticket_and_says_where_we_are():
    js = web_src.part("js/desk.js")
    assert "function renderRail(" in js, "窄轨没有渲染函数"
    r = js[js.index("function renderRail("):js.index("\nfunction ", js.index("function renderRail(") + 1)]
    assert 'data-i="' in r, "每个状态点要挂票据下标，点一下就是换票"
    assert "aria-current" in r, "当前这张票要让读屏器知道（窄轨上没有文字可看）"
    assert "aria-label" in r and "statusOf(" in r, "状态点不能只靠颜色：无障碍名里要说清是哪张票、什么状态"
    assert "renderRail()" in js[js.index("function render("):], "render() 要顺带刷新窄轨，否则状态点停在旧样子"
    # 今天进度：已提交 / 经手总数，口径与今日台账一致（不是"完成度"这种猜出来的词）
    assert "已提交" in r or "/${" in r, "窄轨要给出今天进度（已提交/经手），人要知道还剩几张"


def test_prev_next_and_a_sticky_action_bar():
    js, css = web_src.part("js/desk.js"), web_src.css()
    assert "function stepTicket(" in js, "没有上一票/下一票"
    s = js[js.index("function stepTicket("):js.index("\nfunction ", js.index("function stepTicket(") + 1)]
    assert "S.sel" in s and "render()" in s, "换票要改选中并重画"
    assert "mvOff()" in s, "换票要先退出主单视图，否则核对区还停在主单那张 36 列表上，看着像点了没反应"
    assert js.count('id="submit"') == 1, "提交按钮只能有一个（两处就有两个 id，提交逻辑会只绑上一个）"
    assert 'class="ft bottom"' in js and "position:sticky" in css, "操作条要常驻底部"
    assert 'class="todo"' in js, "待办条（红旗/空值/已改）要置顶常驻，核到一半也看得见还剩什么"
    assert 'setProperty("--todoH"' in js, "表头吸顶的让位高度要量出来：写死数字在文字换行那天会压住第一行字段"
