# -*- coding: utf-8 -*-
"""解析正确率（分析看板）：交接原型 `accuracy-report-redesign.html` 与《技术交接-解析正确率看板》的落地守卫。

原版三个区块纵向平铺、关键数字不突出、「无留痕」满屏刷屏、改动字段长文本换行——看板要解决的就是这四件。
这一份用例盯三件事：
① 数据面（分段归属/计数/筛选、口径闭环两条等式、条形比例、字段标签折叠）在 logic.js 里由 node 断输入输出：
   准确率本身仍然只有 stats.py 一处算（test_stats.py 钉着），前端连"数一下有几行"都不许在渲染里写第二遍；
② 交接文档第 5 节那八条坑点（width 掉帧、长文本换行、无留痕被删、行高两处各写一遍、动画重播、
   overflow:hidden 顶掉吸顶、数字非等宽、口径不闭环）逐条钉在源码上；
③ 这一页是**窗口**不是页面（2026-10-09 用户定案，test_stats.py 也钉着），日期/分组/刷新/导出/关闭
   都是已经接好的真控件，改版不许把它们换成原型里那几个假按钮。
色值另有一条老守卫管着：desk.css/index.html/deck.js 里出现 hex 或 rgba 就是回归
（test_server_http.test_color_literals_only_live_in_tokens），所以原型那套 --bg/--ink/--accent 一律映射到
tokens.css 里已有的令牌——交接文档第 4.1 节说的「直接照搬」在本仓库做不到，变量名与值都不是同一套。
"""
import re

import web_src

# ── 夹具：按 stats.ticket_row 的真实形状摆，不是按原型的 {m,s,up,cmp,diff,fields} ──────
# 原型那 17 行照搬过来（4 张可算 + 13 张无留痕），这样交接文档 §3.4 的两条闭环等式
# 与 §8 的四个分段计数（17/1/3/13）都能拿真数核。
def _row(stem, mawb, hawb, comparable=None, total=None, doc=(), inp=(), legacy=False,
         uploader="admin", role="admin", stager="admin", state="submitted", review=False):
    return {"stem": stem, "mawb": mawb, "hawb": hawb, "state": state, "legacy": legacy,
            "total": total, "doc": list(doc), "inp": list(inp),
            "comparable": 0 if legacy else (comparable or 0),
            "fields": [], "model": "intern-s2", "model_choice": "preset",
            "uploader": "" if legacy else uploader, "uploader_role": "" if legacy else role,
            "stager": "" if legacy else stager, "no_inputter_review": review,
            "when": "2026-10-10T09:00:00", "source_name": stem + ".pdf"}


ROWS = [_row("S1", "999-95764373", "VCE4373", 27, 4,
             ["HAWB_NO", "SHIPPER_INFO", "SHIPPER_INFO_COMP_ADDRESS", "SHIPPER_INFO_COUNTRY"])]
ROWS += [_row("K%d" % i, "999-96009023", "T%d" % i, legacy=True) for i in range(13)]
ROWS += [_row("S2", "131-33059924", "BJS24041749", 27, 0),
         _row("S3", "232-19509512", "BJS00032108", 26, 0),
         _row("S4", "074-55430340", "NO.A-NGB2609026G", 24, 0, review=True)]

# stats.summary 对这批行会给出的那一份聚合值（前端只读它，不重算）
SUMMARY = {"tickets": 4, "legacy": 13, "fields_comparable": 104, "edits_total": 4,
           "edits_doc": 4, "edits_inp": 0, "clean": 3, "clean_rate": 0.75,
           "acc_field": 0.9615, "acc_after_review": 1.0, "unreviewed": 1,
           "by_field": [{"field": "HAWB_NO", "edits": 1, "tickets": 1},
                        {"field": "SHIPPER_INFO", "edits": 1, "tickets": 1},
                        {"field": "SHIPPER_INFO_COMP_ADDRESS", "edits": 1, "tickets": 1},
                        {"field": "SHIPPER_INFO_COUNTRY", "edits": 1, "tickets": 1}]}


def _css():
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  web_src.part("css/desk.css"), flags=re.S)


def _js():
    return web_src.part("js/desk.js")


def _jsCode():
    """剥掉块注释再读 desk.js（保住行数）。变异验证抓到两次"断言被自己的注释喂饱"：
    口径条那句「不进分母」被改掉之后用例照样绿，因为上面那行注释里也写着「不进分母」。
    要钉"界面会说出这几个字"，就必须只看会进模板的那部分。"""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  web_src.part("js/desk.js"), flags=re.S)


def _fn(js, name):
    """按函数名切一段源码。切不到就给一条干净的断言失败，不是 ValueError——
    用例要红在「这条规则没实现」，红在「切片切崩了」等于没红。"""
    i = js.find("function " + name + "(")
    assert i >= 0, "desk.js 里还没有 %s 这个函数" % name
    j = js.find("\nfunction ", i + 1)
    return js[i:j] if j > 0 else js[i:i + 3000]


def _after(js, needle, span=700):
    i = js.find(needle)
    assert i >= 0, "desk.js 里找不到 %r 这一段" % needle
    return js[i:i + span]


# ── 一、数据面：logic.js 的纯函数（node 断输入输出）───────────────────────────────

def test_the_four_segments_partition_the_rows_and_carry_live_counts():
    """四档分段（§3.4）必须互斥且穷尽：一行只能落一档，四档加起来等于总行数。
    计数小字常驻在按钮上，所以计数与筛选必须是同一个归属判据——各写一遍的话，
    「有改动 1」旁边列出 2 行这种事迟早会发生，而这种不一致比数字错更难被发现。"""
    seg = web_src.logic("rows.map(r => L.statsSeg(r))", rows=ROWS)
    assert seg.count("trace") == 13 and seg.count("changed") == 1 and seg.count("clean") == 3, seg
    assert set(seg) <= {"trace", "changed", "clean"}, "出现了第五种归属，分段按钮上没有它"
    c = web_src.logic("L.statsCounts(rows)", rows=ROWS)
    assert c == {"all": 17, "changed": 1, "clean": 3, "trace": 13}, \
        "交接文档 §8 点名的四个计数（17/1/3/13）对不上"
    assert c["changed"] + c["clean"] + c["trace"] == c["all"], "四档不穷尽：有行会被任何一档都筛掉"


def test_a_legacy_row_is_never_counted_as_a_clean_one():
    """无留痕行的 total 是 None，不是 0：算成「改动 0」会把 13 张历史票变成 13 张一次通过，
    一次通过率直接从 75% 跳到 100%——而这个数是拿去看"能不能不经过人直接对接"的。"""
    legacy = [r for r in ROWS if r["legacy"]][0]
    assert web_src.logic("L.statsSeg(r)", r=legacy) == "trace"
    assert legacy["total"] is None, "夹具本身要先成立：真实 legacy 行的 total 就是 None"
    assert web_src.logic("L.statsCounts(rows).clean", rows=ROWS) == 3


def test_segment_filtering_returns_exactly_what_the_count_says():
    for seg, n in (("all", 17), ("changed", 1), ("clean", 3), ("trace", 13)):
        got = web_src.logic("L.statsFilter(rows, f).length", rows=ROWS, f=seg)
        assert got == n, "%s 档筛出 %d 行，计数小字写的是 %d" % (seg, got, n)
    assert web_src.logic("L.statsFilter(rows, 'nope').length", rows=ROWS) == 17, \
        "认不出的档要退回全部：退回空表的话，人会以为这个窗口内真的一张票都没有"


def test_the_scope_bar_closes_both_identities_and_says_so_when_it_does_not():
    """§5.8 口径必须闭环，而且前端要自己比一遍：后端聚合值与明细若不是同一批票
    （比如时间窗一边含端点一边不含），分母口径就悄悄错了，画出来的百分比全都不能用。
    这条自检不是重算准确率——它比的是「行数」与「可比列数之和」两个恒等式。"""
    ok = web_src.logic("L.statsClosure(s, rows)", s=SUMMARY, rows=ROWS)
    assert ok == {"rowsMatch": True, "colsMatch": True}, ok
    short = dict(SUMMARY, tickets=3)
    assert web_src.logic("L.statsClosure(s, rows).rowsMatch", s=short, rows=ROWS) is False, \
        "可算 3 + 无留痕 13 != 17 行，这时候还照画就是画一个对不上明细的百分比"
    wide = dict(SUMMARY, fields_comparable=105)
    assert web_src.logic("L.statsClosure(s, rows).colsMatch", s=wide, rows=ROWS) is False
    assert web_src.logic("L.statsClosure(null, null)").get("rowsMatch") is True, \
        "空窗口（0+0=0 行）是合法状态，不许报成口径错"


def test_bar_length_is_a_ratio_against_the_busiest_field_and_never_a_division_by_zero():
    """§3.3 条长 = 被改次数 ÷ 最大次数，等次数时满格。最大为 0（窗口内没人改过任何列）时
    必须给 0 而不是 NaN/Infinity——NaN 写进宽度，浏览器会静默当没这条声明，条子就停在满格。"""
    bars = web_src.logic("L.statsBars(s.by_field)", s=SUMMARY)
    assert [b["pct"] for b in bars] == [1, 1, 1, 1], "四列都只被改一次，条条都该满格"
    mixed = [{"field": "A", "edits": 4, "tickets": 3}, {"field": "B", "edits": 1, "tickets": 1}]
    assert web_src.logic("L.statsBars(b).map(x => x.pct)", b=mixed) == [1, 0.25]
    assert web_src.logic("L.statsBars([])") == [], "空列表要能算（返回空数组），不许抛"
    assert web_src.logic("L.statsBars([{field:'A',edits:0,tickets:0}])[0].pct") == 0
    assert web_src.logic("L.statsBars(null).length") == 0


def test_only_two_field_tags_are_shown_and_the_rest_fold_into_a_count():
    """§5.2 只完整显示前 2 个 + `+n`：原版把全部字段名塞进一格换行，是这一页最难看的那处。
    折叠不许丢信息——全部字段名要留在悬停提示里，人想知道「另外那两个是什么」不用去导 Excel。"""
    four = ["HAWB_NO", "SHIPPER_INFO", "SHIPPER_INFO_COMP_ADDRESS", "SHIPPER_INFO_COUNTRY"]
    assert web_src.logic("L.statsTags(f)", f=four) == {"shown": ["HAWB_NO", "SHIPPER_INFO"], "more": 2}
    assert web_src.logic("L.statsTags(f).more", f=["HAWB_NO", "SHIPPER_INFO"]) == 0, \
        "刚好两个不该挂 +0 徽标"
    assert web_src.logic("L.statsTags(f)", f=[]) == {"shown": [], "more": 0}
    assert web_src.logic("L.statsTags(null)").get("more") == 0
    # 折叠只是把标签收短，容器自己也得禁止换行：允许换行的话，一行照样能占掉三行的高度，
    # 而"行高固定 31px"就是这么被顶穿的——原版那个毛病会原地复活。
    assert re.search(r"\.sttags\{[^}]*flex-wrap:nowrap", _css()), "标签容器没禁止换行"
    assert re.search(r"\.sttags\{[^}]*overflow:hidden", _css()), "标签容器没裁掉溢出，超宽那截会顶开列"
    cell = _fn(_jsCode(), "stTagCell")
    assert 'title="${esc(label + all.length + " 列：" + all.join("、"))}"' in cell, \
        "折叠不等于丢信息：全部字段名要留在 title 里，否则 +n 后面那几列只能去导 Excel 才看得见"


# ── 二、页面骨架：改版不许把已经接好的真控件换成原型里的假按钮 ─────────────────────

def test_the_board_stays_a_window_and_keeps_every_real_control():
    """这一页是浮层窗口（2026-10-09 用户定案），不是原型那种独立整页；日期/分组/刷新/导出/关闭
    都是已经接好后端的真控件。原型工具栏里那几个（假的日期 span、只有三个选项的分组下拉、
    没有 aria 名字的 ✕）一律不许替换掉现有的。"""
    page, js = web_src.part("index.html"), _js()
    for i in ("statsWin", "stBox", "stFrom", "stTo", "stGroup", "stGo", "stCsv", "statsClose"):
        assert 'id="%s"' % i in page, "改版把 %s 这个控件弄丢了" % i
    assert 'id="viewStats"' not in page and 'id="statsCard"' not in page, \
        "统计不再是页签/整页那一节，别趁改版又立回来"
    assert page.count('<option value="">不分组</option>') == 1, "「不分组」那一档只能有一个"
    for ax in ("model", "role", "user", "field"):
        assert 'value="%s"' % ax in page, "分组下拉少了 %s 这一个真轴——四个轴都接在后端 GROUPS 上，掉一个就是砍功能" % ax
    assert 'class="btn sm pri" id="stCsv"' in page, "导出 Excel 是这一页唯一的主操作，要提成主按钮"
    assert 'id="stCnt"' not in page, "口径条已经把这三个数接管了，顶栏那个计数留着就会两处各说一套"
    assert '"/stats"' in js and '"/stats/export"' in js and "renderStats" in js
    assert '$("#btnStats").hidden = !admin' in js, "入口只对管理员可见（与服务端门禁同一口径）"


def test_exactly_the_three_rate_cards_declare_a_progress_bar():
    """前三张是百分比、后三张是计数，进度条只属于百分比那三张（§3.2）。
    这条必须在源码上钉住 KPI 表里的 bar 标记：浏览器实测就是这么发现第一张卡的条子整个没生成的
    ——bar 写成了空串，模板里 `c.bar ? …` 直接判假，蓝条连 DOM 都没进，而六张卡的数值全对，
    光看数字永远发现不了。"""
    js = _js()
    tbl = js[js.index("const ST_KPI = ["):js.index("function statsQS(")]
    rows = [r for r in tbl.splitlines() if re.search(r'\{k: "', r)]
    assert len(rows) == 6, "KPI 表该是六行一行一张卡，读到 %d 行" % len(rows)
    for r in rows[:3]:
        assert re.search(r'bar: "(blue|green|amber)"', r), "百分比卡少了进度条标记：" + r.strip()[:80]
        assert "rate: true" in r, "百分比卡要标 rate，否则单位会被当成「列/张」那种后缀"
    for r in rows[3:]:
        assert re.search(r'bar: ""', r), "计数卡不该有进度条：" + r.strip()[:80]
    assert [m.group(1) for m in re.finditer(r'\{k: "([\w_]+)"', tbl)] == \
        ["acc_field", "acc_after_review", "clean_rate", "edits_doc", "edits_inp", "unreviewed"], \
        "六张卡取的数不是 §3.2 那六个（顺序与口径钉死：换成 edits_total 就把「未复核就发出」这一张悄悄丢了）"
    body = _fn(_jsCode(), "renderStats")
    assert "c.rate ? pctParts(raw)" in body, \
        "百分比卡没走 pctParts：要么把 0.9615 原样摆出来，要么给计数卡也拼上百分号"


def test_the_group_breakdown_survives_the_redesign():
    """原型没有「分组看」这一块，但真实的分组下拉是接好后端的四个轴，删掉它就是趁改版砍功能。
    交接文档 §9 说本次只规定展示口径、后端逻辑不动——那前端也不该动掉一块能用的。"""
    js = _js()
    assert "s.groups" in js or ".groups" in js, "分组结果没人渲染了"
    assert re.search(r"stGroups|分组看", js), "分组那一块要么有容器要么有标题，别只剩数据没人画"


def test_the_broken_scope_warning_is_gated_on_both_identities():
    """§5.8 那条自检要真的接管画面：只看一个等式、或者算了却不用（把 ok 写死成 true），
    口径条就永远不报错——而「聚合值与明细不是同一批票」恰恰是唯一需要当场喊出来的情况。"""
    body = _fn(_jsCode(), "renderStats")
    assert "cl.rowsMatch && cl.colsMatch" in body, \
        "broken 那一档没同时看两条等式：只看一条等于另一条错了也没人响"
    assert 'ok ? "" : " broken"' in body, "口径条的变脸没接到 ok 上，算出自洽问题也没人看得见"


def test_the_detail_row_never_puts_ledger_text_into_html_unescaped():
    """号、人名、字段名都是台账与 users.json 里的字符串，直接拼进 innerHTML 就是自我 XSS
    （test_auth.py 早就为账号表立过同一条）。这条是变异验证补的：把 esc(r.uploader) 外面那层
    esc 摘掉，当时一条用例都不红。"""
    js = _jsCode()
    body = _fn(js, "renderStatsRows") + _fn(js, "stTagCell")
    for bare in ("${r.uploader}", "${r.stager}", "${r.hawb}", "${r.mawb}", "${r.stem}", "${f}"):
        assert bare not in body, "%s 没转义就进模板" % bare
    for wrapped in ("${esc(r.uploader)}", '${esc(r.hawb || "—")}', "${esc(r.stager)}",
                    "${esc(f)}", "${esc(tip)}"):
        assert wrapped in body, "少了 %s：这一格该走 esc" % wrapped
    assert "esc(L.roleCn(" in body, "角色中文名也要转义（那也是 users.json 里的串）"


def test_a_row_with_neither_number_still_falls_back_to_the_ticket_name():
    """真实数据里两个号都可能解析不出来（原型夹具没这一档）。这时整列写「缺号」就认不出是哪张，
    所以退回票名，并把票名与原始文件名留在悬停提示里——找回那张 PDF 全靠它。"""
    js = _jsCode()
    body = _fn(js, "renderStatsRows")
    assert 'r.mawb ? esc(r.mawb) : (r.hawb ? "—" : esc(r.stem))' in body, \
        "退回票名那条链没写全：号缺失时宁可显示票名，也不能整列都是破折号"
    assert "r.model" not in body, "页面上模型那一列早删了（横评看分组轴与 Excel），别趁改版加回来"
    assert "<th>主单号</th><th>分单号</th>" in js, "两个号各占一列：挤在一格里列一窄就断在点号后面"
    assert 'class="tno"' in js, "号列要挂上不许断行的类"


# ── 三、交接文档第 5 节那八条坑点 ────────────────────────────────────────────────

def test_bars_grow_with_scalex_and_stay_visible_when_motion_is_reduced():
    """§5.1 条宽用 transform:scaleX，不要动画化 width：width 每帧触发布局，列多时掉帧。
    另一处原型没写到的坑：它把初始态写成 scaleX(0) + animation forwards，那么
    「减少动态效果」把 animation 关掉之后条子就永久停在 0——看不见的条形图等于没画。
    所以关键帧要自带 from/to，静态态就是满格。"""
    css = _css()
    assert re.search(r"\.stbfill\{[^}]*transform-origin:left", css), "缩放原点不在左边，条子会从中间往两头长"
    grow = css[css.index("@keyframes stGrow"):]
    grow = grow[:grow.index("}")] + "}"
    assert "from{" in grow.replace(" ", "") or "0%{" in grow.replace(" ", ""), \
        "关键帧只写了 to：关掉动画之后条子停在 scaleX(0)，等于整块图空白"
    assert "width" not in grow, "关键帧里在动画化 width，正是 §5.1 点名不要的那条"
    rm = re.search(r"@media \(prefers-reduced-motion:reduce\)\{(.*?)\n\}", css, re.S).group(1)
    assert ".stbfill" in rm, "条形生长没进减少动态效果那一档"


def test_the_entry_animation_plays_once_and_not_on_every_segment_switch():
    """§5.5 首屏动画只在挂载时播一次；§4.3 分段筛选属于内容更新，不播入场。
    做法与今日归档同一个：骨架（口径条/六卡/两栏）与明细 tbody 是两个函数，
    换分段只重画 tbody。合成一个函数的话，每点一次筛选整页就淡入一遍。"""
    js, css = _js(), _css()
    assert "function renderStatsRows(" in js, "明细要能单独重画，否则换分段必然连六张卡一起重建"
    seg = _after(js, '$("#stBox").addEventListener')
    assert "renderStatsRows(" in seg and "renderStats()" not in seg, \
        "分段那一下调了整页重画：六张卡会跟着重播淡入"
    assert re.search(r"\.stkpi[^\n]*\{animation:stFade", css), "六张卡的入场淡入没接上"
    assert not re.search(r"#stRows[^\n]*\{[^\n]*animation", css), \
        "明细行身上挂了入场动画：换一次筛选就闪一遍，正是原型第 5 条坑点"
    delays = re.findall(r"\.stkpi:nth-child\(\d\)\{animation-delay:\.(\d+)s\}", css)
    assert len(delays) == 6, "六张卡要按序步进淡入（交接文档给的是 0.04s 一档）"
    steps = [int(d) for d in delays]
    assert steps == sorted(set(steps)), "步进延迟不是严格递增的：两张卡同时淡入，看着就像一起闪了一下"
    assert len(set(b - a for a, b in zip(steps, steps[1:]))) == 1, \
        "步进不是等距的（交接文档 §4.3 给的是 0.04s 一档，不等距会看着一顿一顿）"
    body = js[js.index("function renderStats("):js.index("function stPaint(")]
    assert re.search(r'c\.bar \? `<div class="sttrack', body), \
        "进度条没按 c.bar 判：后三张计数卡会多出一条空轨道，六张卡的内容就不齐平了"


def test_no_trace_rows_are_weakened_not_removed():
    """§5.3 无留痕行必须弱化、不允许删除：它不进分母但要能备查，靠浅灰字 + 分段降噪，
    而不是从表里藏掉——藏掉之后「可算 4 张」与明细行数对不上，人只会以为页面漏了数据。"""
    css = _css()
    assert re.search(r"tr\.sttrace td\{[^}]*color:var\(--c-text-3\)", css), \
        "无留痕行没有弱化：13 行和 4 行一样黑，正是原版满屏刷屏的那个毛病"
    trace = css[css.index("tr.sttrace"):]
    assert "display:none" not in trace[:120], "把无留痕行直接隐藏了，备查就没了"
    js = _jsCode()
    body = _fn(js, "renderStatsRows")
    assert "L.statsSeg(r)" in body, "行的归属要问 logic，别在渲染里另判一次 legacy——两处判法迟早错位"
    assert 'seg === "trace" ? "sttrace"' in body and '"stchanged"' in body, \
        "三种行底色没按归属分派：弱化与暖底会互相错位"
    assert "notrace" in body, "可比那一格要写清楚是「无留痕」，不是空着让人以为没数"


def test_row_height_has_one_source_and_cells_do_not_carry_it():
    """§5.4 行高固定 31px 且「与今日归档一致」——那就必须共用同一个令牌：
    两页各写一个 31，改一次密度就只改到一页，而这两页是并排看的。
    交接文档同一节还说「单元格不设高度」，原型自己的 CSS 却把 height 写在 td 上（自相矛盾）。
    这里按今日归档那一招做：min-height 给单元格里的内层盒子（.dvci 的同款 .stci）——
    写在 tr 上浏览器根本不认，写在 td 上又会被内容顶开。"""
    css, js = _css(), _js()
    ci = re.search(r"\.stci\{([^}]*)\}", css)
    assert ci, "少了 .stci 这条：行高得有个真正生效的落点"
    assert "min-height:var(--dv-row)" in ci.group(1), \
        "行高没走 --dv-row：与今日归档就不是一套房了"
    assert "display:flex" in ci.group(1) and "align-items:center" in ci.group(1), \
        "内层盒子要顺手把内容垂直居中，否则 31px 的行里字会贴顶"
    td = re.search(r"#stTable tbody td\{([^}]*)\}", css)
    assert td and "height:" not in td.group(1), "高度又写在 td 上了，内容一多就把 31px 顶开"
    assert '"stci"' in js or "stci" in js, "渲染时单元格要套上那个内层盒子，否则行高退回内容撑开"
    assert re.search(r"#stTable\{[^}]*table-layout:fixed", css), \
        "不给 fixed 的话 col 上那些列宽只是建议值，长字段名照样把列撑开"


def test_panels_clip_and_every_number_is_tabular():
    """§5.6 圆角裁剪用 overflow:clip 不用 hidden（hidden 会新建滚动容器，把后续可能的吸顶吃掉）；
    §5.7 百分比/计数/列数全部等宽 + tabular-nums，否则 96.2% 与 100.0% 竖着排就是锯齿。"""
    css = _css()
    for sel in (".stkpi", ".stpanel"):
        m = re.search(re.escape(sel) + r"\{([^}]*)\}", css)
        assert m and "overflow:clip" in m.group(1), "%s 要圆角裁剪就得用 clip" % sel
        assert "overflow:hidden" not in m.group(1)
    for sel in (".stkpi .vl", ".stnum", ".stbar .n", ".stscope b", ".stseg button .c"):
        m = re.search(re.escape(sel) + r"\{([^}]*)\}", css)
        assert m, "少了 %s 这条：数字得等宽" % sel
        assert "var(--mono)" in m.group(1), "%s 没走等宽字体" % sel
    for sel in (".stkpi .vl", ".stnum"):
        m = re.search(re.escape(sel) + r"\{([^}]*)\}", css)
        assert "tabular-nums" in m.group(1), "%s 少了 tabular-nums：同一列数字宽度会随位数抖" % sel
    assert re.search(r"\.stwrap\{[^}]*overflow-x:auto", css), "明细表要能横向滚动，不许把列压扁"


def test_narrow_windows_stack_the_two_columns_instead_of_squeezing_them():
    """§2/§8 断点 ≤1100px：KPI 每行 3 张、左右两栏堆叠、明细表保留横向滚动。
    这一页是浮层窗口，窄窗口下 320px 的左栏会把右栏挤到只剩几百像素——堆叠不是好看，是还能用。

    堆叠之后那一栏必须写 minmax(0,1fr)：grid 里的 1fr 等于 minmax(auto,1fr)，而 auto 的下限是
    内容的最小宽度——明细表带着 min-width:980px，于是这一栏死活不肯窄于 980px，整块从窗口里溢出去
    （浏览器实测：窗口 538px、窗口宽 483px，那一栏却算出 1042px）。结果"横滚"滚的是整扇窗而不是表格：
    六张卡留在左边、明细表在右边，两截对不上。面板自己也要 min-width:0，否则它作为 grid 项同样撑开。"""
    css = _css()
    m = re.search(r"@media \(max-width:1100px\)\{(.*?)\n\}", css, re.S)
    assert m, "没有 1100px 这一档"
    inside = m.group(1)
    assert re.search(r"\.stkpis\{grid-template-columns:repeat\(3,1fr\)\}", inside), "窄窗口下六张卡该折成每行三张"
    assert re.search(r"\.stmain\{grid-template-columns:minmax\(0,1fr\)\}", inside), \
        "堆叠那一栏要写 minmax(0,1fr)：光写 1fr 会被表格的 min-width 顶到 980px，整块溢出窗口"
    assert re.search(r"\.stpanel\{[^}]*min-width:0", css), \
        "面板作为 grid 项也要 min-width:0，否则它自己就按最小内容宽撑开"
    assert re.search(r"\.stmain\{[^}]*minmax\(0,1fr\)", css), "宽窗口那一版右栏同样要能收窄"


def test_semantic_colors_use_the_text_tokens_not_the_fill_tokens():
    """原型把状态色直接当字色用（.kpi.v-green .value{color:var(--green)}、.diff.zero、.bar-row .n），
    而本仓库的 --c-ok/--c-warn/--c-bad 是「看得见的点」（≥3.0 就够），带 -text 的那支才是
    「读得清的字」（≥4.5）。25px 的大数字看着没事，11px 的 ×n 就会糊——所以这里逐条点名。"""
    css = _css()
    for sel, want in ((".stkpi.v-green .vl", "--c-ok-text"), (".stkpi.v-amber .vl", "--c-warn-text"),
                      (".stkpi.v-red .vl", "--c-bad-text"), (".stkpi.v-blue .vl", "--c-accent"),
                      (".stdiff.zero", "--c-ok-text"), (".stdiff.has", "--c-warn-text"),
                      (".stbar .n", "--c-warn-text"), (".sttag", "--c-warn-text")):
        m = re.search(re.escape(sel) + r"\{([^}]*)\}", css)
        assert m, "少了 %s 这条规则" % sel
        assert "color:var(%s)" % want in m.group(1), \
            "%s 该用 %s（状态填充色当字色过不了 AA，老守卫也会拦）" % (sel, want)


def test_the_light_grays_from_the_prototype_are_mapped_to_a_token_that_passes_aa():
    """原型里 #a3abb5（卡片解释语）、#c2c9d1（空值破折号）、#9aa2ab（无留痕行）这些浅灰
    在白底上只有 2.3:1 上下，全不过 AA；本仓库的守卫按相对亮度真算，所以一律映射到 --c-text-3。
    这条用例盯的是「映射之后确实还在用那一档」，别为了还原原型把浅灰又抄回来。"""
    css, js = _css(), _js()
    for sel in (".stkpi .ds", ".stempty", "tr.sttrace td", ".stscope .note", ".stcmp.notrace"):
        m = re.search(re.escape(sel) + r"\{([^}]*)\}", css)
        assert m, "少了 %s 这条规则" % sel
        assert "color:var(--c-text-3)" in m.group(1), "%s 该用 --c-text-3：原型那个浅灰不过 AA" % sel
    assert "stempty" in js and "—" in js, "空值要统一渲染成灰色破折号，不是留白"


def test_widths_and_delays_are_written_from_js_because_inline_style_is_banned():
    """进度条宽度、条形宽度与生长延迟都是「值当场算出来」的数据，但本仓库那条守卫只放行
    `style="left:` 与 `<col style="width:`两种内联写法——所以百分比一律由 JS 写回元素，
    列宽一律走 col 元素。原型里 style="width:96.2%" 那种写法在这儿过不了关。"""
    js = _js()
    assert '<col style="width:' in js, "明细表列宽要走 col 元素（守卫只放行这一种内联宽度）"
    assert not re.search(r"<th[^>]*style=", js), "th 上又写内联宽度了，那一条守卫会拦"
    assert re.search(r"\.style\.width\s*=", js), "进度条/条形的宽度要由 JS 写回，不是模板里的内联值"
    paint = _fn(js, "stPaint")
    assert re.search(r"fills\[fi\]\.style\.width\s*=", paint), \
        "KPI 那三条进度条的宽度没人写回：六张卡的数字都对、条子全是空的，光看数看不出来"
    assert re.search(r"bf\[i\]\.style\.width\s*=", paint), \
        "条形图的比例宽度没人写回：每根都会顶到满格，条形图当场失去意义"
    assert re.search(r"bf\[i\]\.style\.animationDelay\s*=", paint), "生长延迟要按序号由 JS 写回"
    assert not re.search(r'style="visibility', js), "占位用的 visibility:hidden 该收成一个类"


def test_the_render_never_recomputes_the_server_arithmetic():
    """准确率只有 stats.py 一处算（test_stats.py 也钉着）。改版最容易顺手犯的错是
    在前端把六张卡的百分比重算一遍、或把 by_field 的 edits 加起来当合计——两边迟早对不上。
    前端只许格式化（pct）与数行（logic.js 里那三个纯函数）。"""
    js = _js()
    body = _fn(js, "renderStats") + _fn(js, "renderStatsRows")
    for banned in ("reduce(", "edits_total +", "acc_field *", "1 - ", ".filter(r =>"):
        assert banned not in body, "渲染里出现了 %r：这是在重算服务器给的数" % banned
    assert "L.statsCounts(" in body and "L.statsFilter(" in body, "计数与筛选要走 logic，别在渲染里再写一遍"
    assert "L.statsClosure(" in body, "§5.8 那两条闭环等式要真的比一遍，不是定义了没人调"
    assert "L.statsBars(" in body, "条形比例要走 logic，别在渲染里又除一遍"
    assert "L.statsTags(" in js and ".slice(0,2)" not in js.replace(" ", ""), \
        "字段标签的折叠要走 logic：渲染里自己 slice 就等于第二份口径"


def test_the_scope_bar_says_out_loud_that_no_trace_tickets_are_not_in_the_denominator():
    """§3.1 必须在口径条显式说明「无留痕单据不进入准确率分母」。
    这一句是整页最容易被当成装饰删掉的文字，而删掉之后「可算 4 张」会被读成「一共只有 4 张票」。"""
    js = _jsCode()
    body = _fn(js, "renderStats")
    assert "stScope" in body, "口径条没有容器"
    for word in ("可算", "可比", "无留痕", "分母"):
        assert word in body, "口径条少了「%s」这几个字" % word
