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


def test_both_views_put_their_action_bar_in_the_same_place():
    """主单与分单的按钮要在同一个地方（2026-10-13 用户：不要分单的在下面、主单的在上面）。

    统一到**底部常驻**那一条：分单的操作条本来就是 `position:sticky;bottom:0`（核到最后一格
    也够得着提交），主单那条从前贴在 36 列表的**上面**——一进页看得见，滚下去改完就找不着了。
    反过来把分单挪到上面要再叠一层 sticky，与待办条抢同一道让位高度（`--todoH` 已经为表头
    踩过三次），所以不那么做。"""
    js, css = web_src.part("js/desk.js"), web_src.css()
    assert 'class="ft top"' not in js, "两张表两种位置，正是这次要修掉的毛病"
    assert ".ft.top{" not in css, "上面那条的样式没人用了就该跟着删掉"
    rm = web_src.fn(js, "function renderMain(")
    assert rm.count('class="ft bottom"') == 2, f"两张表各一条底部操作条：{rm.count('class=\"ft bottom\"')}"
    mv = rm[:rm.index("  if (!t){")]
    assert mv.index('<table>') < mv.index('class="ft bottom"'), \
        "主单那条要在 36 列表之后：sticky bottom 靠的是「它在滚动内容末尾」才钉得住"


def test_a_queue_row_can_be_removed_and_stops_its_job():
    """列队里那张不想等的要能当场删掉（2026-10-13 用户）。"删"是两件事：从这一页的列表里
    拿掉，**并且**告诉服务器别再为它花一次模型调用。已经在跑的那一张服务器掐不掉，
    所以那一句要如实说"结果会丢掉"，不能写成"已取消"。"""
    js = web_src.part("js/desk.js")
    fn = web_src.fn(js, "async function dropRow(")
    assert "cancelJob(" in fn, "删掉在跑的那张要通知服务端取消，不然模型白烧一次"
    assert "/cancel" in web_src.fn(js, "async function cancelJob("), "取消走的是那一发 POST /job/{id}/cancel"
    assert "t.job" in fn, "删的是这一张的作业：轮询要能停，结果不许再落回列表"
    assert "dropped" in web_src.fn(js, "async function waitJob("), \
        "移除后要退出轮询，否则那张票的 poller 会一直敲到 30 分钟上限"
    assert "data-drop=" in web_src.fn(js, "function render("), "行上要有那颗删除"
    ex = web_src.fn(js, "async function extract(")
    assert "t.dropped" in ex and "cancelJob(" in ex, \
        "删在「上传已发出、作业号还没回来」那几十毫秒里要补一发取消，别留一张没人等的孤儿解析"


def test_a_refresh_puts_the_day_back_on_the_table():
    """刷新页面不该把一桌子的票变没（2026-10-15 用户：刷新页面啥都没了，能保持住当前信息吗）。
    这一屏本来就是「本人今天在服务器上动过的票」，服务器知道，那就问服务器要回来；
    完整那份数据只在点开时取——一开页面就把每张票的三个 JSON 都读一遍是没必要的重活。"""
    js = web_src.part("js/desk.js")
    rq = web_src.fn(js, "async function restoreQueue(")
    assert '"/day"' in rq, "要问服务器今天本人动过哪些票，不靠浏览器自己记那份名单"
    assert ".concat(keep)" in rq, \
        "补桌那几百毫秒里人可能已经点开过草稿或丢上一张：那几张要留着，整张表盖掉就是把人刚做的事擦了"
    assert "kind" in rq, "主单的行不进分单队列：那张表里两种行混在一起"
    assert "restoreQueue()" in web_src.fn(js, "function startDesk("), "开门就补桌"
    assert "localStorage.setItem(LS.open" in js, "离开时停在哪一张要记住，回来还停在那一张"
    assert "seed" in web_src.fn(js, "function statusOf("), "只种了号与状态的那一行也要说得出它到哪一步了"
    nr = web_src.fn(js, "function needsReview(")
    assert "seed.submitted" in nr and "submitted.mode" in nr, \
        "种子行与已载入的行要同一个判据：同一张票点前点后「需复核 N」不许跟着变"
    assert "filter(needsReview)" in web_src.fn(js, "function render("), \
        "顶上那个计数与「跳下一票」必须同一个判据，两处各数一遍迟早给两个数"
    assert 't.state === "seed"' in web_src.fn(js, "function renderMain("), \
        "选中种子行要先取完整那份：拿一份空 air 去画 38 行，看着就像解析坏了"
    op = web_src.fn(js, "async function openHouse(")
    assert "S.tickets.indexOf(hit)" in op, "种子行点开要就地换成完整那份，不能多出一行重复的票"


def test_staging_or_submitting_refreshes_what_the_server_counts():
    """点完暂存或提交，台账与那张「名下分单」表必须跟着回账（2026-10-15 用户：这里一定得更新，
    不然都不知道下一票处理哪个了）。那两处读的都是服务器，所以只能再问一次服务器，
    拿浏览器里那份状态糊上去只会糊出一屏假账。"""
    js = web_src.part("js/desk.js")
    aw = web_src.fn(js, "function afterWrite(")
    assert 'if (VIEW.cur === "day") loadDay(false);' in aw, \
        "台账那一屏要按服务器重画：判据整行钉住（只认 loadDay 那几个字，把条件改成永不成立也算过）"
    assert "mstRefresh()" in aw and "if (sent) gotoNextTicket();" in aw, \
        "名下分单那张表按号重取；只有真交出去的那一次才把人带去下一票"
    assert "afterWrite(" in web_src.fn(js, "async function stageTicket("), "暂存完要回账"
    sub = web_src.fn(js, "async function submitTicket(")
    assert "afterWrite(t, true)" in sub, "提交完要回账，第二参 true 说的就是「这一张交出去了，带人去下一票」"
    g = web_src.fn(js, "function gotoNextTicket(")
    assert "needsReview" in g and 'toast("' in g, "没有下一票时要说一句，不能默默停在原地"
    assert "afterWrite(" in web_src.fn(js, "async function mvSubmit("), "主单那条链同样要回账"


def test_no_top_level_function_is_declared_twice():
    """desk.js 里两个同名 function，后写的静默盖掉前一个：调用方拿到的是另一个函数、
    参数还不一样，浏览器里才炸（2026-10-15 新加的「跳下一票」就撞在 gotoNextTodo 上）。
    回归全绿也照样是坏的，所以这条只认一件事：顶格名字不许出现两次。"""
    js = web_src.part("js/desk.js")
    names = re.findall(r"^(?:async )?function ([A-Za-z0-9_]+)", js, re.M)
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"顶格重名，后一份会盖掉前一份：{dup}"


# ── 队列只放「还没做完的票」（2026-10-18 用户报的：拿掉的刷新又回来、已提交的还在队列里）──


def test_a_removed_ticket_does_not_come_back_on_refresh():
    """「从列表移除」从前只改内存里那个数组：刷新时 restoreQueue 照 /day 重新派生一遍，
    拿掉的原样回来。现在落一份本机记忆，补桌时跳过——只记今天，/day 本来就是按天的，
    跨天自然失效（第二天重新动过它就该回来）。"""
    js = web_src.part("js/desk.js")
    rq = web_src.fn(js, "async function restoreQueue(")
    assert "L.droppedToday(droppedRaw(), dayKey())" in rq, \
        "补桌时没看「今天拿掉过谁」那份记忆：拿掉的刷新就回来"
    assert "&& !x.submitted && gone.indexOf(x.stem) < 0);" in rq, \
        "补桌的筛子要同时排掉「已提交」与「今天拿掉过」这两档——判据整行钉住，少一样就漏一档"
    lv = web_src.fn(js, "function leaveQueue(")
    assert "if (remember) rememberDropped(t.stem);" in lv, \
        "拿掉那一下没记下来（整行钉住：只有人点的那一路才记，提交那一路不许记）"
    assert "localStorage.setItem(LS.dropped" in web_src.fn(js, "function rememberDropped("), \
        "记忆要落本机（服务器不知道人拿掉过谁），键用 LS.dropped"
    ls = re.search(r"const LS = \{.*?\};", js, re.S)
    assert ls and "dropped:" in ls.group(0), "LS 表里没登记 dropped 这个键"
    assert "forgetDropped(" in web_src.fn(js, "async function openHouse("), \
        "从今日台账主动点开一张拿掉过的票，得把它从记忆里摘掉——那是人主动要它回来"


def test_a_submitted_ticket_leaves_the_queue():
    """交出去＝这票做完了（用户：「我都已经提交了是不是应该从列队里自动删除，证明已经完成
    这票业务了」）。判据用服务器那份 /day 的 submitted，不靠本机记忆——换台机器登录、
    换个浏览器都一样。"""
    js = web_src.part("js/desk.js")
    rq = web_src.fn(js, "async function restoreQueue(")
    assert "x.submitted" in rq, "补桌没跳过已提交的行：今天交出去的票刷新后又回队列"
    sub = web_src.fn(js, "async function submitTicket(")
    assert "leaveQueue(t, false)" in sub, \
        "提交成功没当场把它从队列拿掉；第二参 false 说的是不记本机记忆——已提交由服务器那份判"
    assert "leaveQueue(t, false)" in sub.split("afterWrite(")[0], \
        "拿掉要排在回账之前：afterWrite 里那颗「跳下一票」否则会从刚交出去那张身上往下找"
    assert sub.count("leaveQueue(") == 1, \
        "只有成功那一条尾巴才许拿掉：失败、接口没开通那两条路票还在人手上"


def test_removing_a_ticket_takes_its_unsaved_edits_with_it():
    """改过值的票拿掉时，本机那份未保存的编辑要一起撤：留着就成了「队列里没了、未保存里还在」，
    两个列表各说各话（用户报的「他会跳转到未保存里」正是这个——renderDrafts 只藏队列里那些
    票的草稿，票一离开队列那份草稿就露头）。撤了就没了，所以先问一句。"""
    js = web_src.part("js/desk.js")
    one = web_src.fn(js, "function wireMain(")
    assert "confirmDropEdits(t)" in one and "dropDraft(t.stem)" in one, \
        "「从列表移除」没把本机那份编辑一起撤掉"
    row = web_src.fn(js, "async function dropRow(")
    assert "confirmDropEdits(t)" in row and "dropDraft(t.stem)" in row, \
        "行上那颗 × 与操作条那颗走同一条口径"
    c = web_src.fn(js, "function confirmDropEdits(")
    assert "draftFlush()" in c, "判据要等于盘上那份：待写的那份先落盘再读（与 renderDrafts 同一手）"
    assert "confirm(" in c and "return true" in c, \
        "有本机编辑要先问一句；没有就静默拿掉，别多问"


def test_the_dropped_memory_only_lasts_the_day():
    """「今天拿掉过谁」只认今天：/day 本来就是按天的，跨天自然失效。写坏了的那份一律当空——
    一份坏数据不许把整桌票挡在门外。"""
    assert web_src.logic("L.droppedToday({A:'2026-10-18',B:'2026-10-17'}, '2026-10-18')") == ["A"]
    assert web_src.logic("L.droppedToday({}, '2026-10-18')") == []
    assert web_src.logic("L.droppedToday(null, '2026-10-18')") == []
    assert web_src.logic("L.droppedToday(bad, '2026-10-18')", bad="坏数据") == []
    assert web_src.logic("L.droppedToday({A:'2026-10-18'}, '')") == [], \
        "没有今天这个键时什么都不算拿掉过"

