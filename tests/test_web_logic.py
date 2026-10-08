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


def _pdf_head(box="[ 0 0 595.276 841.89 ]"):
    """造一段最小 PDF 头给 pdfBox 喂（bytes 只能 ASCII：整段就是 ASCII）。"""
    return [b for b in ("%PDF-1.4\n1 0 obj\n<< /Type /Page /MediaBox " + box + " >>\nendobj\n").encode("ascii")]


A4 = {"w": 595.276, "h": 841.89}          # 本机票面实测都是纵向 A4


def test_pdfbox_reads_page_size_from_the_bytes_we_already_hold():
    """票面字节前端本来就有（上传的 File / 取回来的 blob），扫一下就不用为尺寸再发一次请求。"""
    assert _eval("L.pdfBox(b)", b=_pdf_head()) == {"w": 595.276, "h": 841.89}
    assert _eval("L.pdfBox(b)", b=_pdf_head("[ 0 0 842 595 ]")) == {"w": 842, "h": 595}, "横向票要按横向算"
    assert _eval("L.pdfBox(b)", b=[x for x in b"%PDF-1.7 no page dict here"]) is None, "读不到就返回 null，别编一个"


def test_view_cycle_drops_the_state_that_looks_identical():
    """窄而高的票面栏里，纵向 A4 的『整页』和『适应宽度』算出来是同一个缩放——
    留着它就是那颗点了没反应的死按钮。这种重合的一档必须被丢掉。"""
    c = _eval("L.viewCycle(p, q)", p=A4, q={"w": 500, "h": 800})
    keys = [x["k"] for x in c]
    assert "fit" not in keys, "整页与适应宽度重合时还留着它，就是给用户一颗死按钮"
    zs = [x["zoom"] for x in c]
    assert zs == sorted(zs) and all(b > a * 1.09 for a, b in zip(zs, zs[1:])), \
        "留下的每一档都要真的看得出差别：%s" % zs
    assert keys[-1] == "detail", "最后一档是放大看细节，这才是制单员点它想要的东西"


def test_view_cycle_keeps_all_three_when_the_pane_is_wide():
    """栏位够宽时三态各有不同缩放，就该都给（数字含阅读器自己占的那 60×56）。"""
    c = _eval("L.viewCycle(p, q)", p=A4, q={"w": 900, "h": 600})
    assert [x["k"] for x in c] == ["fit", "fith", "detail"], "整页→适应宽度→看细节，由小到大"
    assert [x["zoom"] for x in c] == [65, 141, 200], f"按净宽 840/净高 544 算：{[x['zoom'] for x in c]}"


def test_detail_step_only_exists_when_it_is_actually_bigger():
    """栏位极大时『适应宽度』本身已经 300%+，那『放大看细节』就没意义（200% 反而是缩小）：
    这一档要不出现在循环里，而不是给用户一档越点越小的按钮。"""
    c = _eval("L.viewCycle(p, q)", p=A4, q={"w": 2000, "h": 1400})
    assert [x["k"] for x in c] == ["fit", "fith"], f"放大档被吸掉了才对：{[x['k'] for x in c]}"
    mid = _eval("L.viewCycle(p, q)", p=A4, q={"w": 900, "h": 600})
    assert mid[-1]["k"] == "detail" and mid[-1]["frag"] == "zoom=200", "正常栏位下这一档要在，且拼成 zoom="


def test_view_cycle_wraps_back_to_the_start():
    """循环：最后一档点回去第一档，用户不用找『退出放大』在哪。"""
    c = _eval("L.viewCycle(p, q)", p=A4, q={"w": 900, "h": 600})
    assert _eval("L.nextView(c, c[0].k).k", c=c) == "fith"
    assert _eval("L.nextView(c, c[2].k).k", c=c) == "fit", "到底了要回绕"
    assert _eval("L.nextView(c, '没见过的档').k", c=c) == "fit", "状态丢了就从第一档开始，别返回 undefined"


def test_view_cycle_without_page_size_still_offers_something():
    """扫不到 MediaBox（PDF 变体）时不能白屏：退回原来那两档，行为不比今天差。"""
    c = _eval("L.viewCycle(null, q)", q={"w": 500, "h": 800})
    assert [x["k"] for x in c] == ["fith", "fit"], "没尺寸就用浏览器的 view= 两态"
    assert all("frag" in x for x in c), "每档都要带上能拼进 iframe 地址的参数"


def test_first_view_stays_on_fit_width():
    """打开票面默认停在『适应宽度』：那是窄栏里看得清字的一档。
    档位排序后它是第二个，所以必须由 logic 明确指定，不能拿排序后的第一个当默认。"""
    c = _eval("L.viewCycle(p, q)", p=A4, q={"w": 900, "h": 600})
    assert _eval("L.firstView(c).k", c=c) == "fith", "默认档丢了，票一打开就缩成一小块"
    only = [x for x in c if x["k"] != "fith"]
    assert _eval("L.firstView(only).k", only=only) == only[0]["k"], \
        "适应宽度被丢掉时要退回最小的一档，别返回空"
    assert _eval("L.firstView([])") is None


def test_whole_page_width_for_image_pages():
    """页图模式下『整页看全』= 让一页的高也放得下时的图宽（占栏位宽的百分数，上限 100）。
    纵向票放进窄而高的栏里它等于 100（和『适应宽度』同一件事），那种情况下这颗按钮该藏起来。"""
    assert _eval("L.wholePagePct(595, 842, 500, 800)") == 100, "窄高栏里纵向票的整页=适应宽度，别放一颗死按钮"
    assert _eval("L.wholePagePct(595, 842, 900, 600)") == 47, "宽栏里整页要缩到 47% 才放得下整页高"
    assert _eval("L.wholePagePct(842, 595, 900, 600)") == 94, "横向票差 6% 就同一件事，desk.js 要在 ≥90 时藏掉这颗"
    assert _eval("L.wholePagePct(0, 0, 0, 0)") == 100, "尺寸没读到就退回 100，别算出 NaN"


def test_image_zoom_cannot_exceed_the_rendered_pixels():
    """放大上限由页图像素决定：超过原生像素只是把糊图放大，还占掉更多滚动条。"""
    assert _eval("L.maxZoomPct(1190, 675)") == 176, "A4 @scale2 = 1190px，675 栏位最放到 176% 就见底了"
    assert _eval("L.maxZoomPct(1190, 4000)") == 100, "栏位比图还宽时不许放大（放大也只会更糊）"
    assert _eval("L.maxZoomPct(0, 675)") == 100


def test_wheel_zoom_steps_are_multiplicative_and_bounded():
    """滚轮一格一格调倍率：乘法步进比固定 ±25 自然（小倍率时 25% 一跳太猛，大倍率时又太细）。
    上下都要有界，滚到头不能变成 0 或负宽。"""
    assert _eval("L.zoomStep(100, 1)") == 115
    assert _eval("L.zoomStep(100, -1)") == 87
    assert _eval("L.zoomStep(115, -1)") == 100, "来回一格要回到原值，不能越走越偏"
    assert _eval("L.zoomStep(25, -1)") == 25, "下限"
    assert _eval("L.zoomStep(400, 1)") == 400, "上限"
    assert _eval("L.zoomStep(0, 1)") == 115, "没读到当前倍率时按 100 起步"


def test_zoom_keeps_the_point_under_the_cursor():
    """缩放要钉住鼠标底下那一处：不然每次滚轮都把视野甩回左上角，看细节得重新找位置。"""
    assert _eval("L.zoomAnchor(1000, 0.5, 200)") == 300, "内容中点仍在光标处"
    assert _eval("L.zoomAnchor(2000, 0.25, 300)") == 200
    assert _eval("L.zoomAnchor(1000, 0.1, 500)") == 0, "越界就贴边，别给负数滚动量"


def test_desk_binds_the_wheel_on_our_pane_only():
    """滚轮归缩放，所以必须挡住它原来的动作（滚页面 / Ctrl+滚轮=整页浏览器缩放）；
    但退回浏览器阅读器那张票不能抢——那扇 iframe 的滚轮该由阅读器自己处理。"""
    js = web_src.part("js/desk.js")
    assert '"wheel"' in js, "票面栏要接滚轮"
    assert "L.zoomStep(" in js and "L.zoomAnchor(" in js, "步进与锚点算式要走 logic"
    assert "preventDefault" in js.split("function wheelZoom")[1][:900], "不挡住滚轮就会连带滚页面/缩放整个标签页"
    assert 'PREVIEW.mode === "viewer"' in js.split("function wheelZoom")[1][:400], \
        "阅读器那张票要把滚轮让回去"
    assert "LOC.on" in js.split("function wheelZoom")[1][:400], \
        "定位模式的倍率是 autoZoom 按命中行算的，滚轮去改会跟它打架"
    assert "passive: false" in js, "passive 监听里 preventDefault 会被忽略，滚轮还是去滚页面"


def test_pan_only_offered_when_content_overflows():
    """放大后要能按住拖动看别处（不用去够滚动条）。但内容没超出栏位时不给抓手——
    给了就是骗人：按住拖半天一动不动，比没有还糟。"""
    assert _eval("L.canPan(1200, 675)") is True, "内容比栏位宽就该能横着拖"
    assert _eval("L.canPan(676, 675)") is True, "只溢出 1px 也算，别用等号漏掉"
    assert _eval("L.canPan(675, 675)") is False, "正好放得下时不给抓手"
    assert _eval("L.canPan(400, 675)") is False, "缩得下更不给"
    assert _eval("L.canPan(0, 675)") is False, "还没量到尺寸时不许亮抓手"


def test_desk_installs_drag_to_pan_on_the_preview_pane():
    """抓手要有：按下抓指针（拖出栏位也不丢）、拖动改 scrollLeft/Top、抬起收手；
    并且要压掉浏览器原生的"拖图片"，否则拖出去的是图片本身。"""
    js = web_src.part("js/desk.js")
    css = web_src.css()
    assert "L.canPan(" in js, "能不能拖要由 logic 判，别在 DOM 里现写比较"
    for ev in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
        assert ev in js, "缺 " + ev + "：中途松手或拖出栏位会卡在抓取状态"
    assert "setPointerCapture" in js, "不抓住指针，拖出图片外就断了"
    assert "scrollLeft" in js and "scrollTop" in js, "拖动要落在滚动位置上"
    assert 'classList.remove("pannable")' in js, \
        "退回浏览器阅读器时要收掉抓手：那扇 iframe 自己管滚动，留着就是亮着却拖不动的假抓手"
    assert "draggable" in js or "user-drag" in css, "别把图片本身拖出去（浏览器默认能拖）"
    assert "grab" in css and "grabbing" in css, "光标要真的变成抓手"


def test_desk_renders_pages_itself_and_keeps_the_viewer_only_as_fallback():
    """票面不再嵌浏览器阅读器：Edge/Chrome 各自的侧栏与工具栏会吃掉近 1/4 宽度，
    而且参数两家不通用（实测 Edge 不理 navpanes=0）。走我们自己的 /render 页图，
    缩放与『整页看全』全归我们算；只有拿不到归档（没勾落盘）才退回阅读器。"""
    js = web_src.part("js/desk.js")
    assert "/render/" in js, "票面要走服务端按页渲染"
    assert "X-Ticket-Pages" in js, "页数从渲染响应的头里拿，别再发一次请求问"
    assert "L.wholePagePct(" in js and "L.maxZoomPct(" in js, "整页宽与放大上限要走 logic 那两条算式"
    assert ">= 90" in js, "整页与适应宽度差不到 10% 时要把这颗按钮藏起来"
    assert "showPages" in js and "pvpage" in js, "页图模式要有自己的装载函数与图片类名"


def test_desk_wires_the_preview_toggle_to_logic_not_its_own_math():
    """算式只在 logic.js 一处：desk.js 负责取字节、量栏位、拼地址，别再自己写一遍 min/max。"""
    js = web_src.part("js/desk.js")
    assert "L.viewCycle(" in js and "L.nextView(" in js, "阅读器回退路径的档位仍要走 logic"
    assert "L.firstView(" in js, "默认档位的选法也要走 logic，别再按排序结果猜"
    assert "L.pdfBox(" in js, "页面尺寸要从已在手的 PDF 字节里读"
    assert "arrayBuffer" in js, "blob 字节要读出来才能扫 MediaBox"
    assert "PREVIEW.pdfFit === " not in js, "别再留原来那颗二态硬编码"
    assert "cycle.length < 2" in js, "只剩一档时这颗按钮就是死的，要藏起来而不是留着让人点"



def test_stagedMerge_prefers_the_server_but_names_the_conflict():
    """打开别人暂存的票时，自己那份没存过的改动不能悄悄被盖掉，也不能悄悄盖掉别人。

    规则：服务器（别人核对过的）赢，本地这份不删——它还在「本机未保存的编辑」里；
    但冲突的字段必须点名报给人，否则人会以为自己刚填的值还在。
    只在本机改过的字段（服务器没动的）保留本地值。
    """
    model = {"DEST_NAME": "LAX", "PIECES": 1, "GOODS_INFO": "STEEL"}
    final = {"DEST_NAME": "ORD", "PIECES": 1}
    local = {"DEST_NAME": "MAD", "GOODS_INFO": "STEEL PARTS"}
    assert web_src.logic("L.stagedMerge(m, f, l)", m=model, f=final, l=local) == {
        "airE": {"DEST_NAME": "ORD", "GOODS_INFO": "STEEL PARTS"},
        "conflicts": ["DEST_NAME"], "local": {"DEST_NAME": "MAD"}}
    # 没暂存过：本地改动照旧生效，没有冲突可言
    assert web_src.logic("L.stagedMerge(m, f, l)", m=model, f=None, l=local) == {
        "airE": {"DEST_NAME": "MAD", "GOODS_INFO": "STEEL PARTS"}, "conflicts": [], "local": {}}
    # 服务器改了、本地没碰：直接采用，不许报冲突（报多了人就学会忽略提示）
    r = web_src.logic("L.stagedMerge(m, f, l)", m=model, f=final, l={})
    assert r["airE"] == {"DEST_NAME": "ORD"} and r["conflicts"] == [], r
