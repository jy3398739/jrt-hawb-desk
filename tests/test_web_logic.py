# -*- coding: utf-8 -*-
"""前端纯逻辑单测（设计文档 §3.5）：不碰 DOM 的算式剥到 web/js/logic.js，
用 node 直接 require 这份文件断言输入输出——不引 jsdom、不引测试框架，两边机器都有 node
（开发机 v24 / 服务器 v22）。logic.js 是普通 script（页面里先于 desk.js 加载），
尾部一行 module.exports 只为让 node 能读，浏览器不看它。
碰 DOM 的部分仍靠静态扫描 + 服务器真实浏览器走查。
"""
import web_src

# 求值前端纯逻辑的入口在 web_src 里（多处用例都要用，别各写一份 node 调用）
_eval = web_src.logic


def test_logic_file_is_loaded_before_desk_js():
    """纯逻辑文件得真的进页面、进静态扫描视野，否则测试绿的是磁盘上一份没人读的脚本。"""
    html = web_src.part("index.html")
    assert '<script src="web/js/logic.js"></script>' in html, "页面要加载 logic.js"
    assert html.index("js/logic.js") < html.index("js/desk.js"), "logic.js 必须在 desk.js 前面"
    assert "js/logic.js" in web_src.PARTS, "契约扫描的清单要把 logic.js 算进去"
    assert "L.draft" in web_src.part("js/desk.js"), "desk.js 要走 logic，别再自己抄一份算式"


def test_poll_backoff_grows_and_is_capped():
    """越等越稀疏：解析慢的时候按秒敲服务器没有意义；但也不能无限指数涨上去。"""
    assert _eval("L.pollDelay(1, 2000, 15000)") == 2000, "第一次就退避会把正常速度的结果也拖慢"
    steps = [_eval("L.pollDelay(n, 2000, 15000)", n=n) for n in range(1, 13)]
    assert all(b >= a for a, b in zip(steps, steps[1:])), "间隔必须单调不降：忽长忽短像是坏了"
    assert max(steps) <= 15000, f"退避要有上限：{max(steps)}"
    assert steps[5] > steps[1], f"到第 6 次还没退避就不是调度器：{steps[:6]}"



def test_draft_merge_keeps_human_edits_and_flags_only_moved_columns():
    """恢复草稿时人改的值照单恢复；只有'草稿当时的原值 ≠ 现在解析出的原值'的列才算打架。
    解析没动的列偏要报打架，制单员就得白重改一遍，几次之后这条提示没人再看。"""
    draft = {"base": {"A": "旧品名", "B": "1"}, "edit": {"A": "人工改过", "B": "2"},
             "acked": ["EORI 形态不合规"], "extraFlags": ["SHIPPER_INFO_TEL 缺区号"]}
    m = _eval("L.draftMerge(d, {A: '新品名', B: '1'})", d=draft)
    assert m["edit"] == {"A": "人工改过", "B": "2"}, "改过的列必须原样恢复，否则草稿等于没存"
    assert m["conflicts"] == ["A"], "只有 A 的底层解析动了，B 没动就不该报"
    assert m["acked"] == ["EORI 形态不合规"] and m["extraFlags"] == ["SHIPPER_INFO_TEL 缺区号"]
    assert m["editBase"] == {"A": "旧品名", "B": "1"}, "基准不能被新解析覆盖：覆盖了下次刷新就再也说不动过"


def test_draft_merge_without_draft_starts_clean_from_current_parse():
    m = _eval("L.draftMerge(null, {A: '新品名'})")
    assert m == {"edit": {}, "acked": [], "extraFlags": [], "conflicts": [], "editBase": {"A": "新品名"}}


def test_draft_merge_flags_a_column_the_parse_newly_filled():
    """漏取的列人补上了，重解析后模型这次取到了：人填的那格和解析出来的一对着呢，
    不报出来就等于拿人的猜测盖掉了模型的读数。"""
    m = _eval("L.draftMerge(d, {A: '模型这次取到了'})",
              d={"base": {}, "edit": {"A": "人工补的"}, "acked": [], "extraFlags": []})
    assert m["conflicts"] == ["A"]


def test_draft_touch_back_to_parsed_value_stops_being_an_edit():
    """改回与当前解析一致的值就不再是改动；同时这一列的打架消解——人已经对着新值看过了。"""
    base = {"A": "旧品名"}
    a = _eval("L.draftTouch({A:'人工改过'}, b, ['A'], 'A', '新品名', '新品名')", b=base)
    assert a["edit"] == {} and a["conflicts"] == [], "改回原值还记着改动，界面会一直挂着'已改 1 列'"
    assert a["editBase"] == {"A": "新品名"}, "基准要跟着挪到当前解析，否则下次刷新又报一次打架"


def test_draft_touch_with_a_real_change_records_value_and_baseline():
    a = _eval("L.draftTouch({B:'旧'}, b, ['A'], 'A', '人工改过', '新品名')", b={"B": "旧"})
    assert a["edit"] == {"B": "旧", "A": "人工改过"}
    assert a["editBase"] == {"B": "旧", "A": "新品名"}
    assert a["conflicts"] == [], "touch 过的那一列已按新解析重改，打架该消掉"
    """把解析出的值清空同样是改动（提交时要真的写个空值回公司），别和'资料本来就没这列'混清。"""
    c = _eval("L.draftTouch({}, b, [], 'A', '', '新品名')", b={"A": "旧品名"})
    assert c["edit"] == {"A": ""}
    """解析这次没有这一列（None），人也没填（''）→ 不是改动。"""
    d = _eval("L.draftTouch({}, b, [], 'A', '', null)", b={})
    assert d["edit"] == {}



def test_draft_entry_disappears_when_nothing_is_pending():
    """三样全空就返回 null：留一条空草稿会让'本机有未提交改动'一直亮着，人就不信这个状态了。"""
    empty = _eval("L.draftEntry({}, {}, [], [], '2026-09-30 10:00:00')")
    assert empty is None, "没有改动也没有确认，不该占一条草稿"
    ack = _eval("L.draftEntry({}, {}, ['红旗已核对'], [], '2026-09-30 10:00:00')")
    assert ack["acked"] == ["红旗已核对"] and ack["savedAt"] == "2026-09-30 10:00:00", "只确认红旗也是工作"
    assert _eval("L.draftEntry({A:'原'}, {A:'改过'}, [], [], 't')['base']") == {"A": "原"}


def test_draft_put_never_mutates_the_snapshot_it_read():
    """localStorage 读出来的对象是快照：就地改它会让'清草稿'在写回时又被自己读回来。"""
    got = _eval("(function(){const all={'999-1':{edit:{A:'x'}}}; "
                "const cleared=L.draftPut(all,'999-1',null); "
                "const back=L.draftPut(cleared,'999-2',L.draftEntry({},{B:'y'},[],[],'t')); "
                "return [all, cleared, back];})()")
    assert list(got[0]) == ["999-1"], "原快照不能被动过"
    assert got[1] == {}, "传 null 就是删条目"
    assert list(got[2]) == ["999-2"] and got[2]["999-2"]["edit"] == {"B": "y"}
