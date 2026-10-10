# -*- coding: utf-8 -*-
"""今日归档（紧凑高密度视图）：交接原型 `archive-dense-view.html` 与《技术交接-今日归档表格》的落地守卫。

原型是"一天几十票一屏扫完"的那版视图，交互以它为准；这一份用例盯两件事：
① 数据面（聚簇/筛选/排序/计数/时间缩写）在 logic.js 里由 node 断输入输出——渲染里再算一遍迟早对不上；
② 交接文档第 5 节那九条坑点（吸顶失效、折叠闪屏、min-height 抖动、收尾二次动画、控制条换行、
   表头写死像素、中途反向、定时器互抢、入场动画重播）逐条钉在源码上——它们都是浏览器里实测出来的，
   复现成本高，所以不许"换个方案"。
样式值另有一条老守卫管着：desk.css/index.html/desk.js 里出现 hex 或 rgba 就是回归
（test_server_http.test_color_literals_only_live_in_tokens），所以原型的色板一律进 tokens.css。
"""
import re

import web_src


def _css():
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  web_src.part("css/desk.css"), flags=re.S)


# ── 一、数据面：logic.js 的纯函数（node 断输入输出）───────────────────────────────
# 后端 /day 给的是"分单行 + 主单行按 processed_at 合流"，并不保证主单紧挨着自己的分单
# （交接文档第 3 节写的"后端保证连续"在真实数据上不成立），所以聚簇必须由前端算。
ROWS = [
    {"kind": "mawb", "mawb": "999-95764373", "stem": "", "processed_at": "2026-10-10T09:00:00",
     "flags": 0, "submitted": None, "failed": False, "voided": None, "filled": 16, "cols_total": 36},
    {"kind": "hawb", "mawb": "999-95764373", "stem": "A1", "processed_at": "2026-10-10T09:02:00",
     "flags": 1, "submitted": None, "failed": False, "voided": None},
    {"kind": "mawb", "mawb": "074-55430340", "stem": "", "processed_at": "2026-10-10T09:03:00",
     "flags": 2, "submitted": None, "failed": False, "voided": None, "filled": 36, "cols_total": 36},
    {"kind": "hawb", "mawb": "99995764373", "stem": "A2", "processed_at": "2026-10-10T09:04:00",
     "flags": 0, "submitted": {"reviewer": "宛平", "at": "2026-10-10T09:20:00"}, "failed": False, "voided": None},
    {"kind": "hawb", "mawb": "07455430340", "stem": "B1", "processed_at": "2026-10-10T09:05:00",
     "flags": 0, "submitted": None, "failed": True, "voided": None},
    {"kind": "hawb", "mawb": "", "stem": "C1", "processed_at": "2026-10-10T09:06:00",
     "flags": 3, "submitted": None, "failed": False, "voided": None},
    {"kind": "hawb", "mawb": "999-95764373", "stem": "A3", "processed_at": "2026-10-10T09:07:00",
     "flags": 0, "submitted": None, "failed": False, "voided": {"by": "admin", "at": "x"}},
]


def test_norm_no_treats_dash_and_space_variants_as_the_same_number():
    """聚簇与折叠都按主单号认组：台账里 999-95764373 与 99995764373 是同一张主单的两种写法
    （模型抄的与人填的），不归一就会分成两组、折叠也只折得动一半。"""
    assert web_src.logic("L.normNo(a)", a="999-95764373") == "99995764373"
    assert web_src.logic("L.normNo(a)", a=" 999 9576 4373 ") == "99995764373"
    assert web_src.logic("L.normNo(a)", a="abc-12") == "ABC12"
    assert web_src.logic("[L.normNo(a), L.normNo(null), L.normNo(b)]", a="", b=None) == ["", "", ""]


def test_cluster_puts_each_master_first_and_keeps_its_own_subs_together():
    """按主单聚簇：组内主单在前、分单随后，组的先后按这一天第一次出现的时间。
    不许改传进来的那份数组——它还是"后端原始顺序"，切回别的排序要用。"""
    out = web_src.logic("L.dayCluster(rs)", rs=ROWS)
    assert [r["stem"] or r["mawb"] for r in out] == \
        ["999-95764373", "A1", "A2", "A3", "074-55430340", "B1", "C1"], out
    assert web_src.logic("rs.slice()", rs=ROWS) == ROWS, "聚簇改了传进来的数组"


def test_cluster_leaves_a_ticket_without_a_master_number_on_its_own():
    """票面没取到主单号的分单不能随便并进上一组（并错了就是"这张票属于谁"说假话），
    自己一组、按时间留在原位；没有分单的主单同样自成一组（箭头是占位的、点不动）。"""
    rows = [{"kind": "mawb", "mawb": "111-11111111", "stem": "", "processed_at": "t1"},
            {"kind": "hawb", "mawb": "", "stem": "X", "processed_at": "t2"},
            {"kind": "hawb", "mawb": "", "stem": "Y", "processed_at": "t3"},
            {"kind": "hawb", "mawb": "111-11111111", "stem": "Z", "processed_at": "t4"}]
    out = web_src.logic("L.dayCluster(rs)", rs=rows)
    assert [r["stem"] or r["mawb"] for r in out] == ["111-11111111", "Z", "X", "Y"], out
    assert web_src.logic("L.daySubCounts(rs)", rs=rows) == {"11111111111": 1}, \
        "没有主单号的分单不该冒出一个空键的组"
    # 两张没主单号的票夹着一组有主单号的：各自的键必须是各自的 stem，
    # 共用一个空键的话它们会被并成一组、整组挪到主单前面去（折叠也会一次收掉两张不相干的票）。
    mixed = [{"kind": "hawb", "mawb": "", "stem": "X", "processed_at": "t1"},
             {"kind": "mawb", "mawb": "111-11111111", "stem": "", "processed_at": "t2"},
             {"kind": "hawb", "mawb": "111-11111111", "stem": "Z", "processed_at": "t3"},
             {"kind": "hawb", "mawb": "", "stem": "Y", "processed_at": "t4"}]
    assert [r["stem"] or r["mawb"] for r in web_src.logic("L.dayCluster(rs)", rs=mixed)] == \
        ["X", "111-11111111", "Z", "Y"], "没主单号的票被并成了一组"


def test_the_four_filters_match_the_handover_rules():
    """全部 / 未提交 / 红旗 / 已提交。未提交要与控制条上那个数同口径（含解析失败的），
    否则"未提交 5"点进去只剩 4 行，制单员当天就得来找人。"""
    assert len(web_src.logic("L.dayFilter(rs, f)", rs=ROWS, f="all")) == 7
    assert [r["stem"] for r in web_src.logic("L.dayFilter(rs, f)", rs=ROWS, f="unsent")] == \
        ["", "A1", "", "B1", "C1", "A3"]
    assert [r["stem"] for r in web_src.logic("L.dayFilter(rs, f)", rs=ROWS, f="flag")] == \
        ["A1", "", "C1"]
    assert [r["stem"] for r in web_src.logic("L.dayFilter(rs, f)", rs=ROWS, f="done")] == ["A2"]
    assert web_src.logic("L.dayFilter(rs, f)", rs=ROWS, f="看不懂的值") == \
        web_src.logic("L.dayFilter(rs, f)", rs=ROWS, f="all"), "认不出的档位退回全部，别给人一张空表"


def test_flag_first_sorts_by_flag_count_then_unsent_and_keeps_ties_in_order():
    """红旗优先：红旗多的在前，同样多时未提交的在前，再一样就保持原顺序（稳定排序，
    同一天连点两次不该看着像重新洗了牌）。"""
    out = web_src.logic("L.dayOrder(rs, m)", rs=ROWS, m="flag")
    assert [(r["stem"] or r["mawb"], r["flags"]) for r in out] == \
        [("C1", 3), ("074-55430340", 2), ("A1", 1),
         ("999-95764373", 0), ("B1", 0), ("A3", 0), ("A2", 0)], out
    assert [r["stem"] for r in web_src.logic("L.dayOrder(rs, m)", rs=ROWS, m="cluster")][:2] == ["", "A1"]


def test_bar_counts_rows_with_flags_not_the_flag_total():
    """控制条那四个数：共 n 票 / 主单 n / 红旗 n 票 / 未提交 n。
    红旗是"有红旗的票数"，不是红旗标记总数（交接文档第 3 节点名了这一条）；
    作废的行一个数都不进，只单独报一个已作废。它复用 daySummary，不再算第二套。"""
    got = web_src.logic("L.dayBar(rs)", rs=ROWS)
    assert got == {"total": 6, "masters": 2, "flagged": 3, "pending": 5,
                   "failed": 1, "flags": 6, "voided": 1}, got
    assert web_src.logic("L.dayBar(rs)", rs=[]) == \
        {"total": 0, "masters": 0, "flagged": 0, "pending": 0, "failed": 0, "flags": 0, "voided": 0}


def test_short_time_drops_the_year_and_the_seconds():
    """提交记录那一格要放进 240px：2026-10-10T09:20:00 整串等宽排下来会挤掉操作人姓名。
    同一年里月日时分足够认票，秒与年留在悬停提示里。"""
    assert web_src.logic("L.shortTime(a)", a="2026-10-10T09:20:00") == "10-10 09:20"
    assert web_src.logic("L.shortTime(a)", a="2026-10-10 09:20:00") == "10-10 09:20"
    assert web_src.logic("[L.shortTime(a), L.shortTime(b), L.shortTime(c)]",
                         a="", b=None, c="mock") == ["", "", "mock"]


# ── 二、九条坑点：钉在源码上 ────────────────────────────────────────────────────
def test_controlbar_keeps_a_constant_height_and_never_wraps():
    """坑 5：窄窗口下控制条换行变高（原型曾出现 80px），表头吸顶位置就和它错位。
    高度恒定、不换行，放不下就在条内横向滚。"""
    css = _css()
    bar = css[css.index(".dvbar{"):css.index("\n", css.index(".dvbar{"))]
    assert "position:sticky" in bar and "top:var(--hdh)" in bar, \
        "控制条要吸在顶栏下面，且 top 走实测令牌（顶栏在 1020-1460px 会折成两排）"
    assert "height:var(--dv-bar-h)" in bar, "控制条高度必须是令牌给的定值"
    assert "flex-wrap:nowrap" in bar, "控制条不许换行"
    assert "overflow-x:auto" in bar, "放不下时条内横向滚，高度不许变"
    assert "--dv-bar-h:40px" in web_src.part("css/tokens.css"), "控制条高度令牌没定义"


def test_table_head_sticks_by_variable_not_by_a_copied_pixel():
    """坑 6：表头吸顶位置 = 顶栏实测高 + 控制条高，两个都走令牌。写死像素的话顶栏一折行就重叠。"""
    css = _css()
    m = re.search(r"#dayBox th\{([^}]*)\}", css)
    assert m, "台账表头没有自己的吸顶规则"
    assert "position:sticky" in m.group(1)
    assert re.search(r"top:calc\(var\(--hdh\)\s*\+\s*var\(--dv-bar-h\)\)", m.group(1)), \
        "表头的 top 要由两个令牌算出来：" + m.group(1)
    assert "px" not in re.search(r"top:[^;]*", m.group(1)).group(0), "表头吸顶位置写死了像素"


def test_the_table_container_clips_instead_of_hiding_overflow():
    """坑 1：overflow:hidden 会让容器成为 sticky 的滚动上下文，而容器自己不滚 ⇒ 表头完全不吸顶。
    要圆角裁剪就用 overflow:clip（只裁剪、不建滚动容器）。"""
    css = _css()
    box = re.search(r"#dayBox\{([^}]*)\}", css)
    assert box, "台账容器没有自己的规则"
    assert "overflow:clip" in box.group(1), "圆角裁剪要用 overflow:clip"
    assert "overflow:hidden" not in box.group(1), "overflow:hidden 会把表头吸顶顶掉"
    assert not re.search(r"#dayCard[^{]*\{[^}]*overflow\s*:\s*(hidden|auto|scroll)", css), \
        "台账外层也不能建滚动容器，否则控制条与表头一起吸不住"


def test_cell_inner_transitions_height_and_opacity_but_never_min_height():
    """坑 3：min-height 写进过渡列表，动画收尾清内联值时它会从 0 再动画回行高——
    肉眼就是"展开到 31px 又掉到 17px 再爬回来"。行高本身也走令牌，别在 JS 里另写一份。"""
    css = _css()
    m = re.search(r"\.dvci\{([^}]*)\}", css)
    assert m, "折叠要作用在 td 里的包裹层 .dvci 上（行高不能作用在 td 上）"
    inner = m.group(1)
    assert "min-height:var(--dv-row)" in inner, "紧凑行高走令牌"
    tr = re.search(r"transition:([^;]*)", inner).group(1)
    assert "height" in tr and "opacity" in tr, "过渡的是 height 与 opacity"
    assert "min-height" not in tr, "min-height 混进过渡列表就会在收尾抖一下：" + tr
    assert re.search(r"body\[data-density=cozy\] \.dvci\{min-height:var\(--dv-row-cozy\)\}", css), \
        "舒适档的行高也要走令牌"
    tok = web_src.part("css/tokens.css")
    assert "--dv-row:31px" in tok and "--dv-row-cozy:39px" in tok, "两档行高没进令牌"


def test_row_height_in_js_is_read_from_the_same_token():
    """行高一个来源：CSS 用 --dv-row，动画的目标高度也从同一个令牌读回来。
    两边各写一个 31 的话，改一次密度就会动画到一半停住。"""
    js = web_src.part("js/desk.js")
    body = js[js.index("function dvRowH("):js.index("\n}", js.index("function dvRowH("))]
    assert "--dv-row" in body and "getComputedStyle" in body, "目标行高要从令牌实测回来"
    assert re.search(r"return parseFloat\(v\)", body), \
        "量回来的那个值没被用上：等于又在 JS 里写死了一份行高"


def test_collapse_never_rebuilds_the_table():
    """坑 2：折叠时重渲染整表（innerHTML 重建）+ 每行入场动画 = 整表闪一下。
    折叠只切状态集合 + 对现有行做高度过渡。"""
    js = web_src.part("js/desk.js")
    for fn in ("function dvToggle(", "function dvAnimate("):
        i = js.index(fn)
        body = js[i:js.index("\nfunction ", i + 1)]
        assert "innerHTML" not in body, fn + " 里重建了 DOM，折叠会闪屏"
        assert "renderDay" not in body, fn + " 里触发了整体重渲染"


def test_animation_cleanup_silences_the_transition_first():
    """坑 4：动画收尾要清掉内联的 height/min-height/opacity 交还 CSS，
    但必须先关过渡、强制 reflow、再清值，否则残留属性变化会触发第二次动画（抖动根因）。"""
    js = web_src.part("js/desk.js")
    i = js.index("function dvAnimate(")
    body = js[i:js.index("\nfunction ", i + 1)]
    tail = body[body.index("setTimeout("):]
    off = tail.find('el.style.transition = "none"')
    assert off >= 0, "收尾没有先把过渡关掉"
    clear = tail.find('el.style.height = ""')
    assert clear >= 0, "收尾没把内联高度交还 CSS"
    assert off < clear, "先清内联值再关过渡 = 收尾又动画一遍"
    assert "offsetWidth" in tail, "少了强制 reflow，同一个 tick 里的样式改动会被合并"


def test_expand_starts_from_the_current_height_and_timers_are_per_group():
    """坑 7 + 坑 8：动画中途再点要从当前实际高度接续（不能写死 0 或目标值），
    而且「全部折叠」时各组并行，定时器必须按主单号分开存——共用一个的话后启动的组会
    清掉前一组的收尾回调，前一组就永远停在半路（内联高度不清、行高再也回不到 CSS）。"""
    js = web_src.part("js/desk.js")
    i = js.index("function dvAnimate(")
    body = js[i:js.index("\nfunction ", i + 1)]
    assert "offsetHeight" in body, "起点高度要量当前实际值"
    assert re.search(r"DV\.timers\[\w+\]", body), "定时器要按组分别保存"
    assert "clearTimeout(DV.timers[" in body, "同一组连点两次要先清掉上一个定时器"


def test_the_entry_fade_plays_only_on_a_fresh_load():
    """坑 9：入场淡入只在首次挂载时播。筛选/排序/折叠导致的重渲染不许重播，否则列表频繁闪。"""
    css, js = _css(), web_src.part("js/desk.js")
    assert not re.search(r"#dayBox tbody tr\{[^}]*animation", css), \
        "淡入挂在了裸的 tbody tr 上：每次重渲染都会重播"
    assert re.search(r"#dayBox tbody tr\.dvin\{animation:", css), "淡入要挂在首次挂载才加的类上"
    assert "fade" in js[js.index("function renderDay("):js.index("\nfunction ", js.index("function renderDay(") + 1)], \
        "重渲染要能区分「这是新数据」与「只是换了个筛」"


def test_the_collapsed_state_survives_a_rerender():
    """折叠状态是全局的（以主单号为键），切换筛选/排序/密度之后仍然保留：
    重渲染时按状态集合把 is-collapsed 与箭头的 closed 一起还原。"""
    js = web_src.part("js/desk.js")
    # 折叠态在行模板里还原（renderDay 只是把 DV.collapsed 交给它），所以两个函数一起看
    body = js[js.index("function dvRowHtml("):js.index("\nfunction ", js.index("function renderDay(") + 1)]
    assert "DV.collapsed" in body and "is-collapsed" in body, "重渲染没还原折叠态"
    assert "show-hint" in body and "closed" in body, "重渲染没还原箭头与「已折叠」提示"


def test_density_is_a_local_preference():
    """密度存本机（各人屏幕不一样），换页/刷新都还在；改密度不重渲染，行高由 CSS 令牌接管。"""
    js = web_src.part("js/desk.js")
    assert "dayDensity" in js[js.index("const LS = {"):js.index("\n", js.index("const LS = {"))], \
        "密度没有自己的 localStorage 键"
    i = js.index('$("#dayDensity")')
    wiring = js[i:js.index("});", i)]
    assert "localStorage.setItem" in wiring and "dataset.density" in wiring, "切密度要写回本机并落到 body 上"
    assert "renderDay" not in wiring, "切密度不该重渲染整表"


def test_columns_and_cells_follow_the_handover_layout():
    """七列的次序与宽度（宽度是版面常量，走 class 不走内联样式——内联那条老守卫会拦），
    以及红旗格的红底挂在红旗那一列上（原型写的是 nth-child(4)，落在提交记录列，是它的笔误）。"""
    js, css = web_src.part("js/desk.js"), _css()
    head = js[js.index("<th class=\"w-doc\""):js.index("</tr></thead>", js.index("<th class=\"w-doc\""))]
    assert [c for c in re.findall(r"<th[^>]*>([^<]*)</th>", head)] == \
        ["单据", "状态", "红旗", "提交记录", "缩影", "附件", "操作"], head
    for w in ("w-doc", "w-flag", "w-rec", "w-snap", "w-att", "w-op"):
        assert re.search(r"#dayBox th\.%s\{width:\d+px\}" % w, css), w + " 的列宽要在样式表里给"
    assert re.search(r"#dayBox tr\.flagged td\.c-flag\{background:", css), \
        "红旗行的红底要落在红旗那一格（用类，别用 nth-child 数列）"
    row = js[js.index("function dvRowHtml("):js.index("\nfunction ", js.index("function dvRowHtml(") + 1)]
    assert '<td class="c-flag">' in row, "红旗那一格没挂 c-flag，样式表里那条红底就落不下去"
    assert 'style="' not in js[js.index("function dvRowHtml("):js.index("\nfunction ", js.index("function dvRowHtml(") + 1)], \
        "台账行里不许有内联样式"


def test_the_flag_is_an_inline_svg_painted_by_currentcolor():
    """红旗是旗帜 SVG + 等宽 ×n，不是 emoji 也不是"红冲/红圈"那种词；
    颜色走 currentColor（desk.js 里出现 hex 就是回归），文字用 -text 那一支才过 AA。"""
    js, css = web_src.part("js/desk.js"), _css()
    assert "<svg" in js and "stroke=\"currentColor\"" in js, "旗帜要用内联 SVG + currentColor"
    assert re.search(r"\.dvfc\{[^}]*color:var\(--c-bad-text\)", css), "红旗文字要用可读的那一支红"
    assert not re.search(r"\.dvfc[^{]*\{[^}]*color:var\(--c-bad\)", css), "状态填充色不能当文字色"


def test_clickables_in_the_dense_rows_are_real_buttons_or_links():
    """折叠箭头、单号、附件、作废都得是键盘能到的元素；javascript: 伪链接不许出现。"""
    js = web_src.part("js/desk.js")
    assert "javascript:" not in js, "伪链接键盘到不了、也念不出来"
    row = js[js.index("function dvRowHtml("):js.index("\nfunction ", js.index("function dvRowHtml(") + 1)]
    assert '<button type="button" class="dvchev' in row, "折叠箭头要是真按钮"
    assert 'data-open=' in row and 'data-mopen=' in row, "分单号与主单号仍然点开原票/回主单检索"
    assert row.index("<button") < row.index("dvchev"), "箭头那个元素得写成 button"


def test_the_dense_view_keeps_void_restore_and_only_void_filtering():
    """换成高密度视图不许把已有的业务动作弄丢：两步确认的作废、恢复、「只看作废」、
    点分单号回工作台、主单号回主单检索、公司发送状态指路。"""
    js, page = web_src.part("js/desk.js"), web_src.part("index.html")
    assert 'id="dayOnlyVoid"' in page and 'id="dayCollapse"' in page
    for frag in ('data-void="', 'data-undo="', 'data-vno=', '"/void"', "L.dayVisible",
                 "openHouse(", "主单检索", "L.dayBar"):
        assert frag in js, "少了 " + frag
    for seg in ("dayFilter", "daySort", "dayDensity"):
        assert 'id="%s"' % seg in page, seg + " 这组控件没了"
    for label in ("全部", "未提交", "红旗", "已提交", "按主单聚簇", "红旗优先", "紧凑", "舒适", "全部折叠"):
        assert label in page, "控制条少了 " + label


def test_every_fetch_in_the_day_view_still_goes_through_the_mount_prefix():
    """页面挂在 /hawb/ 下：新写的渲染不许把端点写成根路径（踩过一次，本机测不出来）。
    盯的是「第一个参数就是写死的 /xxx」那种写法——LOC.base 这类变量本来就是 BASE 拼出来的。"""
    js = web_src.part("js/desk.js")
    bad = [m.group(0) for m in re.finditer(r"""fetch\(\s*(['"`])/""", js)]
    assert not bad, "这些 fetch 没带挂载前缀：" + "、".join(bad)
    assert 'fetch(BASE + "/day"' in js, "台账这一个端点必须点名走 BASE，别只靠上一条的否证"


def test_reduced_motion_covers_the_new_animations():
    """折叠过渡与入场淡入也要能被"减少动态效果"关掉——但只关位移与淡入，
    折叠本身（高度归零）是功能不是装饰，靠 is-collapsed 兜住。"""
    css = web_src.css()
    blk = re.findall(r"@media \(prefers-reduced-motion:reduce\)\{(.*?)\n\}", css, re.S)
    assert blk, "没有减少动态效果的降级"
    body = "\n".join(blk)
    assert ".dvci" in body and "dvin" in body, "台账那两处新动效没进降级名单"
    assert "!important" not in body, "别用大锤关动画"
