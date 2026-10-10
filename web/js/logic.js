"use strict";
/* 审核台的纯逻辑：不碰 DOM、不发请求，喂进去什么就该吐出什么。
   浏览器里 desk.js 直接读这个全局 L（index.html 把它排在 desk.js 前面）；
   node 靠最后一行 module.exports 读同一份文件做单测（tests/test_web_logic.py），
   这样"草稿会不会被新解析盖掉"这种算式是被人验证过的，而不是看着像对。 */
const L = {
  str(o, k){ const v = o ? o[k] : null; return v === null || v === undefined ? "" : String(v); },

  /* 账号名、单号、错误 message 都是外部字符串，拼进 innerHTML 前只能过这里。
     单引号也要转：属性值里藏一个 ' 就能把一段文本变成一段事件处理器。 */
  esc(s){ return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); },
  /* 角色名是给登录者看的身份提示：说错了比不说更糟（录入员被写成"制单员"会以为自己没登进去）。
     后端将来加角色，宁可原样显示，也别替他猜一个别人的身份。 */
  roleCn(role){ return ({admin:"管理员", reviewer:"制单员", inputter:"录入员"})[role] || String(role); },

  /* 轮询的下一间隔：第一次就退避会把正常速度的结果也拖慢，所以第 1 次就是基础值；
     之后越等越稀疏（解析慢的时候按秒敲服务器没有意义），但要有上限。 */
  pollDelay(tries, every, cap){
    return Math.min(Math.round(every * Math.pow(1.6, Math.max(0, (tries | 0) - 1))), cap);
  },

  /* 今日台账的报数口径：pending 是"还没回公司"的张数（含解析失败的），
     制单员每天说的"还剩几张没发"就是这个数，别把它算成 total-submitted-failed。 */
  /* 台账的数。作废的行不进 N/M/K/F —— 清完测试数据数就该跟着变，否则那个数永远是被污染的；
     作废的张数单独报一个 voided，给「只看作废」那颗开关用。
     筛与算都只在这一处：渲染里再判一遍 truthy，迟早和这里对不上（这条口径有用例钉着）。 */
  daySummary(rows){
    const all = rows || [];
    const rs = all.filter(r => !r.voided);
    let submitted = 0, failed = 0, flags = 0;
    rs.forEach(r => {
      if (r.submitted) submitted++;
      if (r.failed) failed++;
      flags += r.flags || 0;
    });
    return {total: rs.length, submitted: submitted, pending: rs.length - submitted,
            failed: failed, flags: flags, voided: all.length - rs.length};
  },

  dayVisible(rows, onlyVoided){
    return (rows || []).filter(r => !!r.voided === !!onlyVoided);
  },

  /* ── 今日归档（高密度台账）的算法 ─────────────────────────────────────────
     后端 /day 给的是"分单行 + 主单行按 processed_at 合流"，**并不保证主单紧挨着自己的分单**
     （交接文档第 3 节写的"后端保证连续"在 store.day_rows 上不成立：它是两个列表拼完再按时间排），
     所以聚簇必须由前端算。算式放这里由 node 断输入输出——渲染里再算一遍，迟早和这里对不上。 */

  /* 号归一：999-95764373 与 99995764373 是同一张主单的两种写法（模型抄的与人填的），
     不归一就会分成两组、折叠也只折得动一半。非字母数字一律丢掉，字母统一大写。 */
  normNo(v){
    return String(v === null || v === undefined ? "" : v).replace(/[^0-9A-Za-z]/g, "").toUpperCase();
  },
  /* 这一行属于哪一组。没取到主单号的分单**自己一组**（用文件 stem 当键）：并进上一组
     等于替票面说了一句它没说的话——"这张票属于那张主单"。stem 是归档键，天然唯一，
     所以键在筛选/排序之后仍然稳定（折叠态才不会因为换了个筛就认错组）。 */
  dayKey(r){ return L.normNo(r && r.mawb) || ("~" + L.str(r, "stem")); },

  /* 组内主单在前、名下分单随后；组的先后按这一天第一次出现的时间。
     不改传进来的那份数组——它还是"后端原始顺序"，切回别的排序要用。 */
  dayCluster(rows){
    const groups = {}, order = [], out = [];
    (rows || []).forEach(r => {
      const k = L.dayKey(r);
      if (!Object.prototype.hasOwnProperty.call(groups, k)){ groups[k] = {m: [], s: []}; order.push(k); }
      groups[k][r.kind === "mawb" ? "m" : "s"].push(r);
    });
    order.forEach(k => { out.push.apply(out, groups[k].m); out.push.apply(out, groups[k].s); });
    return out;
  },
  /* 每张主单名下有几条分单：0 的那一组箭头是占位的（点不动），N>0 才在折叠后报"分单 N 已折叠"。
     没有主单号的分单不进这张表——冒出一个空键的组，等于界面上多一组折不起来的东西。 */
  daySubCounts(rows){
    const all = rows || [], c = {};
    all.forEach(r => { if (r.kind === "mawb"){ const k = L.normNo(r.mawb); if (k) c[k] = 0; } });
    all.forEach(r => { if (r.kind !== "mawb"){ const k = L.normNo(r.mawb); if (k) c[k] = (c[k] || 0) + 1; } });
    return c;
  },

  /* 四档筛选。认不出的档退回"全部"：退回空表的话，人会以为这一天真的没票。
     「已提交」是**所有**提交过的行（主单与分单都算）——原型那版只留主单，
     制单员点进去就看不见自己刚发出去的分单，等于这一页在骗他。 */
  dayFilter(rows, f){
    const all = rows || [];
    if (f === "unsent") return all.filter(r => !r.submitted);
    if (f === "flag") return all.filter(r => (r.flags || 0) > 0);
    if (f === "done") return all.filter(r => !!r.submitted);
    return all.slice();
  },
  /* 红旗优先：红旗多的在前，一样多时未提交的在前，再一样就保持原顺序（稳定排序——
     同一天连点两次不该看着像重新洗了牌）。下标那一项是显式的稳定性保底。 */
  dayFlagFirst(rows){
    return (rows || []).map((r, i) => ({r: r, i: i}))
      .sort((a, b) => ((b.r.flags || 0) - (a.r.flags || 0))
        || (a.r.submitted === b.r.submitted ? 0 : (a.r.submitted ? 1 : -1))
        || (a.i - b.i))
      .map(x => x.r);
  },
  dayOrder(rows, mode){ return mode === "flag" ? L.dayFlagFirst(rows) : L.dayCluster(rows); },

  /* 控制条那四个数。红旗是"有红旗的**票数**"，不是红旗标记总数（交接文档第 3 节点名了这一条）；
     总数/未提交/失败/作废复用 daySummary，不另算一套——同一套算术写两遍迟早给出两个数。 */
  dayBar(rows){
    const s = L.daySummary(rows), live = (rows || []).filter(r => !r.voided);
    return {total: s.total,
            masters: live.filter(r => r.kind === "mawb").length,
            flagged: live.filter(r => (r.flags || 0) > 0).length,
            pending: s.pending, failed: s.failed, flags: s.flags, voided: s.voided};
  },

  /* 提交记录那一格只有 240px：整串 2026-10-10T09:20:00 等宽排下来会把操作人姓名挤掉。
     同一年里月日时分足够认票，秒与年留在悬停提示里。认不出的形状原样返回（"mock" 这种
     测试值不该被吃掉），空的还是空的。 */
  shortTime(v){
    const s = String(v === null || v === undefined ? "" : v).trim();
    const m = /^\d{4}-(\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(s);
    return m ? (m[1] + " " + m[2]) : s;
  },

  /* ── 解析正确率看板：分段归属 / 口径闭环 / 条形比例 / 字段标签折叠 ────────────────
     准确率本身只有 stats.py 一处算（前端连百分号都不许自己拼一遍），这里只做四件"画法"上的事：
     一行属于哪一档、四档各几行、后端那份聚合值与这份明细是不是同一批票、条子该多长、
     字段名显示几个。全是纯函数，node 直接断输入输出。 */
  statsSeg(r){
    const x = r || {};
    // legacy 行的 total 是 null 不是 0：当成「改动 0」会把历史票算成一次通过，
    // 而一次通过率正是拿去回答"能不能不经过人直接对接"的那个数。
    if (x.legacy) return "trace";
    return (x.total || 0) > 0 ? "changed" : "clean";
  },
  statsCounts(rows){
    const all = rows || [], c = {all: all.length, changed: 0, clean: 0, trace: 0};
    all.forEach(r => { c[L.statsSeg(r)] += 1; });
    return c;
  },
  /* 认不出的档退回"全部"：退回空表的话，人会以为这个窗口内真的一张票都没有。 */
  statsFilter(rows, f){
    const all = rows || [];
    if (f !== "changed" && f !== "clean" && f !== "trace") return all.slice();
    return all.filter(r => L.statsSeg(r) === f);
  },
  /* 两条恒等式（交接文档 §5.8）：可算 + 无留痕 = 明细行数；各票可比之和 = 顶部可比列数。
     这不是重算准确率，是自检"后端给的聚合值与这份明细是不是同一批票"——时间窗一边含端点
     一边不含就会悄悄错开；对不上还照画，画出来的百分比全都不能用。 */
  statsClosure(summary, rows){
    const s = summary || {}, all = rows || [];
    let cols = 0;
    all.forEach(r => { cols += (r.comparable || 0); });
    return {rowsMatch: (s.tickets || 0) + (s.legacy || 0) === all.length,
            colsMatch: cols === (s.fields_comparable || 0)};
  },
  /* 条长 = 被改次数 ÷ 最大次数（等次数时满格）。最大为 0 就给 0：除出 NaN 写进宽度的话，
     浏览器静默当没这条声明，条子反而停在满格——看着像"这一列改得最多"。 */
  statsBars(byField){
    const list = byField || [];
    let max = 0;
    list.forEach(e => { if ((e.edits || 0) > max) max = e.edits || 0; });
    return list.map(e => ({field: e.field, edits: e.edits || 0, tickets: e.tickets || 0,
                           pct: max ? (e.edits || 0) / max : 0}));
  },
  /* 字段标签只完整显示前 2 个，其余收 +n：把全部字段名塞进一格换行正是原版最难看的那处。
     折叠不等于丢——调用方要把整个列表放进 title，人想知道另外那几个是什么不必去导 Excel。 */
  statsTags(fields, keep){
    const all = fields || [], k = keep === undefined ? 2 : keep;
    return {shown: all.slice(0, k), more: all.length > k ? all.length - k : 0};
  },

  /* 打开主单 / 重新解析之后，把本机草稿并回当前解析。
     人改过的列照单恢复；只有"草稿当时的原值 ≠ 现在解析出的原值"的列才算打架——
     底层解析动了，人对着旧值改的那一格可能已经不对，要点名复核。
     editBase 不能被新解析覆盖：覆盖了下次刷新就再也说不出"这一列曾经动过"。 */
  draftMerge(draft, ams){
    const cur = ams || {};
    if (!draft) return {edit:{}, acked:[], extraFlags:[], editBase:Object.assign({}, cur), conflicts:[]};
    const edit = Object.assign({}, draft.edit || {}), base = draft.base || {};
    return {edit: edit,
            acked: (draft.acked || []).slice(),
            extraFlags: (draft.extraFlags || []).slice(),
            editBase: Object.assign({}, base),
            conflicts: Object.keys(edit).filter(k => L.str(base, k) !== L.str(cur, k))};
  },

  /* 打开别人（或自己在别的机器）暂存过的票时怎么合并：服务器那份赢，
     本地这份没存过的改动不删，但冲突的字段必须点名报给人。
     静悄悄盖任何一边都坏——盖掉服务器，同事以为你看的是他核对过的值；
     盖掉本地，人刚填的两格凭空消失又不知道为什么。
     「服务器改了哪几列」只认服务端算好的那份 edits（store.field_diff 对数字列做了归一，
     45.0 与 45 不算改动）；前端自己再比一遍就会把这些假改动摆到人面前。
     出来的值一律转成字符串：核对页整条值管道按字符串处理，数字塞进 airE 会在渲染时抛异常。 */
  stagedMerge(final, localDiff, serverEdits){
    const f = final || {}, l = localDiff || {}, ed = serverEdits || {};
    const has = Object.prototype.hasOwnProperty;
    const airE = {}, conflicts = [], kept = {};
    const seen = {}, keys = [];
    Object.keys(ed).concat(Object.keys(l)).forEach(k => {
      if (seen[k]) return; seen[k] = 1; keys.push(k);
    });
    keys.forEach(k => {
      const sv = has.call(ed, k) && has.call(f, k) ? L.str(f, k) : null;
      const lv = has.call(l, k) ? L.str(l, k) : null;
      if (sv !== null){
        airE[k] = sv;
        if (lv !== null && lv !== sv){ conflicts.push(k); kept[k] = l[k]; }
      } else if (lv !== null){ airE[k] = lv; }
    });
    return {airE: airE, conflicts: conflicts, local: kept};
  },

  /* 人改了一列：值与当前解析一致就不算改动；基准挪到当前解析，这一列的打架随之消解
     （他已经对着新值看过了，再报一次只会让人学会忽略提示）。 */
  draftTouch(edit, editBase, conflicts, k, value, orig){
    const o = orig === null || orig === undefined ? "" : String(orig);
    const e = Object.assign({}, edit), b = Object.assign({}, editBase);
    if ((value || "") === o) delete e[k]; else e[k] = value;
    b[k] = o;
    return {edit: e, editBase: b, conflicts: (conflicts || []).filter(x => x !== k)};
  },

  /* 三样全空 → null（不落条目）：挂着一条空草稿，"本机有未提交改动"就一直亮着，
     很快没人信这个状态。 */
  draftEntry(editBase, edit, acked, extraFlags, savedAt){
    if (!Object.keys(edit || {}).length && !(acked || []).length && !(extraFlags || []).length) return null;
    return {savedAt: savedAt, base: editBase || {}, edit: edit || {},
            acked: (acked || []).slice(), extraFlags: (extraFlags || []).slice()};
  },

  /* 读出来的快照从不就地改：写回时又被自己读回来，"提交成功清草稿"就清了个寂寞。 */
  draftPut(all, mawb, entry){
    const out = Object.assign({}, all || {});
    if (entry) out[mawb] = entry; else delete out[mawb];
    return out;
  },

  /* ── 票面浏览的缩放档位 ────────────────────────────────────────────────
     内嵌阅读器只吃地址参数（view=FitH / view=Fit / zoom=数字），所以"下一档是什么"得我们自己算。
     坑在于：整页 = min(按宽, 按高)，票面栏比票更高时按宽就是那个最小值，FitH 与 Fit 算出同一个
     缩放——按钮只剩标签在换，画面不动（用户报"点了没变化"就是这个）。所以重合的档位要丢掉，
     并补一档真的更大的。页面尺寸从已在手的 PDF 字节里扫 MediaBox，不为此再发一次请求。 */
  pdfBox(buf){
    if (!buf || !buf.length) return null;
    let s = "";
    const n = Math.min(buf.length, 200000);        // MediaBox 在文件前部，扫 200KB 足够
    for (let i = 0; i < n; i++) s += String.fromCharCode(buf[i]);
    const at = s.indexOf("/MediaBox");
    if (at < 0) return null;
    const m = /\[\s*[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)\s*\]/.exec(s.slice(at, at + 80));
    if (!m) return null;
    const w = parseFloat(m[1]), h = parseFloat(m[2]);
    return w > 0 && h > 0 ? {w: w, h: h} : null;
  },

  /* 返回按缩放由小到大的档位；每档 {k, name, frag, zoom}，frag 直接拼进 iframe 地址。
     算不出尺寸时退回浏览器自己的两态（不比今天差）；只剩一档说明这票在这栏里没法再分档。
     栏位要按"阅读器留给页面的净尺寸"算：Chrome 的 FitH 实测比 栏宽/页宽 保守（它自己还要留
     滚动条与页面边距），照栏宽算会把两档误判成不同、又留下一颗点了不动的死按钮。
     2026-10-02 在 655×707 的 iframe 上实测：FitH 给 80%、Fit 给 58%。 */
  viewCycle(page, pane, pad){
    const PRI = [
      {k:"fith", name:"适应宽度", frag:"view=FitH", rank:0},
      {k:"fit", name:"整页看全", frag:"view=Fit", rank:1},
      {k:"detail", name:"放大看细节", frag:null, rank:2},
    ];
    if (!page || !pane || !pane.w || !pane.h) return PRI.slice(0, 2).map(s => Object.assign({}, s));
    const p = pad || {w: 60, h: 56};                // 阅读器自己吃掉的宽/高
    const cw = Math.max(1, pane.w - p.w), ch = Math.max(1, pane.h - p.h);
    const fitW = 100 * cw / page.w, fitP = Math.min(fitW, 100 * ch / page.h);
    const out = [{s: PRI[0], zoom: Math.round(fitW)}, {s: PRI[1], zoom: Math.round(fitP)}];
    const detail = Math.min(200, Math.max(100, Math.round(fitW * 1.6)));
    if (detail > Math.round(fitW) * 1.1) out.push({s: PRI[2], zoom: detail});   // 不比适应宽度大就不算一档
    out.sort((a, b) => a.zoom - b.zoom);
    const kept = [];
    for (const o of out) {
      const last = kept[kept.length - 1];
      // 差不到 10% 就是同一档（阅读器还会把缩放吸附到它自己那串档位上）：留下更该出现的那个
      if (last && Math.abs(o.zoom - last.zoom) / last.zoom < 0.1) {
        if (o.s.rank < last.s.rank) kept[kept.length - 1] = o;
        continue;
      }
      kept.push(o);
    }
    return kept.map(o => Object.assign({}, o.s, {zoom: o.zoom,
      frag: o.s.k === "detail" ? "zoom=" + o.zoom : o.s.frag}));
  },

  nextView(cycle, k){
    if (!cycle || !cycle.length) return null;
    let i = -1;
    for (let n = 0; n < cycle.length; n++) if (cycle[n].k === k) i = n;
    return cycle[(i + 1) % cycle.length];            // 没找到（i=-1）就从第一档重新开始
  },

  /* 打开票面停在哪一档：适应宽度——窄栏里它是能看清字的那档，整页会把票缩成一小块。
     这一档被丢掉时（与整页撞车）就退回最小的那档，别返回空。 */
  firstView(cycle){
    if (!cycle || !cycle.length) return null;
    for (const s of cycle) if (s.k === "fith") return s;
    return cycle[0];
  },

  /* ── 页图模式（票面不再嵌浏览器阅读器）的算式 ─────────────────────────────
     图宽按"占栏位宽的百分数"给：100% 就是适应宽度。 */
  wholePagePct(pageW, pageH, paneW, paneH){
    if (!(pageW > 0 && pageH > 0 && paneW > 0 && paneH > 0)) return 100;
    return Math.min(100, Math.round(100 * paneH * pageW / (paneW * pageH)));
  },
  /* 放大到超过渲染出来的像素只是把糊图撑大，还多占滚动条：上限就是原生像素 */
  maxZoomPct(pageW, paneW){
    if (!(pageW > 0 && paneW > 0)) return 100;
    return Math.max(100, Math.round(100 * pageW / paneW));
  },
  /* 按住拖动看别处只在内容真超出栏位时才给抓手：给了却拖不动，比不给更让人以为自己点坏了。
     宽、高分开问。 */
  canPan(content, pane){ return (content || 0) - (pane || 0) >= 1; },

  /* 格子的自定义尺寸（2026-10-09 用户三样都要：拖过的高度要算数 + 两道把手）。
     长文本格一直有原生把手，但 autoGrow 每下输入都按内容重设高度，拖完一打字就弹回去——
     "能拖"在用户那儿等于"不能自定义"，所以记过的那一列必须以人拖的为准。
     上下限都得有：拖成 6px 是看不见，拖成 4000px（或本机存进个坏值）是一屏放不下别的字段。 */
  fieldHeight(recorded, contentH){
    const r = Math.round(Number(recorded));
    if (r > 0) return Math.min(900, Math.max(28, r));
    return Math.min(320, Math.max(28, Math.round(Number(contentH) || 28)));
  },
  /* 分栏那道把手存的是"整行宽度的百分数"，不是比值也不是像素：CSS 那边要写进 minmax()，
     而 Chrome 不接受 calc(var(--x) * 1fr)——整条 grid-template-columns 会被判非法、掉回自动布局，
     把手那一轨直接缩成 0px（2026-10-09 实测）。百分数在 minmax 里是好的，窗口一缩放两侧
     保持同样的视觉份额；两侧各留 260px 底线——把票面挤成一条缝、或把字段栏挤成一列竖字，
     都不叫自定义。 */
  splitPctAfter(px, avail, min){
    if (!(avail > 0)) return 50;
    const lo = min > 0 ? min : 260;
    const v = Math.min(avail - lo, Math.max(lo, px));
    return Math.round(v / avail * 1000) / 10;
  },
  colWidthAfter(px, lo, hi){
    const v = Math.round(Number(px));
    if (!(v > 0)) return lo;
    return Math.min(hi, Math.max(lo, v));
  },
  /* 值格右边缘那道把手：人往右拉是想要框更宽，而框的宽就是值这一列的宽——这一列没有自己的
     刻度，它是"整行减去票面栏"剩下的，所以宽度只能从票面栏那头扣：pullRight>0 ⇒ 票面份额变小。
     方向写反就是"想拉宽结果更窄"，这条最容易错，所以单独一个函数、单独一条用例。
     底线沿用分栏那道把手的（同一个偏好、两个入口，界限必须一致，否则一边能把另一边拖不进的位置停住）。 */
  boxWidthPct(viewW, pullRight, avail, min){
    return L.splitPctAfter((Number(viewW) || 0) - (Number(pullRight) || 0), avail, min);
  },

  /* 点字段定位后放多大：各人的手感不一样（2026-10-09 用户要的选择权）——
     auto 按命中行高算（约占窗格 1/3），fixed 用人填的，off 干脆不动。
     三条边界都得管：都不许超过页图真实像素（超过只是把糊图撑大）；填坏了退回 100 而不是
     把 NaN 写进 style；认不出的档退回 auto——把票面留在"没倍数"的状态比退回默认更糟。
     返回 null 的意思就是"别碰宽度"。 */
  autoZoomPct(paneH, boxH100){
    if (!(paneH > 0 && boxH100 > 0)) return 100;
    return Math.min(500, Math.max(100, Math.round(paneH * 100 / (3 * boxH100))));
  },
  locateZoom(mode, fixedPct, autoPct, maxPct){
    const cap = maxPct > 0 ? maxPct : 100;
    if (mode === "off") return null;
    if (mode === "fixed"){
      const f = Math.round(Number(fixedPct));
      if (!(f > 0)) return 100;
      return Math.min(Math.max(100, f), cap);
    }
    return Math.min(autoPct > 0 ? autoPct : 100, cap);
  },

  /* 滚轮一格一格调倍率：乘法步进（±15%）比固定 ±25 自然——小倍率时一跳 25% 太猛，
     大倍率时 25% 又太细；而且来回滚一格要能回到原值。 */
  zoomStep(z, dir){
    const v = (z > 0 ? z : 100) * (dir > 0 ? 1.15 : 1 / 1.15);
    return Math.min(400, Math.max(25, Math.round(v)));
  },
  /* 缩放要钉住鼠标底下那一处，否则每滚一下视野都甩回左上角，看细节得重新找位置。
     给"新内容尺寸、鼠标点在旧内容里的比例、光标距栏位左上角的距离"，返回该放到的滚动量。 */
  zoomAnchor(contentNew, frac, cursorInPane){
    return Math.max(0, Math.round(contentNew * frac - cursorInPane));
  },
};
if (typeof module !== "undefined") module.exports = L;
