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
  daySummary(rows){
    const rs = rows || [];
    let submitted = 0, failed = 0, flags = 0;
    rs.forEach(r => {
      if (r.submitted) submitted++;
      if (r.failed) failed++;
      flags += r.flags || 0;
    });
    return {total: rs.length, submitted: submitted, pending: rs.length - submitted,
            failed: failed, flags: flags};
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
    return Math.max(28, Math.round(Number(contentH) || 28));
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
