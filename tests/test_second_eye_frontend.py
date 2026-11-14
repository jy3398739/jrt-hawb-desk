# -*- coding: utf-8 -*-
"""互核弹层的接线契约（静态扫描）：浮层在页面里、按钮在操作条上、渲染函数存在、
滚体复用 .statsbody（粘性表头守卫自动覆盖）、死旗走 L.secondEyeVisible。
互核只提示不改值：断言渲染函数里没有给 air/face 赋值的手。"""
import web_src


def test_gate_skeleton_is_in_the_page():
    html = web_src.part("index.html")
    assert 'class="gate" id="seWin" hidden' in html, "互核浮层要按 .gate 骨架进页面（Esc/焦点自动管）"
    assert 'id="seBox"' in html and "statsbody" in html.split('id="seBox"')[0][-60:], \
        "滚体复用 .statsbody：粘性表头守卫与滚动样式自动覆盖"
    assert 'id="seClose"' in html


def test_desk_wires_button_gate_and_dead_flags():
    js = web_src.part("js/desk.js")
    assert 'id="seBtn"' in js, "操作条要有互核清单入口（qc.second_eye 存在才渲染）"
    assert 'openGate("seWin")' in js and "renderSecondEye(t)" in js
    assert "L.secondEyeVisible(f, t.locked)" in js, "effectiveFlags 要走 logic 的死旗判据"
    assert 'seWin: "#seBtn"' in js, "焦点归还表要把互核窗口记上"


def test_render_is_readonly_and_explains_both_may_be_wrong():
    js = web_src.part("js/desk.js")
    body = web_src.fn(js, "function renderSecondEye(t){")
    assert "两边都可能错" in body, "件数类冲突必须带上'两边都可能错'的说明"
    assert ".air =" not in body and ".face =" not in body and "edited" not in body, \
        "互核弹层只读：不许碰票面值或人工编辑"
    assert "不拦暂存与提交" in body, "弹层要写明互核不拦任何闸门"


def test_gate_close_and_open_are_wired():
    js = web_src.part("js/desk.js")
    assert '$("#seClose").addEventListener' in js


def test_status_line_is_in_the_ops_bar_and_takes_both_states():
    js = web_src.part("js/desk.js")
    assert 'id="seStatus"' in js, "操作条附近要有状态行的小字"
    body = web_src.fn(js, "function updateSeStatus(t){")
    assert '"互核中…"' in body and '"已回：无冲突"' in body and "条冲突" in body, \
        "两态文案都要有（带引号=字面量；注释里有同款字样不算数）"
    assert "L.seStatus(" in js, "状态走 logic 纯函数，不许 desk 里现编"


def test_parse_result_carries_se_pending_and_the_desk_uses_it():
    js = web_src.part("js/desk.js")
    assert "sePending: !!j.se_pending" in js, "解析结果落票时要把互核在跑标记带上"
    assert "/ticket/" in js and "pollSecondEye" in js, "互核中时要借轮询机制取回结论"
    assert "renderSecondEye(t)" in js, "结论落地的刷新照旧走弹层渲染"


def test_settings_panel_has_the_runtime_switch():
    html = web_src.part("index.html")
    assert 'id="seSel"' in html and html.index('id="seSel"') > html.index('id="setWin"'), \
        "运行时开关进管理员设置面板"
    js = web_src.part("js/desk.js")
    assert 'fetch(BASE + "/second_eye"' in js, "拨开关要打到管理路由"
    assert "r.second_eye" in js, "下拉值必须取自服务端回报（.env 是真值）"
    assert "seOn() && ((t.qc && t.qc.second_eye)" in js and            "seOn() && t.qc && t.qc.second_eye" in js,         "关=核对页零互核字样：状态行与按钮的渲染条件都要过总开关（注释不算数）"
