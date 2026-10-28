# -*- coding: utf-8 -*-
"""刷新回位（2026-10-28 反馈："刷新以后回到的永远是登录后的界面"）。

反馈人要的从来不是数据——队列会种回来、离开时的票会重开、没提交的改动在「未保存的编辑」；
丢的是**位置**：今日归档的档位与日期、左栏页签、主单面板。两层分工与全项目同款：

① 数据面（位置算不算数、隔天作不作废、垃圾输入怎么归一）在 logic.js 由 node 断输入输出；
② desk.js 只做 localStorage 接线，契约扫描钉住三处挂点与两处恢复，谁把挂点删了谁红。

一条老定案要接着守：台账那档**不写本机偏好**（"记住全部的那次，人会把全局的数当自己的数
报"）。位置不是偏好——所以位置只认**今天**：当天刷新回到原地，隔天一律回角色默认。
"""
import json

import web_src


# ── 一、数据面：L.viewRestore（node 断输入输出）──────────────────────────────────

def test_same_day_full_position_restores_verbatim():
    raw = json.dumps({"day": "D1", "view": "day", "rail": "mawb",
                      "dayv": {"scope": "all", "date": "2026-10-20", "q": "074"}},
                     ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") == {
        "view": "day", "rail": "mawb", "dayv": {"scope": "all", "date": "2026-10-20", "q": "074"},
        "mst": "", "mstOpen": False}


def test_position_from_another_day_is_dead():
    raw = json.dumps({"day": "D0", "view": "day", "rail": "draft",
                      "dayv": {"scope": "all", "date": "2026-10-20", "q": "074"},
                      "mst": "016-71086260", "mstOpen": True},
                     ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") is None, \
        "隔天的位置是过期偏好不是当前位置：隔天必须回默认（台账那档的老定案）"


def test_open_master_is_part_of_the_position():
    """v1.36.1：只看不改的主单不落草稿（draftEntry 三样全空返回 null），所以主单位置必须
    单独进"位置"这份——2026-10-28 业主截图报的"这个页面刷新还消失"正是这条路。"""
    raw = json.dumps({"day": "D1", "view": "desk", "rail": "mawb",
                      "mst": "016-71086260", "mstOpen": True}, ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") == {
        "view": "desk", "rail": "mawb", "dayv": None,
        "mst": "016-71086260", "mstOpen": True}
    raw = json.dumps({"day": "D1", "view": "desk", "rail": "queue",
                      "mst": "016-71086260", "mstOpen": False}, ensure_ascii=True)
    got = web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1")
    assert got is not None, "只搜过没开核对页的主单也是位置：不能当默认空转"
    assert got["mst"] == "016-71086260" and got["mstOpen"] is False


def test_mst_is_validated_like_the_server_would():
    raw = json.dumps({"day": "D1", "view": "desk", "mst": "9" * 50}, ensure_ascii=True)
    got = web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1")
    assert got["mst"] == "9" * 40, "号超长要截在 40：跟 /day 的搜索词同一把尺"
    for bad in ("074/55430384", "074\\55430384"):
        raw = json.dumps({"day": "D1", "view": "desk", "mst": bad}, ensure_ascii=True)
        assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") is None, bad
    raw = json.dumps({"day": "D1", "view": "desk", "mst": 7}, ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") is None


def test_mst_open_needs_a_number_and_a_real_boolean():
    raw = json.dumps({"day": "D1", "view": "desk", "mstOpen": True}, ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") is None, \
        "没有号就谈不上开核对页：整份位置当默认空转"
    raw = json.dumps({"day": "D1", "view": "desk", "mst": "016-71086260",
                      "mstOpen": "yes"}, ensure_ascii=True)
    got = web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1")
    assert got["mstOpen"] is False, "只认真布尔：字符串 'yes' 别当开"


def test_garbage_inputs_all_land_on_null():
    for raw in ("not json", "null", "5", '"x"', "{}", "[]", "[1,2]"):
        assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") is None, raw


def test_all_defaults_position_is_a_no_op():
    raw = json.dumps({"day": "D1", "view": "desk", "rail": "queue"}, ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") is None


def test_unknown_rail_falls_back_to_queue():
    raw = json.dumps({"day": "D1", "view": "desk", "rail": "bogus"}, ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") is None
    raw = json.dumps({"day": "D1", "view": "desk", "rail": "draft"}, ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") == {
        "view": "desk", "rail": "draft", "dayv": None, "mst": "", "mstOpen": False}


def test_unknown_view_falls_back_to_desk():
    raw = json.dumps({"day": "D1", "view": "bogus", "rail": "draft"}, ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") == {
        "view": "desk", "rail": "draft", "dayv": None, "mst": "", "mstOpen": False}


def test_dayv_unknown_scope_is_dropped_not_passed_through():
    raw = json.dumps({"day": "D1", "view": "day", "dayv": {"scope": "bogus"}}, ensure_ascii=True)
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") == {
        "view": "day", "rail": "queue", "dayv": None, "mst": "", "mstOpen": False}, \
        "非法档位喂给 loadDay 会查出一批不存在的行：宁可不恢复也不恢复错的"


def test_dayv_bad_date_is_dropped_good_one_kept():
    raw = json.dumps({"day": "D1", "view": "day",
                      "dayv": {"scope": "mine", "date": "明天", "q": ""}}, ensure_ascii=True)
    got = web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1")
    assert got["dayv"]["date"] == "", "非法日期发去 /day 是 400：必须丢掉"
    raw = json.dumps({"day": "D1", "view": "day",
                      "dayv": {"scope": "mine", "date": "2026-10-20", "q": ""}}, ensure_ascii=True)
    got = web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1")
    assert got["dayv"]["date"] == "2026-10-20"


def test_dayv_q_is_capped_at_40_like_the_server():
    q50 = "9" * 50
    raw = json.dumps({"day": "D1", "view": "day",
                      "dayv": {"scope": "mine", "date": "", "q": q50}}, ensure_ascii=True)
    got = web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1")
    assert got["dayv"]["q"] == "9" * 40, "/day 对 q 长度的闸在 40：恢复的值也得是同一把尺"
    raw = json.dumps({"day": "D1", "view": "day",
                      "dayv": {"scope": "mine", "date": "", "q": 7}}, ensure_ascii=True)
    got = web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1")
    assert got["dayv"]["q"] == ""


def test_object_input_is_accepted_too():
    raw = {"day": "D1", "view": "day", "rail": "draft",
           "dayv": {"scope": "toreview", "date": "", "q": ""}}
    assert web_src.logic("L.viewRestore(raw, today)", raw=raw, today="D1") == {
        "view": "day", "rail": "draft", "dayv": {"scope": "toreview", "date": "", "q": ""},
        "mst": "", "mstOpen": False}


# ── 二、接线契约：desk.js 只做读写与摆位 ────────────────────────────────────────

def test_ls_has_the_view_key():
    assert 'view:"hawb.review.view"' in web_src.part("js/desk.js")


def test_save_view_is_wired_into_all_three_state_changers():
    js = web_src.part("js/desk.js")
    for head in ("function setView(", "function railTab(", "async function loadDay("):
        assert "saveView()" in web_src.fn(js, head), head
    save = web_src.fn(js, "function saveView(")
    assert "dayKey()" in save and "LS.view" in save, "位置要带当天日期存（隔天作废靠它）"


def test_restore_view_position_reads_logic_and_applies():
    js = web_src.part("js/desk.js")
    body = web_src.fn(js, "function restoreViewPosition(")
    assert "L.viewRestore" in body, "判据在 logic.js，desk.js 别另写一份"
    assert "railTab(" in body and "setView(" in body


def test_restore_master_fills_the_search_back():
    js = web_src.part("js/desk.js")
    body = web_src.fn(js, "function restoreMaster(")
    assert "mstDrafts()" in body and "mstSearch(" in body
    assert "savedAt" in body, "只回今天动过的那张：判据得落在草稿自己的时间上"


def test_start_desk_restores_before_render():
    body = web_src.fn(web_src.part("js/desk.js"), "function startDesk(")
    assert body.index("restoreViewPosition()") < body.index("render()"), \
        "先摆位置再画：render 之后才恢复，人看见的就是默认页"
    assert "restoreMaster()" in body


def test_day_view_refresh_does_not_yank_back_to_a_ticket():
    js = web_src.part("js/desk.js")
    assert 'if (VIEW.cur !== "day") await openHouse(pick);' in js, \
        "恢复到归档页的人别被拽回某张票（toast 也一样吵）"


# ── 三、v1.36.1：主单位置与草稿解耦（只看不改的主单也要回堂）────────────────────

def test_save_view_carries_the_master_position():
    save = web_src.fn(web_src.part("js/desk.js"), "function saveView(")
    assert "mst:" in save and "MST_LAST" in save, "主单号要从检索结果/MV 里现取，别另存一份状态"
    assert "mstOpen" in save


def test_every_master_state_change_persists_the_position():
    js = web_src.part("js/desk.js")
    for head in ("function openMvFromMst(", "async function mstSearch("):
        assert "saveView()" in web_src.fn(js, head), head
    mvoff = web_src.fn(js, "function mvOff(")
    assert "if (was) saveView();" in mvoff, \
        "mvOff 关掉核对页那一刻要落位置：只查 saveView() 这个词会被 if(false) 骗过（变异 N6 活的教训）"


def test_restore_master_prefers_the_position_over_drafts():
    body = web_src.fn(web_src.part("js/desk.js"), "function restoreMaster(")
    assert "(VIEW_RESTORE && VIEW_RESTORE.mst)" in body, \
        "主路径必须读位置里那份 mst（只看不改也有）；草稿只当升级前的兜底"
    assert "mstDrafts()" in body, "升级前留下的草稿要能兜住"
    assert "MV_AUTO_OPEN" in body, "核对页开着的话要摆回去（v1.36.0 只重跑了检索、没回核对页）"


def test_position_restore_keeps_the_validated_master_state():
    body = web_src.fn(web_src.part("js/desk.js"), "function restoreViewPosition(")
    assert "VIEW_RESTORE" in body


def test_search_result_reopens_the_review_once():
    body = web_src.fn(web_src.part("js/desk.js"), "async function mstSearch(")
    assert "MV_AUTO_OPEN" in body, "一次性标记：检索回来后按它把核对页摆回去，再清掉"
    assert "openMvFromMst(" in body, "摆回核对页必须走唯一入口（与那颗按钮同一条路）"
