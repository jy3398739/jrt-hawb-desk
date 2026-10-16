# -*- coding: utf-8 -*-
"""分段核对流（右栏五段 + 票面联动 + 三态提交）的契约。

三条底线，用例就是围着它们写的：
  1. **红旗是硬门**：三态按钮只放行"空值未确认"，任何"带未确认红旗提交"的路径都不许存在
     （前端不发、服务端也拒），这条与既有提交口径一致，改版不许降级。
  2. **待办与进度不许写死**：分母由数据实时算（外部交接稿里那个 13 是它示例票的巧合）。
  3. **票面联动只画真坐标**：锚点/高亮都来自 L1 转录行；空栏在真实数据里没有出处，
     宁可不画，也不许回到"手工标注一版建议框"那种迟早漂移的东西。
"""
import contextlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import fieldspec
import server
import store
import web_src


def _js():
    """剥掉块注释再读 desk.js（保住行数）：钉「界面会说出这几个字」必须只看会进模板的那部分，
    否则断言会被自己写在注释里的话喂饱（v1.14.0 变异验证抓过两次）。"""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  web_src.part("js/desk.js"), flags=re.S)


def _css():
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  web_src.part("css/desk.css"), flags=re.S)


def _fn(js, name):
    i = js.find("function " + name + "(")
    assert i >= 0, "desk.js 里找不到函数：" + name
    j = js.find("\nfunction ", i + 1)
    k = js.find("\nasync function ", i + 1)
    if k >= 0 and (k < j or j < 0):
        j = k
    return js[i:j if j > 0 else len(js)]


# ── 一、纯函数：状态机与待办口径（node 跑 logic.js，输入输出可枚举）────────────────


def test_field_status_six_states_and_flag_wins_over_everything():
    """交接稿 §3.3 的六态，先命中先得。红旗优先于一切：填了值也不变绿，必须显式确认。"""
    g = lambda **kw: web_src.logic("L.fieldStatus(o)", o=kw)
    assert g(flags=1, acked=False, raw="", air="", edited=False)["s"] == "flag"
    assert g(flags=1, acked=True, raw="X", air="X", edited=False)["s"] == "confirmed"
    assert g(flags=0, acked=False, raw="", air="")["s"] == "empty"
    assert g(flags=0, acked=True, raw="", air="")["s"] == "emptyok"
    assert g(flags=0, acked=False, raw="CN", air="")["s"] == "miss"
    assert g(flags=0, acked=False, raw="TIANJIN", air="TIANJIN")["s"] == "same"
    # 红旗字段同时又是空的：仍是 flag（不许因为"看起来一样"就降级成 empty 少一次确认）
    assert g(flags=2, acked=False, raw="", air="")["s"] == "flag"
    assert g(flags=0, acked=False, raw="", air="")["todo"] is True
    assert g(flags=0, acked=True, raw="", air="")["todo"] is False
    assert g(flags=1, acked=True, raw="A", air="A")["todo"] is False, "已确认的红旗不该继续挂着当待办"
    assert g(flags=0, acked=False, raw="1", air="")["todo"] is False, "miss 是提示，不是待办"


def test_empty_means_both_l2_and_l3_blank_not_just_l3():
    """空值的判据是 L2 与 L3 都空。只看 L3 会把"模型读到了、归一化没落上"当成票面本来就没有——
    那是两种完全不同的活（前者是漏，后者是免修）。"""
    f = lambda raw, air: web_src.logic("L.isEmptyVal(raw, air)", raw=raw, air=air)
    assert f("", "") is True
    assert f("CN", "") is False          # 这是 miss，不是 empty
    assert f("", "CN") is False
    assert f("  ", "\t") is True


def test_todo_and_progress_are_computed_from_data_never_hardcoded():
    rows = [{"k": "TO1", "flags": 1, "raw": "", "air": ""},            # 红旗未确认
            {"k": "TO2", "raw": "", "air": ""},                          # 空值未确认
            {"k": "ORIGIN_NAME", "raw": "TIANJIN", "air": "TIANJIN"},   # 干净，不打扰
            {"k": "TO3", "raw": "", "air": "", "acked": True},         # 已确认「本就为空」
            {"k": "DEST_NAME", "raw": "TSN", "air": ""},                # miss 只是提示，不算待办
            {"k": "SHIPPER_INFO_TEL", "raw": "", "air": ""}]
    got = web_src.logic("L.todoOf(rows)", rows=rows)
    assert got["keys"] == ["TO1", "TO2", "SHIPPER_INFO_TEL"]
    assert got["n"] == 3
    # 进度：分母是"进这张票时的待办快照"，分子是已经清掉的项数；0/0 是 100%，不是 NaN
    assert web_src.logic("L.progress(0, 0)") == {"done": 0, "total": 0, "pct": 100}
    assert web_src.logic("L.progress(16, 4)") == {"done": 12, "total": 16, "pct": 75}
    assert web_src.logic("L.progress(16, 20)")["done"] == 0, "分母被改小也不能出现负进度"


def test_no_hardcoded_todo_total_in_the_panel():
    """外部交接稿验收里那句「进度 0/13」是示例票的数，抄进代码就是一个会说谎的常量。"""
    js = _js()
    for banned in ("/ 13", "13 项", "共 13"):
        assert banned not in js, "把示例票的 13 写进了面板：" + banned


def test_segment_state_reports_cleared_count_and_the_right_warning():
    fields = [{"k": "MAWB_NO", "g": "id"}, {"k": "HAWB_NO", "g": "id"},
              {"k": "ORIGIN_NAME", "g": "route"}, {"k": "TO1", "g": "route"},
              {"k": "TO2", "g": "route"}]
    seg = web_src.logic("L.segState(fields, hard, soft)", fields=fields,
                        hard={"TO1": 1}, soft={"TO1": 1, "TO2": 1})
    by = {s["g"]: s for s in seg}
    assert by["id"] == {"g": "id", "tot": 2, "todo": 0, "hard": 0, "soft": 0,
                        "cleared": 2, "state": "clear"}
    assert by["route"]["tot"] == 3 and by["route"]["hard"] == 1
    assert by["route"]["soft"] == 2
    assert by["route"]["todo"] == 2, "待办 = 红旗字段 ∪ 空值字段（TO1 两样都占，只算一次）"
    assert by["route"]["cleared"] == 1
    # 红旗段比空值段更重：两处都有事时不许被抹平成琥珀
    assert by["route"]["state"] == "hasflag"
    only_soft = web_src.logic("L.segState(fields, hard, soft)", fields=fields,
                              hard={}, soft={"TO2": 1})
    assert {s["g"]: s["state"] for s in only_soft}["route"] == "hasissue"


def test_next_todo_wraps_and_starts_from_the_beginning_past_the_end():
    ks = ["TO1", "SHIPPER_INFO_POSTAL", "SHIPPER_INFO_TEL"]
    assert web_src.logic("L.nextTodo(ks, 'TO1')", ks=ks) == "SHIPPER_INFO_POSTAL"
    assert web_src.logic("L.nextTodo(ks, 'SHIPPER_INFO_TEL')", ks=ks) == "TO1", "到末尾要回绕"
    assert web_src.logic("L.nextTodo(ks, '')", ks=ks) == "TO1"
    assert web_src.logic("L.nextTodo(ks, 'GONE')", ks=ks) == "TO1", "当前项已不在名单里就从头开始"
    assert web_src.logic("L.nextTodo(ks, 'TO1')", ks=[]) == ""


# ── 二、票面联动：只画真坐标 ─────────────────────────────────────────────────────


def test_anchor_only_for_flags_with_a_real_spot_on_the_page():
    """常驻锚点给的是"票面印了、模型没取"的那一格：出处先取 fidelity.sources，
    没有就退一步用红旗文案里那个 值='…' 去转录行里找；两条都找不到就不画。
    画一个猜出来的框比不画更坏——它会让人以为已经核对过位置。"""
    lines = [{"i": 1, "text": "232 TSN 19509696", "bbox": [10, 20, 90, 30], "page": 1},
             {"i": 2, "text": "HAWB No: 883000430", "bbox": [10, 40, 90, 50], "page": 1}]
    got = web_src.logic("L.anchorLines(src, lines, flag)", src={"MAWB_NO": [1]}, lines=lines,
                        flag="保真 MAWB_NO: 漏抄（值='232-19509696'）")
    assert [l["i"] for l in got["MAWB_NO"]] == [1]
    got2 = web_src.logic("L.anchorLines(src, lines, flag)", src={}, lines=lines,
                        flag="保真 CONSIGNEE_INFO_TEL: 漏抄（值='861069479536'）")
    assert got2 == {}, "转录里没有那个值就不要编一个框"
    got3 = web_src.logic("L.anchorLines(src, lines, flag)", src={"GOODS_INFO": [2]},
                         lines=lines, flag="别的东西")
    assert [l["i"] for l in got3["GOODS_INFO"]] == [2], "sources 里有就直接用，不必再查文本"
    # 值印在整行里（票面常把主单号与航段码并排）也要认得出来
    got4 = web_src.logic("L.anchorLines(src, lines, flag)", src={}, lines=lines,
                         flag="MAWB_NO 缺失（值='19509696'）")
    assert [l["i"] for l in got4["MAWB_NO"]] == [1]


def test_layers_are_three_and_each_is_emptied_before_it_is_filled():
    """三层浮层生命周期不同（常驻 / 悬停 / 定位），各自一层：混在一层里，
    移开鼠标就会把定位框一起擦掉，或者定位完留一屏没清的悬停框。"""
    js = _js()
    page = web_src.part("index.html")
    for i in ("pvAnchors", "pvHover", "pvBoxes"):
        assert 'id="%s"' % i in page, "票面少了一层浮层：" + i
        assert i in js, "desk.js 没用到那一层：" + i
    for fn in ("drawAnchors", "hoverField"):
        body = _fn(js, fn)
        assert 'innerHTML = ""' in body, fn + " 要先清空自己那一层"
    assert "title" in _fn(js, "drawAnchors"), "锚点得说清是哪一列、为什么在那儿（悬停提示）"


def test_hover_does_not_touch_the_locate_layer():
    js = _js()
    body = _fn(js, "hoverField")
    assert "pvBoxes" not in body, "悬停只写 hover 层：动定位层会把刚点出来的定位框闪掉"


# ── 三、面板接线与渲染铁律 ───────────────────────────────────────────────────────


def test_each_segment_is_its_own_table_so_the_head_can_stick():
    """段头必须是每段那张表自己的 <thead>。<tr> 里放 position:sticky 是粘不住的
    （包含块就是那一行，撑不出"钉在容器顶"）——这条是浏览器量出来的，不是抄来的说法。"""
    js, css = _js(), _css()
    assert "<thead" in _fn(js, "renderMain"), "段头没做成 thead，吸顶就是假的"
    assert 'class="gt' in js, "每段一张表（class=gt）"
    assert "<colgroup><col class=\"kcol\">" in js, \
        "段表自己那份 colgroup 没了：主单表/统计/清理面板也写 colgroup，光扫「colgroup」这个词挡不住"
    assert re.search(r"col\.kcol\{width:var\(--colK\)", css), "字段名列宽挂在 col 上（fixed 表格只认列定义）"
    assert re.search(r"col\.scol\{width:var\(--scolW\)", css), "状态列宽同上"
    assert re.search(r"tr\.grp th\{[^}]*position:sticky", css), "段头没写 sticky"


def test_row_actions_per_state_and_both_entry_points_share_one_data():
    js = _js()
    for act in ("红旗已核", "本就为空", "撤回", "还原"):
        assert act in js, "行内缺这个动作：" + act
    assert "ackE" in js and "ackF" in js, "空值确认与红旗确认要各一份（同一份集合会互相顶掉）"
    assert "data-ack=" in _fn(js, "orphanRows"), "确认入口在：一条旗点了哪几列就挂在哪几行，谁也不点的落在「整张票」那一格"
    assert "flagLines(" in _fn(js, "fieldRowHtml"), "红旗原文要写在它点名的那一格底下（顶部 chip 那排已撤）"


def test_only_todo_keeps_your_own_edits_visible():
    """「只看待核对」把自己的编辑行过滤掉是最难受的一种省：人正在改的那几格会当场消失。"""
    js = _js()
    seg = _fn(js, "renderMain") + (_fn(js, "applyOnlyTodo") if "applyOnlyTodo" in js else "")
    assert "airE" in seg, "只看过滤要把已改的行留下（现系统就是这么做的）"
    assert "只看待核对" in js or "只看待核对" in web_src.part("index.html")


def test_input_events_do_not_rebuild_the_list():
    """稿子 §7 坑点 1：输入只就地更新。全量 render() 会丢焦点、打断中文输入法。"""
    js = _js()
    body = _fn(js, "wireMain")
    i = body.find('addEventListener("input"')
    assert i >= 0, "没找到输入监听"
    seg = body[i:body.find("});", i)]
    assert not re.search(r"\brender\(\)", seg), "input 里调了 render()：焦点与输入法会丢"
    assert "refreshRow" in js


def test_enter_jumps_to_next_todo_without_submitting():
    js = _js()
    seg = _fn(js, "blockEnter")
    assert "Enter" in seg, "短字段回车没接跳待办"
    assert "shiftKey" in seg, "Shift+Enter 要能换行（长字段本来就靠它）"
    assert "submitTicket(" not in seg, "回车顺手提交：那是不可逆动作，只能点按钮"
    assert "nextTodo" in js


def test_topbar_wraps_so_the_view_switch_stays_reachable():
    """顶栏想做单行，1620px 窗口实测那一排要 2326px——「今日归档」排在 2036px 处，
    管理员就这么找不到台账了（用户："管理员账号我找不到今天台账了"）。所以顶栏必须能折行；
    但每个控件内部不折行（「分单模型」被拆成两截比折行难读）。高度仍旧由 JS 实测——
    写死 52px 正是 v1.3.0「表头压住第一行」与 v1.10.3「[hidden] 被 display 顶掉」同族的坑。"""
    js, css = _js(), _css()
    body = _fn(js, "measureChrome")
    assert "--hdh" in body and "offsetHeight" in body, "顶栏高度不再实测了"
    assert re.search(r"\.hdr\{[^}]*flex-wrap:wrap", css), "顶栏不许再强制单行：视图切换会被挤出视野"
    assert ".hdr>*{white-space:nowrap}" in css, "折行要折在控件之间，不是把一句标签拆成两截"
    assert not re.search(r"\.hdr\{[^}]*overflow-x:auto", css), \
        "能折行就别再留横向滚动——那条滚动条就是「看不见入口」的帮凶（没人知道往右拖）"



def test_left_rail_tabs_exist_and_are_wired():
    page, js = web_src.part("index.html"), _js()
    for i in ("railTabs", "tabQueue", "tabMawb", "tabDraft"):
        assert 'id="%s"' % i in page, "左栏标签页缺控件：" + i
    assert 'id="railTabs" role="tablist"' in page, "左栏那组页签不是 tablist：顶上「视图切换」也是 tablist，光扫这个词挡不住"
    assert 'role="tab"' in page
    assert "aria-selected" in page, "标签页要有选中态（读屏器分得出当前在哪一页）"
    for i in set(re.findall(r'\$\("#(tab[A-Z][A-Za-z]*)"\)', js)):
        assert 'id="%s"' % i in page, "desk.js 用到页面里没有的标签页控件：#" + i
    assert "railTab" in js
    body = _fn(js, "railTab")
    assert '$("#railTab"' in body and '$("#tab"' in body, \
        "页签的按钮与面板要按同一个名字拼出来：写成 $(\"#rail\" + id) 会拼成 #railtabXxx，aria-selected 永远设不上（浏览器里量出来的）"


def test_every_grip_survives_the_redesign():
    """三道把手（分栏、字段名列、值格斜角）+ 行高按列存本机是 v1.5.0–1.7.0 交付的能力：
    换成稿子那个固定 430px 就等于把用户已经在用的选择权收回去。"""
    js, css = _js(), _css()
    assert 'id="gripView"' in web_src.part("index.html")
    assert 'class="grip gripCol"' in js, "字段名列那道把手没了（分段之后每段一张表，它是类不是 id：五份同宽的一列共用一个偏好）"
    assert "edgeGripHtml" in js and ".edg" in css
    assert "LS.colK" in js and "LS.split" in js, "两道把手共用的那份偏好不能丢"
    assert "LS.fieldH" in js, "行高按列存本机这条不能丢"


# ── 四、提交闭环：红旗仍是硬门，只有空值能「跳过并留痕」 ─────────────────────────


def test_three_state_button_never_bypasses_the_flags():
    js = _js()
    seg = _fn(js, "drawSubmitBtn")
    for word in ("红旗", "待核对", "提交本票"):
        assert word in seg, "三态按钮少了一态：" + word
    assert "submitTicket(" not in seg, "画按钮的函数不该顺手把票发出去"
    sub = _fn(js, "submitTicket")
    assert "effectiveFlags(t)" in sub, "提交函数不再拦未确认红旗了：那道硬门不能松（effectiveFlags＝红旗减去已确认的那几条）"
    assert sub.index("effectiveFlags(t)") < sub.index("fetch(SUBMIT_URL"), \
        "检查排在发送后面：门就在请求之后，等于没门"


def test_arm_two_step_is_on_the_button_not_the_gate():
    js = _js()
    seg = _fn(js, "armSubmit")
    assert "armed" in seg and "setTimeout" in seg, "二次确认要有自动还原"
    assert "fetch(" not in seg, "第一下不许发请求（与作废、彻底删同一规矩）"


def test_skipping_empties_is_sent_with_the_submit_body():
    """人逐列点过的「本就为空」要随提交体出去（payloadFor 里那份 acked_empty）：
    哪些是"没确认硬发"由服务端按落盘值重算，前端只报确认过哪些，台账里两档才分得开。"""
    js = _js()
    p = _fn(js, "payloadFor")
    assert "acked_empty" in p, "确认过的空栏没进提交体：留痕只剩前端一句话"
    assert "ackE" in p, "payloadFor 里读的是 t.ackE 那份集合，不是重新数一遍空值"


# ── 五、服务端留痕：空栏分「确认过本就为空」与「没确认硬发」两档 ────────────────────

AIR = {"MAWB_NO": "235-96146363", "HAWB_NO": "CLA26090022", "DEST_NAME": "LAX",
       "ORIGIN_NAME": "TAO", "PIECES": 5}


@contextlib.contextmanager
def _desk(tmp: Path, air: dict):
    """把提交这一程会读写的目录全指到临时目录，并落好这张票的 L3（L2 故意不落盘：
    没落盘时服务端按「L3 空即空」算，用例只关心空栏留痕，不必再造一份 L2）。

    主单那两处也必须指开并预置「已提交发送」：分单提交门读的就是主单台账（2026-10-14 定案
    先主单再分单）。不指开的话，这道门就在读这台机器真 output 里的主单台账，换台机器结果就不同。
    """
    stem = "CLA26090022"
    old = (config.SUBMIT_LEDGER, config.ARCHIVE_DIR, config.STAGED_DIR, config.OUTPUT_AIR_DIR,
           config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR,
           config.SUBMIT_GUARD_DIR, config.MASTER_DIR, config.MASTER_LEDGER, auth.USERS_FILE)
    try:
        config.SUBMIT_LEDGER, config.ARCHIVE_DIR = tmp / "submitted.json", tmp / "archive"
        config.STAGED_DIR, config.SUBMIT_GUARD_DIR = tmp / "staged", tmp / "submit_guard"
        config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR = tmp / "air", tmp / "raw"
        config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR = tmp / "qc", tmp / "transcript"
        config.MASTER_DIR, config.MASTER_LEDGER = tmp / "master", tmp / "master_submitted.json"
        auth.USERS_FILE = tmp / "users.json"
        for p in (config.ARCHIVE_DIR, config.STAGED_DIR, config.OUTPUT_AIR_DIR,
                  config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR,
                  config.SUBMIT_GUARD_DIR, config.MASTER_DIR):
            p.mkdir(parents=True)
        (config.ARCHIVE_DIR / stem).mkdir()
        (config.OUTPUT_AIR_DIR / f"{stem}.json").write_text(
            json.dumps(air, ensure_ascii=False), encoding="utf-8")
        store.save_qc(stem, {"source_name": stem + ".pdf", "channel": "vlm",
                             "processed_at": "2026-10-10T09:00:00", "flags": [], "error": ""})
        store.mark_master_submitted(air.get("MAWB_NO") or "235-96146363", "admin", {"mode": "mock"})
        auth.ensure_seed()
        client = TestClient(server.app)
        r = client.post("/login", json={"name": "admin", "password": "admin123"})
        assert r.status_code == 200, f"默认管理员登录失败：{r.status_code} {r.text}"
        yield client
    finally:
        (config.SUBMIT_LEDGER, config.ARCHIVE_DIR, config.STAGED_DIR, config.OUTPUT_AIR_DIR,
         config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR,
         config.SUBMIT_GUARD_DIR, config.MASTER_DIR, config.MASTER_LEDGER,
         auth.USERS_FILE) = old
        shutil.rmtree(tmp, ignore_errors=True)


def _tk(**extra):
    tk = {"stem": "CLA26090022", "filename": "CLA26090022.pdf", "air_reviewed": dict(AIR)}
    tk.update(extra)
    return tk


def _blank_cols():
    """这份夹具里真空着的列，按字段表的顺序——服务端记的就是这个顺序。"""
    return [k for k in fieldspec.desk_keys() if not str(AIR.get(k) or "").strip()]


def test_ledger_splits_confirmed_empties_from_skipped_ones():
    """空栏哪一列是人确认「票面本来就没印」、哪一列是点第二次按钮硬发过去的，服务端分两档记。
    判据由服务端按落盘那份重算：前端报"我只跳过了 1 个"不算数（跟红旗同一个路子）。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_skip_"))
    blanks = _blank_cols()
    with _desk(tmp, AIR) as client:
        r = client.post("/submit", json={"tickets": [_tk(acked_empty=["TO3", "SLAC"])]})
        assert r.status_code == 200, r.text
        led = store.ledger()["CLA26090022"]
        assert led["acked_empty"] == ["TO3", "SLAC"], "确认过的空栏按字段表顺序进台账（留痕要能按列聚合）"
        assert led["skipped_empty"] == [k for k in blanks if k not in ("TO3", "SLAC")], \
            "没确认的空栏要逐列留在 skipped_empty，而不是只记一个数"


def test_empty_track_ignores_columns_that_are_not_empty():
    """人指着有值的列说「本就为空」不该照抄进台账：那种声明没有事实依据，
    记下来只会污染以后按列做的对比。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_skip_"))
    with _desk(tmp, AIR) as client:
        r = client.post("/submit", json={"tickets": [
            _tk(acked_empty=["DEST_NAME", "TO3", "NOT_A_COLUMN"])]})
        assert r.status_code == 200, r.text
        led = store.ledger()["CLA26090022"]
        assert led["acked_empty"] == ["TO3"], "有值的列、字段表里没有的键都不进留痕"
        assert "DEST_NAME" not in led["skipped_empty"], "有值的列不该被算成跳过的空栏"


def test_no_empty_track_keys_means_empty_lists_not_missing():
    """老客户端（不带这两个键）提交的票：acked_empty 是空表、skipped_empty 是全部空格。
    两档都必须是表而不是缺键——统计与导出按"有没有这一档"分支，缺键会让它们当场崩或悄悄漏。
    没确认过就等于没确认过：这里不替老客户端编一份「人都确认过了」。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_skip_"))
    with _desk(tmp, AIR) as client:
        assert client.post("/submit", json={"tickets": [_tk()]}).status_code == 200
        led = store.ledger()["CLA26090022"]
        assert led["acked_empty"] == [], "acked_empty 要落成空表"
        assert led["skipped_empty"] == _blank_cols(), "没人确认过的空格全部记在 skipped_empty 里"


# ── 六、这一轮浏览器里量出来过的三处：分母、行的输入、锚点不许猜 ──────────────────


def test_progress_denominator_is_the_snapshot_at_entry_not_the_column_count():
    """分母是「刚进这张票时的待办数」，不是 38 列：写死列数等于把外部稿那个 0/13 换个写法继续说谎。"""
    js = _js()
    seg = _fn(js, "renderMain")
    assert "t.todoBase = todo.n" in seg, "进票时没抓待办快照，进度没有分母"
    assert "L.progress(t.todoBase" in seg, "renderMain 里的进度不再读那份快照"
    assert "L.progress(t.todoBase" in _fn(js, "refreshChrome"), "就地刷新时进度要跟着同一份快照走"
    assert "FIELDS.length" not in seg and "38" not in seg, "分母从列数或字面量来：那是「一共几列」，不是「还剩几项」"


def test_field_rows_carry_the_inputs_logic_recomputes_from():
    """todoOf/progress 拿每行的 flags/acked/raw/air 重算待办。行里只留一个算完的 st，
    fieldStatus 会把这些行统统当成空值待办——进度分母当场变成 38（2026-10-12 浏览器里量到的）。"""
    js = _js()
    body = _fn(js, "fieldRows")
    for key in ("flags:", "acked:", "raw:", "air:"):
        assert key in body, "fieldRows 没把 " + key.rstrip(":") + " 留在行上，todoOf 只能瞎猜"
    assert "L.fieldStatus" in body, "六态还是由 logic.js 算，别在 desk.js 里另写一份判据"


def test_submit_click_cannot_slip_past_the_flags():
    """三态那颗按钮在「还有红旗」时只做跳转：这条 return 必须在 submitTicket 之前。
    顺序反了就是点一下红按钮把票发出去了——服务端那道门还在，但界面上的话已经说了假话。"""
    js = _js()
    body = _fn(js, "submitClick")
    assert "ticketTodo(t)" in body, "点击时不再数未确认红旗（或绕开了那个唯一算法入口，自己另算一份）"
    m = re.search(r"if \(ef\.length\)\{(.*?)\n  \}", body, re.S)
    assert m and "return;" in m.group(1), \
        "红旗那一档自己没 return：后面那句 if (todo.n…) 里的 return 挡不住它一路走到提交"
    assert m.group(1).index("ef.length") < m.group(1).index("return;"), "先发送再检查？门就在请求之后"
    assert "armSubmit(t)" in body, "空栏那一档的二次确认没接上：琥珀态点了直接发送"


def test_anchors_never_fabricate_a_spot_for_an_empty_field():
    """空栏在真实数据里没有转录出处（fidelity.sources 命中数为 0）。给它画框就是画一个猜出来的框，
    比不画更坏：人会以为那个位置已经核对过。锚点的来源只能是「还没确认的红旗」。"""
    js = _js()
    body = _fn(js, "drawAnchors")
    assert "effectiveFlags(t)" in body, "锚点不再按未确认红旗来挑"
    for banned in ("empty", "isEmptyVal", "ackE"):
        assert banned not in body, "锚点里出现了空值那一档：" + banned + "（空栏不该有框）"


# ── 五、红旗只认一种"已清"：人点过确认（2026-10-12 现场：界面 红旗 0，提交仍被拒）────


def test_no_flag_may_be_hidden_by_a_guess_the_submit_gate_does_not_share():
    """前端从前有一条"税号那格已自动摘掉标签 → 当它已清"的猜测：它把旗从界面上拿掉，
    却**不写进 acked_flags**，而服务端那道门只认 acked_flags 里的字符串（server.py 的
    `f not in acked`）。于是那条旗成了看不见也点不到的死旗——整段核对那颗也只收
    effectiveFlags，同样漏它。现场：NO.A-NGB2609026B 待办显示 红旗 0，提交回 400。
    已清只许两种：人点过确认；或号补齐后服务端重算本来就不再报它（那条两边同源）。"""
    eff = web_src.fn(_js(), "function effectiveFlags(")
    for banned in ("isTaxLabelFlag", "taxFlagCleared"):
        assert banned not in eff, banned + "：前端自己判「这条旗不算数」，服务端不认这个账"
    assert "isMissingAwbFlag" in eff, "缺号那一档要留着：号补上后服务端重算也不报它，两边同源"
    # 「只许两种已清」要钉死，不是点名两个函数就完事：换一种写法（直接匹配旗面文案）照样是死旗
    outs = [l.strip() for l in eff.splitlines() if "return;" in l]
    assert len(outs) == 2, "effectiveFlags 里只许有两个提前退出（人确认过 / 缺的号补上了），现在 " \
        + str(len(outs)) + " 个：多出来的那一个就是服务端不认的自动已清"
    assert any("ack.includes(f)" in l for l in outs), "人点过确认那一档没了：确认了还算未清，红旗永远清不掉"
    assert any("okM && okH" in l for l in outs), "缺号补齐那一档要留着（服务端重算也不报它，两边同源）"


def test_flags_the_server_names_in_a_rejection_become_ackable():
    """服务端 400 的 detail 是 {message, flags}：那串 flags 就是"到底是哪几条"。
    不接住它，制单员只看到光秃秃一句"还有未清的红旗"，而那条旗可能压根没画在界面上
    （改了值之后文案会变，变出来的新旗本地那份没有）。接住 → 并进待确认列表，能点、能提交。
    注：静态用例只能钉"这条链在代码里"，钉不出"这个分支真会进"（把条件改成 if(false) 它照样
    绿）——那一条只有浏览器端到端兜得住，本轮已实测（见留档 §12 v1.17.2）。"""
    js = _js()
    sub = web_src.fn(js, "async function submitTicket(")
    assert "Array.isArray(d.flags)" in sub, "没按 400 的 {message, flags} 那个形状接：detail 换个形状就又看不见"
    assert "t.srvFlags =" in sub, "点名的 flags 没存进这张票：toast 说完就丢，人还是没地方点确认"
    eff = web_src.fn(js, "function effectiveFlags(")
    assert "srvFlags" in eff, "服务端报的旗没并进待确认列表：画不出来也就点不到"
    sd = web_src.fn(js, "function draftBody(t){")
    assert "srvFlags" in sd, "这份列表没进本机草稿：刷新一次就又变成看不见的死旗"


# ── 六、「未保存」只放没交出去的（方案 A，用户定）────────────────────────────────


def test_a_successful_submit_leaves_no_local_draft_behind():
    """从前提交成功走的是 saveDraft(t)：把本机那份又写一遍，而「未保存」那一栏把说明写死成
    "未提交"——票明明交出去了，页签上仍挂着它，人只能反复怀疑没交成。
    方案 A：值与确认都在服务器上了，本机这份当场删掉；只有"提交接口没通、结果只存在这台
    浏览器"的 pending 那份要留（那是唯一副本）。"""
    js = _js()
    sub = web_src.fn(js, "async function submitTicket(")
    cut = sub.index('t.submitted = {mode:"server"')
    ok, pend = sub[cut:], sub[:cut]
    assert "dropDraft(" in ok, "提交成功后没删本机草稿：交出去的票还挂在未保存里"
    assert "saveDraft(t)" not in ok, "成功路径不许再存一份本机副本"
    assert "saveDraft(t)" in pend, "pending 那一份是唯一副本，删了就再也发不出去"
    assert "function dropDraft(" in js and "function pruneSubmittedDrafts(" in js, \
        "没有删这一格的函数：老用户浏览器里那些已提交的副本永远清不掉"
    assert js.count("pruneSubmittedDrafts()") >= 2, \
        "只定义了没在开机时调用一次：历史副本（这次之前已提交却还挂着的）清不掉"
    rd = web_src.fn(js, "function renderDrafts(")
    assert "提交成功后仍可在此回看" not in rd, "空态那句还在承诺「提交后能在这儿回看」，与方案 A 相反"




# ── 七、红旗挂到哪一列：一处判据（2026-10-12 逐条数出四类挂不上列的旗）────────────────


def _keys38():
    import fieldspec
    return [k for k, _lab, _g in fieldspec.DESK_FIELDS]


def test_a_flag_attaches_to_every_column_it_names():
    """挂到哪几列只有 `L.flagKeys` 一处说了算。四类从前挂不上的旗各有来处：
    `L3 ` 前缀把 startsWith 顶掉、句中出现的列名不认、`SHIPPER 电话区号…` 那句里根本没有列名、
    `保真 TAX_ID:` 那个列名早在 v1.12.0 就删了。
    2026-10-13 顶部那排 chip 撤了（用户：以后上面都不要了，红圈都放在字段后面），
    所以"一条旗点了两格"不再是"干脆不挂"，而是**两格都挂**：只挑一格，另一格看着像没问题。
    区号那条从前只写 `SHIPPER 电话区号`（句里没有列名，谁都挂不上），现在 validator 那句
    把两格的列名都写进旗面了——一条谁也不点、界面上又没有 chip 的旗就是没人能确认的死旗。"""
    ks = _keys38()
    got = lambda f: web_src.logic("L.flagKeys(f, ks)", f=f, ks=ks)
    assert got("L3 SHIPPER_INFO_EORI 混进了标签，只填号码本身（航空口径已自动摘掉）: 'VAT 123'") \
        == ["SHIPPER_INFO_EORI"], "L3 前缀不该把这一列顶掉"
    assert got("L3 CONSIGNEE_INFO_EORI 形态异常(过短): '123'") == ["CONSIGNEE_INFO_EORI"]
    assert got("L3 SHIPPER_INFO_EORI 与电话同值，疑似电话栏吞了税号: '00280273000137'") \
        == ["SHIPPER_INFO_EORI"]
    assert got("保真 CONSIGNEE_INFO_TEL: 未在L1逐字转录中找到原文（可能被改写或转录遗漏）（值='0055 11'）") \
        == ["CONSIGNEE_INFO_TEL"]
    # 老形状一条都不许退化
    assert got("TO1 疑似填成承运人码: 'JL'") == ["TO1"]
    assert got("L3 DEST_NAME 未转成 IATA 三字码: 'MANAUS'（码表缺项，需补 CITY_IATA）") == ["DEST_NAME"]
    assert got("CREATE_TIME 格式非 YYYY-MM-DD: '20/09/26'") == ["CREATE_TIME"]
    assert got("HAWB_NO 疑似误填主单号: '999-1234567'") == ["HAWB_NO"]
    assert got("PIECES 缺失或为0") == ["PIECES"]
    # 两栏都为空：说的是两格，就两格都亮
    assert got("MAWB_NO 与 HAWB_NO 均为空") == ["MAWB_NO", "HAWB_NO"]
    assert sorted(got("SHIPPER_INFO_TEL 的电话区号 +39(IT) 与 SHIPPER_INFO_COUNTRY US 不一致，疑似电话串边")) \
        == ["SHIPPER_INFO_COUNTRY", "SHIPPER_INFO_TEL"], "区号那条从前谁也不挂，现在两格都要挂"
    assert got("保真 SHIPPER_INFO_TEL: 与 CONSIGNEE_INFO_TEL: 疑似互换") \
        == ["SHIPPER_INFO_TEL", "CONSIGNEE_INFO_TEL"]
    assert got("保真 TAX_ID: USCI 税号漏抄：票面有 91310000MA1FL2XY3Z，需核票填 SHIPPER_INFO_EORI 或 "
               "CONSIGNEE_INFO_EORI（识别号只有 EORI 这一格，已有号就用 / 拼在一起两个都留）"
               "（值='91310000MA1FL2XY3Z'）") == ["SHIPPER_INFO_EORI", "CONSIGNEE_INFO_EORI"]
    # 一个列名都不点的：交给表尾「整张票」那一格，别猜
    assert got("这条旗不提任何列名") == []
    # 藏在长名字里的列名不算点名（整词边界）：HAWB_NO 出现在 OTHER_HAWB_NO 里不认
    assert got("OTHER_HAWB_NO 与 GOODS_INFO_TEL 都不是这一列") == []


def test_the_flag_to_column_map_has_exactly_one_source():
    """`flagsByField`、chipOne（已撤）与主单那张表从前各抄了一份那五种模式——几份必然走偏
    （这次就是：一处改了另一处没改，旗在 chip 上能确认、在列上永远不显红）。判据只留 logic.js 一份。"""
    js, lg = web_src.part("js/desk.js"), web_src.part("js/logic.js")
    assert 'f.includes(kk +' not in js, "desk.js 里还留着一份自己写的匹配：两处判据迟早又分开"
    assert "flags.filter(f => f.includes(" not in js, "主单那张表也有一份手写的匹配，同样该并到 L.flagKeys"
    assert js.count("L.flagKeys(") >= 3, f"三处（分单挂列 / 主单挂列 / 孤儿旗）都该走同一个算式：{js.count('L.flagKeys(')}"
    assert "flagKeys(f, keys)" in lg, "logic.js 里那份判据本身没了"


def test_a_flag_with_no_column_still_counts_as_a_todo():
    """待办数从前只数行上的旗，于是挂不到列的那两类（区号串边、税号漏抄）能让待办条写「红旗 0」，
    而提交那道门按 effectiveFlags 数——正是 2026-10-12「界面 红旗 0、提交仍被拒」的另一扇门。
    L.todoOf 收第二份名单：没有列可挂的旗要进 n，但不许给它编一个假列名（keys 只收真列）。"""
    rows = [{"k": "TO1", "lab": "航路 1", "g": "route", "flags": 0, "acked": False,
             "raw": "AMS", "air": "AMS"},
            {"k": "PIECES", "lab": "件数", "g": "cargo", "flags": 1, "acked": False,
             "raw": "", "air": ""}]
    one = web_src.logic("L.todoOf(r)", r=rows)
    assert one["n"] == 1 and one["keys"] == ["PIECES"], one
    two = web_src.logic("L.todoOf(r, o)", r=rows, o=["资料里有税号 VAT 123，主单表没有税号列：填进同主体的 EORI 列"])
    assert two["n"] == 2, "没有列可挂的旗也要进待办数，否则这一页会说谎"
    assert two["keys"] == ["PIECES"], "不许为它凭空造一个列名：跳转只能跳到真列上"
    assert two["other"] == 1


def test_a_flag_with_no_column_still_has_a_place_to_be_acked():
    """顶部那排 chip 撤了（2026-10-13 用户：以后上面都不要了）。撤掉的代价不能是"这条旗没人能确认"：
    v1.17.2 与 v1.20.0 各修过一次死旗，症状都是"待办 0、提交却退回来"。
    所以一条列名都不点的旗仍然要有落点（字段表末尾那格「整张票」）、仍然带一颗确认，
    并且待办数走同一个入口。"""
    js = web_src.part("js/desk.js")
    assert "不落在某一列" in js, "挂不到列的旗要自己说明为什么没有列"
    assert "data-ack=" in js and "data-unack=" in js, "它照样要能逐条确认与撤回"
    assert "ticketTodo(" in js, "待办数与提交门要走同一个入口，别在渲染里各算一遍"
    assert '<div class="chips">${chips}</div>' not in js, "顶部那排红旗 chip 已经撤了，两张表都不许再画"


def test_the_phone_country_flag_names_both_columns_it_is_about():
    """旗面不写列名，审核台就挂不上任何一行——顶部那排 chip 撤了之后，连"挂在最上面"这条退路也没了。
    这一条从前写「SHIPPER 电话区号 +39(IT) 与国家 US 不一致」：句里一个列名都没有。
    撤 chip 与改这句是同一个动作的两半，少一半就是一条没人能确认的死旗。"""
    import validator
    warns = validator.validate_raw({"SHIPPER_INFO_TEL": "0039 02 1234", "SHIPPER_INFO_COUNTRY": "US"})
    hit = [w for w in warns if "电话串边" in w]
    assert hit, f"这条该报：{warns}"
    assert "SHIPPER_INFO_TEL" in hit[0] and "SHIPPER_INFO_COUNTRY" in hit[0], hit[0]
    assert web_src.logic("L.flagKeys(f, ks).length", f=hit[0], ks=_keys38()) == 2, "两格都要亮"
