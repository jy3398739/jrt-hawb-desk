"use strict";
/* ==FIELDS== */
/* 由 fieldspec.py 生成，改字段只改那一处，然后跑：python deploy/gen_fields.py
   ——tests/test_fieldspec.py 会盯着有没有忘记重生成，别让这里长出一份手写副本。 */
const FIELDS = [
  ["MAWB_NO","主单号","id"],
  ["HAWB_NO","分单号","id"],
  ["ORIGIN_NAME","起运港","route"],
  ["TO1","航路 1","route"],
  ["TO2","航路 2","route"],
  ["TO3","航路 3","route"],
  ["DEST_NAME","目的港","route"],
  ["CREATE_TIME","签发日期","cargo"],
  ["PIECES","件数","cargo"],
  ["WEIGHT","毛重","cargo"],
  ["SLAC","小件数 SLAC","cargo"],
  ["GOODS_INFO","货物描述","cargo"],
  ["GOODS_HS_CODE","HS 编码","cargo"],
  ["SEND_STATUS","状态","cargo"],
  ["SHIPPER_INFO","发货人整串","ship"],
  ["SHIPPER_INFO_COMP_NAME","公司名称","ship"],
  ["SHIPPER_INFO_COMP_ADDRESS","详细地址","ship"],
  ["SHIPPER_INFO_CITY","城市","ship"],
  ["SHIPPER_INFO_STATE","州 / 省","ship"],
  ["SHIPPER_INFO_POSTAL","邮编","ship"],
  ["SHIPPER_INFO_COUNTRY","国家","ship"],
  ["SHIPPER_INFO_TEL","电话","ship"],
  ["SHIPPER_INFO_FAX","传真","ship"],
  ["SHIPPER_INFO_EORI","EORI / 税号","ship"],
  ["SHIPPER_INFO_AEO","AEO","ship"],
  ["SHIPPER_INFO_EMAIL","邮箱","ship"],
  ["CONSIGNEE_INFO","收货人整串","cons"],
  ["CONSIGNEE_INFO_COMP_NAME","公司名称","cons"],
  ["CONSIGNEE_INFO_COMP_ADDRESS","详细地址","cons"],
  ["CONSIGNEE_INFO_CITTY","城市","cons"],
  ["CONSIGNEE_INFO_STATE","州 / 省","cons"],
  ["CONSIGNEE_INFO_POSTAL","邮编","cons"],
  ["CONSIGNEE_INFO_COUNTRY","国家","cons"],
  ["CONSIGNEE_INFO_TEL","电话","cons"],
  ["CONSIGNEE_INFO_FAX","传真","cons"],
  ["CONSIGNEE_INFO_EORI","EORI / 税号","cons"],
  ["CONSIGNEE_INFO_AEO","AEO","cons"],
  ["CONSIGNEE_INFO_EMAIL","邮箱","cons"]
];
const KEYS = FIELDS.map(f => f[0]);
const GROUPS = {id:"单号",route:"航路",cargo:"货物与日期",ship:"发货人 SHIPPER",cons:"收货人 CONSIGNEE"};
const NUM = {PIECES:"int", SLAC:"int", WEIGHT:"num"};
const LONG = new Set(["SHIPPER_INFO","CONSIGNEE_INFO","GOODS_INFO","SHIPPER_INFO_COMP_ADDRESS","CONSIGNEE_INFO_COMP_ADDRESS"]);
/* ==/FIELDS== */
const EXTS = [".png",".jpg",".jpeg",".bmp",".tif",".tiff",".pdf",".xlsx",".xlsm",".xls"];

/* 页面可能被反代挂在子路径下（服务器上是 /hawb/）：接口前缀跟着当前目录走。
   根路径下为空串，与从前完全一致；/hawb/ 与 /hawb/index.html 下均为 "/hawb"。 */
const BASE = location.pathname.replace(/\/(index\.html)?$/, "");
const SUBMIT_URL = BASE + "/submit";

const LS = {draft:"hawb.review.drafts", masterDrafts:"hawb.review.masterDrafts", hintLocate:"hawb.review.hint.locate", dayDensity:"hawb.review.day.density",
            locateZoom:"hawb.review.locate.zoom", fieldH:"hawb.review.fieldH",
            split:"hawb.review.split", colK:"hawb.review.colK", open:"hawb.review.open"};
const $ = s => document.querySelector(s);
const nowTxt = () => new Date().toISOString().slice(0,19).replace("T"," ");
const S = {tickets:[], sel:null, running:false};
/* 分段核对流的界面态：不属于任何一张票，也不落盘。
   FOLD = 折了哪几段（折不折是看板的偏好，换票不该重来）；
   DESK.onlyTodo = 只看待核对；DESK.armed = 提交那颗按钮武装了没有。 */
const FOLD = new Set();
const DESK = {onlyTodo:false, armed:false};
const RAILTAB = {cur:"queue"};
/* MV = 「主单」视图：公司主单检索 → 自动解析（L1 原文 / 36 列可编辑 / 红旗）→ 人工确认后
   按 mawb3 提交发送回公司。它不是分单票据：字段面、红旗判据、提交接口都是主单那一套。 */
let MV = null;
let MST_LAST = null;             // 最近一次主单检索的原始响应，「送入主单核对」用它，不必再打一次公司接口
let MV_POLL = null;             // 主单核对区那个轮询的句柄（退出视图要停）
const MV_LONG = new Set(["SHIPPER_INFO_COMP_NAME", "SHIPPER_INFO_COMP_ADDRESS",
                         "CONSIGNEE_INFO_COMP_NAME", "CONSIGNEE_INFO_COMP_ADDRESS",
                         "NOTIFY_INFO_COMP_NAME", "NOTIFY_INFO_COMP_ADDRESS"]);
let ME = null;   // 当前登录者 {name, role}：null = 未登录（显示登录遮罩）
let PW_DEFAULT = false;   // 管理员口令还是默认值——服务端只告诉管理员本人（见 /api/me）

/* 提示成队列：一次失败常常同时冒出两条原因（"这张票没提交" + "登录已过期"），
   后一条把前一条吃掉，制单员就只剩半句线索可猜。读屏器靠容器的 aria-live 知道有新消息，
   所以这里必须真的新增节点，而不是往同一个节点覆写文字。 */
function toast(msg, kind){
  const box = $("#toasts"), el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add("on"));              // 先上图再加 on，淡入才看得见
  while (box.children.length > 4) box.removeChild(box.firstChild);   // 上限：堆几十条等于没有
  el.addEventListener("click", () => el.remove());                   // 看一眼就能手动关掉，不用等它自己消失
  setTimeout(() => el.remove(), kind === "bad" ? 9000 : 3500);       // 失败留久一点：那是要拿去改的东西
}
/* 定位这件事在界面上只剩六个字（点字段名 → 定位票面），第一次核票的人不知道那能点。
   所以首次解析成功时提一次，提过就写进 localStorage——天天弹同一句就是噪音。 */
function hintLocate(){
  if (localStorage.getItem(LS.hintLocate)) return;
  localStorage.setItem(LS.hintLocate, "1");
  toast("点右边的字段名，左边票面会定位并放大对应原文", "ok");
}
const esc = L.esc;
/* ── 轮度调度器：三处"等结果"共用一个（主单检索、主单核对、解析作业） ──────────────
   从前各自 setTimeout 递归：固定节奏、失败也照打、页面切走还在打，登录一过期就变成
   401 死循环——制单员看着像卡死，其实是客户端在敲一扇不再开的门。
   这里统一：越等越稀疏、有总时限、页面隐藏就停、切回来立刻补一次；run 返回 true 表示别再打了。 */
const POLLERS = new Set();
function poller(run, opts){
  const o = Object.assign({every: 2000, cap: 15000, maxMs: 30 * 60 * 1000, giveUp: () => {}}, opts || {});
  const p = {started: Date.now(), tries: 0, timer: null, paused: false};
  p.step = () => {
    p.tries++;
    const again = () => {
      if (!POLLERS.has(p)) return;
      if (Date.now() - p.started > o.maxMs){ POLLERS.delete(p); o.giveUp(); return; }
      p.timer = setTimeout(p.step, L.pollDelay(p.tries, o.every, o.cap));
    };
    Promise.resolve().then(() => run(p.tries))
      .then(stop => { if (stop) POLLERS.delete(p); else again(); })
      .catch(() => again());          // 网络抖也按同一退避继续等，别把制单员的结果弄丢
  };
  POLLERS.add(p);
  p.timer = setTimeout(p.step, o.every);
  return {stop(){ clearTimeout(p.timer); p.paused = false; POLLERS.delete(p); }};
}
document.addEventListener("visibilitychange", () => {
  POLLERS.forEach(p => {
    if (document.hidden){ clearTimeout(p.timer); p.paused = true; }
    else if (p.paused){ p.paused = false; p.timer = setTimeout(p.step, 0); }   // 切回来立刻补一次
  });
});
function txt(o, k){ return L.str(o, k); }
function toNum(k, v){
  const s = String(v).replace(/,/g, "").trim();
  const m = s.match(NUM[k] === "int" ? /-?\d+(\.\d+)?/ : /-?\d+(\.\d+)?/);
  if (!m) return s === "" ? null : s;
  if (NUM[k] === "int" && m[0].includes(".")) return s;   // 件数出现小数：留着原样让人去查，不悄悄取整
  return NUM[k] === "int" ? parseInt(m[0], 10) : parseFloat(m[0]);
}

/* ── 即时规则提示：与后端 validator 同口径，只提示不阻断 ─────────────────
   只提示航空口径这一列：原文口径已不展示，提示它等于让人去改看不见的东西。
   原文侧的毛病由后端 validator / 保真回查出红旗，挂在它点名的那一行上。 */
function checks(k, l2, l3){
  const out = [], g = v => (v === undefined || v === null ? "" : String(v).trim()), a = g(l3);
  if (k === "MAWB_NO"){
    const d = a.replace(/\D/g, "");
    if (d){
      if (d.length !== 11) out.push(["主单号要 3 位前缀+8 位数字", "warn"]);
      else if (+d.slice(3, 10) % 7 !== +d.slice(10)) out.push(["主单号 IATA 校验位不符", "warn"]);
    }
  }
  if (k.endsWith("_COUNTRY")){
    if (a && !/^[A-Z]{2}$/.test(a)) out.push(["国家要 ISO 两位码", "warn"]);
    if (!a && g(l2)) out.push(["票面有国家却没转出码：码表缺项", "warn"]);
  }
  if (["ORIGIN_NAME","DEST_NAME","TO1","TO2","TO3"].includes(k)){
    if (a && !/^[A-Z0-9]{3}$/.test(a)) out.push(["城市要三字码：补码表或改票面名", "warn"]);
    if (!a && g(l2)) out.push(["票面有城市却没映射出三字码", "warn"]);
  }
  if (/_TEL$|_FAX$/.test(k)){
    if (a && !/^\+\d{6,}$/.test(a.replace(/[\s().-]/g, ""))) out.push(["电话/传真未归一成 +数字", "warn"]);
  }
  if (k.endsWith("_EORI")){
    /* 识别号只剩这一格：海关号与税号同住，一格可能用 " / " 装两个号，所以逐段判
       （分隔符两侧必须带空格——CNPJ 自己就含斜杠）。 */
    const segs = a.split(/\s+\/\s+/).filter(s => s);
    if (segs.some(s => /^(USCI|CNPJ|CPF|RFC|GST\s*IN|GST|TAX\s*(ID|NO)?|VAT\s*(NO|NR|ID|NUMBER)?|统一社会信用代码)[#.:：\s]/.test(s)))
      out.push(["识别号混进了标签，只填号码本身", "warn"]);
    if (segs.some(s => s.replace(/[^0-9A-Za-z]/g, "").length < 8))
      out.push(["识别号过短，疑似截断", "warn"]);
  }
  if (k === "CREATE_TIME"){
    if (a && !/^\d{4}-\d{2}-\d{2}$/.test(a)) out.push(["签发日期要 YYYY-MM-DD", "warn"]);
    if (a && /^\d{1,2}-[A-Za-z\u4e00-\u9fa5]{2,}-?\d{2}$/.test(g(l2)) && g(l2).slice(-2) === a.slice(8))
      out.push(["票面是两位年，别把「年」当成日", "warn"]);
  }
  if (NUM[k] !== undefined){
    if (a && toNum(k, a) === null || (NUM[k] === "int" && a && /\./.test(a))) out.push(["件数不该有小数或非数字", "warn"]);
    const p = parseFloat(g(l2).replace(/[^\d.]/g, "")), q = parseFloat(a.replace(/[^\d.]/g, ""));
    if (k === "WEIGHT" && p && q && Math.abs(q - p * 2) < 1e-6)
      out.push(["疑似把总数行重复累加了：正好是票面毛重的 2 倍", "warn"]);
  }
  return out;
}

/* ── 质检红旗 → 落到哪几列 ───────────────────────────────────────────── */
function flagsByField(qc){
  const map = {}, rest = [];
  for (const f of (qc && qc.flags) || []){
    const ks = L.flagKeys(f, KEYS);
    if (ks.length) for (const k of ks) (map[k] = map[k] || []).push(f); else rest.push(f);
  }
  return {map, rest};
}
function flagNote(f){
  if (f.startsWith("保真")) return "保真未命中票面转录：" + f.slice(3).trim();
  return f;
}

/* 缺号类红旗按"当前值"动态判定：制单员手填主/分单号后，那条"XX_NO 缺失/均为空"就该消，
   否则会出现"号已填却仍被判需复核、提交点不动"。保真/误填等其它红旗不受此影响。
   识别号"混进了标签（航空口径已自动摘掉）"同理：值已被系统洗干净且未被人工改掉，纯提示不该卡提交。 */
function awbFilled(t){
  const rev = reviewed(t);
  return { MAWB_NO: String(rev.MAWB_NO || "").trim() !== "",
           HAWB_NO: String(rev.HAWB_NO || "").trim() !== "" };
}
function isMissingAwbFlag(f){
  return /缺失|均为空|都(?:为空|缺失)/.test(f) && (f.includes("MAWB_NO") || f.includes("HAWB_NO"));
}
function effectiveFlags(t){
  /* 「已清」只认两种：人点过确认（ackF 就是提交时的 acked_flags），或号补齐后服务端重算
     本来就不再报它。从前这里还有第三条"税号那格已自动摘掉标签 → 当它已清"的猜测：它把旗从
     界面上拿掉却不写进 acked_flags，而服务端那道门只认 acked_flags 里的字符串
     （server.py 的 `f not in acked`）——那条旗就成了看不见也点不到的死旗，整段核对同样收不到
     它（2026-10-12 现场：待办 红旗 0，提交回 400）。
     srvFlags 是服务端 400 里点名的几条：改了值旧旗文案会变，重算出的可能本地根本没有，
     不接住就永远只看见"还有未清的红旗"这句没有指名的话。 */
  const filled = awbFilled(t), ack = t.ackF || [];
  const seen = {}, out = [];
  (((t.qc && t.qc.flags) || []).concat(t.srvFlags || [])).forEach(f => {
    if (seen[f] || ack.includes(f)) return;
    if (isMissingAwbFlag(f)){
      const okM = !f.includes("MAWB_NO") || filled.MAWB_NO;
      const okH = !f.includes("HAWB_NO") || filled.HAWB_NO;
      if (okM && okH) return;              // 缺的号都补上了 → 服务端重算也不报它
    }
    seen[f] = 1; out.push(f);
  });
  return out;
}

/* ── 本机未保存的编辑（防刷新丢编辑；接口未就绪时也是提交兜底） ──────────────
   改名是因为「暂存」现在单指服务器那一档：同名会让制单员以为点了就交上去了，
   而没上服务器的票同事查不到。这块存的只是这台浏览器里的编辑。 */
function drafts(){ try { return JSON.parse(localStorage.getItem(LS.draft) || "{}"); } catch (e) { return {}; } }
/* 写盘改成「记脏 + 攒一下」：从前每敲一个字都把全部草稿 stringify 一遍，一次编辑 input 与 blur
   还各写一遍——打字延迟和写放大都出在这条上（2026-10-16 效率审查 F2）。现在输入路径只记哪几张
   脏了，350ms 后合并写一次；失焦、切走、关页面前立刻落盘。
   读仍每次现读（不缓存整份）：另一个标签页写的草稿不会被这一份盖掉。 */
const DRAFT_MS = 350, DRAFT_KEEP = 20;
let draftPending = {}, draftTimer = 0;
function draftBody(t){
  /* ackE（哪些空栏人确认过「本就为空」）与 todoBase（进度分母那份快照）都要跟着存：
     只存 airE 的话，刷新一次确认全丢、进度从"核到一半"退回 0/0——那正是最该留住的两样。 */
  return {stem:t.stem, filename:t.filename, channel:t.channel, elapsed:t.elapsed, savedAt:nowTxt(),
          qc:t.qc, raw:t.raw, air:t.air, airE:t.airE, ackF:t.ackF || [], ackE:t.ackE || [],
          skipE:t.skipE || [],
          srvFlags:t.srvFlags || [], todoBase:t.todoBase, submitted:t.submitted};
}
function saveDraft(t){
  if (!t.stem) return;
  draftPending[t.stem] = draftBody(t);
  clearTimeout(draftTimer);
  draftTimer = setTimeout(draftFlush, DRAFT_MS);
}
function draftFlush(){
  clearTimeout(draftTimer); draftTimer = 0;
  const keys = Object.keys(draftPending);
  if (!keys.length) return;         // 没脏的就不写：blur 与 input 落到这儿只留一下
  const d = drafts();
  keys.forEach(k => { d[k] = draftPending[k]; });
  draftPending = {};
  writeDrafts(d);
}
/* 写不进去只有两种可能：这台浏览器不让存（隐私模式）或者存满了。都不该把整页编辑带崩，
   但也绝不许悄悄不说——先丢掉最旧的几张再试一次，还不行就当场讲一句「这一改刷新就没了」。 */
function writeDrafts(d){
  try { localStorage.setItem(LS.draft, JSON.stringify(d)); return; } catch (e) {}
  try { localStorage.setItem(LS.draft, JSON.stringify(L.draftTrim(d, DRAFT_KEEP))); }
  catch (e2) {
    toast("本机浏览器存不下草稿（隐私模式或已写满）：这一次编辑只在页面上，刷新就没了", "bad");
  }
}
function dropDraft(stem){
  delete draftPending[stem];        // 待写那份一起撤：不然下一次合并把删掉的又写回来
  const d = drafts();
  if (!Object.prototype.hasOwnProperty.call(d, stem)) return;
  delete d[stem];
  writeDrafts(d);
  renderDrafts();
}
window.addEventListener("pagehide", draftFlush);        // 关页面/被收回内存前落盘
document.addEventListener("visibilitychange", () => { if (document.hidden) draftFlush(); });
/* 老版本提交成功后把那份副本留着（还写着"未提交"）：开机清一次，值与确认都在服务器上了。 */
function pruneSubmittedDrafts(){
  const d = drafts();
  let n = 0;
  Object.keys(d).forEach(k => {
    if (d[k] && d[k].submitted && d[k].submitted.mode === "server"){ delete d[k]; n++; }
  });
  if (n) writeDrafts(d);
  return n;
}
function loadDraft(stem){
  const d = drafts()[stem]; if (!d) return null;
  return {file:null, filename:d.filename || d.stem, stem:d.stem, state:"done", restored:true,
          channel:d.channel, elapsed:d.elapsed, qc:d.qc, raw:d.raw || {}, air:d.air || {},
          airE:d.airE || {}, ackF:d.ackF || [], ackE:d.ackE || [], skipE:d.skipE || [], srvFlags:d.srvFlags || [],
          todoBase:typeof d.todoBase === "number" ? d.todoBase : null,
          submitted:d.submitted || null};
}

/* ── 票据模型：页面只展示/编辑航空口径，原文口径留在数据里做保真追溯 ──────── */
function airVal(t, k){
  if (k in t.airE) return t.airE[k];
  return txt(t.air, k);
}
function setEdit(t, k, v){
  const orig = txt(t.air, k);
  if (v === orig) delete t.airE[k]; else t.airE[k] = v;
  const fl = flagsByField(t.qc).map[k] || [];
  if (fl.length && (t.ackF || []).length) t.ackF = t.ackF.filter(f => !fl.includes(f));
  saveDraft(t);
}
function editedList(t){
  return Object.keys(t.airE).map(k => "air." + k);
}
function reviewed(t){
  const base = Object.assign({}, t.air), box = t.airE;
  for (const k in box) base[k] = NUM[k] !== undefined ? toNum(k, box[k]) : box[k];
  return base;
}
function payloadFor(t, who){
  return {
    filename: t.filename, stem: t.stem, channel: t.channel, elapsed: t.elapsed,
    reviewer: who, reviewed_at: nowTxt(),
    needs_review: effectiveFlags(t).length > 0, flags: effectiveFlags(t),
    acked_flags: (t.ackF || []).slice(),
    acked_empty: (t.ackE || []).slice(),
    edited_fields: editedList(t),
    raw_original: t.raw, air_original: t.air,
    air_reviewed: reviewed(t),
  };
}

/* ── 票面浏览：核对时把原件和字段并排看 ───────────────────────────────── */
const PREVIEW = {key:"", url:"", zoom:100, pages:null, mode:"", box:null, cycle:[], view:null};
let _pvSeq = 0, _pdfSeq = 0;

function fileKind(name){
  const e = "." + (String(name || "").split(".").pop() || "").toLowerCase();
  if (e === ".pdf") return "pdf";
  if ([".png", ".jpg", ".jpeg", ".bmp"].includes(e)) return "img";
  return "other";     // xlsx / tif：浏览器渲染不了，走服务端转出的 PDF 或下载
}
function releasePreview(){
  if (PREVIEW.url){ URL.revokeObjectURL(PREVIEW.url); PREVIEW.url = ""; }
  for (const u of (PREVIEW.pages || [])) URL.revokeObjectURL(u);
  PREVIEW.pages = null;
}
function pvBody(html){
  /* 舞台与三层浮层是 index.html 里的常驻节点：以前这里直接换 #pvBody 的 innerHTML，
     现在只换内容那一格——不然每画一次票面就把锚点/悬停/定位三层各自的框一起删掉，
     移开鼠标会把刚点出来的定位框也擦走（三层各管一件事的理由就在这）。 */
  $("#pvPages").innerHTML = html;
  drawAnchors();
  hoverField("");
  $("#pvBoxes").innerHTML = "";
}
function applyZoom(){
  const imgs = [...document.querySelectorAll("#pvBody img")];
  if (!imgs.length) return;
  const box = $("#pvBody"), im = imgs[0];
  /* 放大不许超过渲染出来的像素（超了只是把糊图撑大），没解码完就先不设上限 */
  PREVIEW.zoom = Math.min(PREVIEW.zoom, im.naturalWidth ? L.maxZoomPct(im.naturalWidth, box.clientWidth) : 400);
  for (const x of imgs) x.style.width = PREVIEW.zoom + "%";
  $("#pvZoomTxt").textContent = PREVIEW.zoom + "%";
  box.classList.toggle("pannable",
    L.canPan(box.scrollWidth, box.clientWidth) || L.canPan(box.scrollHeight, box.clientHeight));
  drawAnchors();          // 倍率一变，锚点/悬停那些按像素画的框就要照着新矩形重摆（不清就会飘在旧位置上）
  hoverField(HOVER.k);
  if (PREVIEW.mode !== "pages") return;
  const pct = L.wholePagePct(im.naturalWidth, im.naturalHeight, box.clientWidth, box.clientHeight);
  const btn = $("#pvPage");
  btn.dataset.pct = String(pct);
  btn.textContent = Math.abs(PREVIEW.zoom - pct) < 3 ? "适应宽度" : "整页看全";
  btn.hidden = pct >= 90;               // 和适应宽度差不到 10% = 点了没反应，别放这颗按钮
}
/* 放大后按住票面拖就能看别处，不用去够那根滚动条。只在 .pannable（内容真超出栏位）时接手，
   光标与行为同一判据，不会出现"看着能拖其实拖不动"。
   用 pointer 事件：鼠标 / 触屏 / 手写笔一套代码；按下即抓住指针，拖出栏外也不断，松手或被系统
   取消（pointercancel，例如手势接管）都要收手，否则下次一进来就是抓取状态。 */
function installPan(){
  const pane = $("#pvBody");
  let drag = null;
  pane.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !pane.classList.contains("pannable")) return;
    drag = {x: e.clientX, y: e.clientY, l: pane.scrollLeft, t: pane.scrollTop};
    pane.setPointerCapture(e.pointerId);
    pane.classList.add("grabbing");
    e.preventDefault();                 // 顺手把"拖图片"和选中也压掉
  });
  pane.addEventListener("pointermove", e => {
    if (!drag) return;
    pane.scrollLeft = drag.l - (e.clientX - drag.x);
    pane.scrollTop = drag.t - (e.clientY - drag.y);
  });
  const stop = () => { if (!drag) return; drag = null; pane.classList.remove("grabbing"); };
  pane.addEventListener("pointerup", stop);
  pane.addEventListener("pointercancel", stop);
}
/* 滚轮 = 调倍率（挪视野已经交给按住拖动，滚轮再留着滚动意义不大）。两处必须挡住：
   普通滚轮会连着滚整页，Ctrl+滚轮会被浏览器当成"缩放整个标签页"——所以 passive:false。
   定位模式下不接：那边的倍率是 autoZoom 按命中行算的，这里改会跟它打架。
   退回浏览器阅读器那张票也不接：iframe 的滚轮本来就该阅读器自己处理。 */
function wheelZoom(e){
  if (PREVIEW.mode === "viewer" || LOC.on) return;
  const pane = $("#pvBody"), im = pane.querySelector("img");
  if (!im) return;
  e.preventDefault();
  const r = pane.getBoundingClientRect(), cx = e.clientX - r.left, cy = e.clientY - r.top;
  const fx = im.offsetWidth ? (cx + pane.scrollLeft) / im.offsetWidth : 0;
  const fy = im.offsetHeight ? (cy + pane.scrollTop) / im.offsetHeight : 0;
  PREVIEW.zoom = L.zoomStep(PREVIEW.zoom, e.deltaY < 0 ? 1 : -1);
  applyZoom();                        // 里面还会按渲染像素夹一次上限
  pane.scrollLeft = L.zoomAnchor(im.offsetWidth, fx, cx);
  pane.scrollTop = L.zoomAnchor(im.offsetHeight, fy, cy);
}
function showPreview(url, kind){
  releasePreview();
  PREVIEW.url = url;
  PREVIEW.mode = kind === "pdf" ? "viewer" : "img";     // 回退路径：按钮语义按阅读器/原图分开
  if (kind === "pdf"){
    $("#pvZoom").hidden = true;
    $("#pvPdf").hidden = false;
    $("#pvBody").classList.remove("pannable");   // 阅读器自己管滚动，留着上一张票的抓手就是骗人
    PREVIEW.box = null;
    readPdfBox();                       // 先量到页面尺寸，再画第一帧（省一次重画）
  } else {
    PREVIEW.zoom = 100;
    $("#pvZoom").hidden = false;
    $("#pvPdf").hidden = true;
    pvBody(`<img id="pvImg" src="${url}" alt="票面原件">`);
    applyZoom();
  }
}
/* 阅读器默认带缩略图侧栏，窄栏里它要占掉一半宽度、票面只剩半个——用开放参数关掉。
   档位（整页 / 适应宽度 / 放大看细节）由 L.viewCycle 算：窄而高的栏里"整页"和"适应宽度"
   是同一个缩放，那种档留着就是颗点了没反应的死按钮，所以会被丢掉（2026-10-02 用户报的就是这个）。 */
function pdfFrame(){
  const box = $("#pvBody");
  PREVIEW.cycle = L.viewCycle(PREVIEW.box, {w: box.clientWidth, h: box.clientHeight});
  if (!PREVIEW.cycle.some(s => s.k === PREVIEW.view)) PREVIEW.view = (L.firstView(PREVIEW.cycle) || {}).k;
  const cur = PREVIEW.cycle.filter(s => s.k === PREVIEW.view)[0];
  const nxt = L.nextView(PREVIEW.cycle, PREVIEW.view);
  $("#pvPdf").hidden = PREVIEW.cycle.length < 2;        // 只剩一档就没得切，别留颗死按钮
  if (cur && nxt) $("#pvPage").textContent = nxt.name;
  pvBody(`<iframe src="${PREVIEW.url}#navpanes=0&${cur ? cur.frag : "view=FitH"}" title="票面 PDF"></iframe>`);
}
/* 页面尺寸就在我们手里那份 PDF 字节的前部（MediaBox），扫一下即可，不为此发请求。
   blob 读很快，第一帧就等它，避免"先画一版再重画"把用户的滚动位置冲掉。 */
function readPdfBox(){
  const blobUrl = PREVIEW.url, seq = ++_pdfSeq;   // blob: 地址只在浏览器内，不发往服务器
  fetch(blobUrl).then(r => r.arrayBuffer()).then(b => {
    if (seq !== _pdfSeq || blobUrl !== PREVIEW.url) return;
    PREVIEW.box = L.pdfBox(new Uint8Array(b));
    pdfFrame();
  }).catch(() => {
    if (seq !== _pdfSeq || blobUrl !== PREVIEW.url) return;
    PREVIEW.box = null; pdfFrame();     // 读不到就退回浏览器那两态，行为不比今天差
  });
}
/* 票面默认不再嵌浏览器阅读器：Chrome/Edge 各自的侧栏与工具栏要吃掉近 1/4 宽度，
   而关侧栏的参数两家不通用（实测 Edge 完全不理 navpanes=0）。改成逐页取我们服务端渲染的
   页图（/render，定位模式本来就在用），缩放与「整页看全」全归我们算，两家表现一致。
   拿不到归档（只可能是早于「上传一律落盘」上线的老票）才退回阅读器，所以这里返回 false 让调用方接着走老路。 */
const PV_SCALE = 2;                     // ≈150dpi：A4 出 1190px，够放到 176% 不糊
async function showPages(stem, seq){
  const urls = [];
  try{
    const first = await fetch(BASE + `/render/${encodeURIComponent(stem)}?page=1&scale=${PV_SCALE}`);
    if (!first.ok) return false;
    const total = parseInt(first.headers.get("X-Ticket-Pages") || "1", 10) || 1;
    urls.push(URL.createObjectURL(await first.blob()));
    for (let n = 2; n <= total; n++){
      const r = await fetch(BASE + `/render/${encodeURIComponent(stem)}?page=${n}&scale=${PV_SCALE}`);
      if (r.ok) urls.push(URL.createObjectURL(await r.blob()));
    }
  }catch(e){ return false; }
  if (seq !== _pvSeq){ for (const u of urls) URL.revokeObjectURL(u); return true; }
  releasePreview();
  PREVIEW.pages = urls.slice(1);        // 第 1 页放 url，「新窗口」拿它
  PREVIEW.url = urls[0];
  PREVIEW.mode = "pages";
  PREVIEW.zoom = 100;
  pvBody(`<img class="pvpage" id="pvImg" src="${urls[0]}" alt="票面第 1 页">` +
         urls.slice(1).map((u, i) => `<img class="pvpage" src="${u}" alt="票面第 ${i + 2} 页">`).join(""));
  $("#pvZoom").hidden = false;
  $("#pvPage").textContent = "整页看全";
  $("#pvPage").hidden = false;
  $("#pvPdf").hidden = false;
  const im = $("#pvBody img");
  if (im && !im.naturalWidth) im.addEventListener("load", applyZoom);   // 量不到像素会把按钮错藏掉
  applyZoom();
  return true;
}
/* 同一张票不重建预览：render() 会因为解析进度等反复跑，重建会把 iframe 的滚动位置也一起清掉 */
function previewKey(t){
  if (!t) return "";
  /* 有归档名就能走页图模式；键里带上这个，刚上传那会儿只能吃 blob、归档一落定就升级成页图 */
  if (t.stem) return "S|" + t.stem + (t.state === "done" ? "|d" : "|p");
  if (t.file && fileKind(t.filename) !== "other") return "F|" + t.filename;
  return "A|" + t.filename + (t.state === "done" ? "|d" : "|p");
}
/* 主单视图：左栏放 L1 原文（公司资料逐行），右栏放解析出来的 36 列可编辑面。
   主单没有票面版式原件，所以这一栏是"资料文本"而不是"票面图"，点字段也不能定位——界面上说明白。 */
function mvPreview(){
  const tr = (MV.rec && MV.rec.transcript) || null;
  const lines = tr && tr.lines && tr.lines.length
    ? tr.lines : Object.keys(MV.order || {}).filter(k => k !== "AMS_RECORD" && typeof (MV.order[k]) === "string" && String(MV.order[k]).trim())
      .map(k => ({i: 0, text: k + ": " + MV.order[k]}));
  if (!lines.length)
    return (MV.rec && MV.rec.state === "parsing")
      ? '<div class="empty">正在取公司资料并解析：解析完成后这里显示 L1 原文。</div>'
      : '<div class="empty">公司侧这条主单没有资料文本：接口没返回，或从未录入。</div>';
  /* 本机写回公司的那几行**不进这一栏**（剔在后端一处，判据见 master_fields.original_lines）。
     主单没有票面版式原件，这段资料文本就是唯一的原始凭据：混进我们自己的输出，模型就是在抄
     上次的答案，漏取与逐字保真两道核查也一起假绿。v1.27.0 只是标一下，IT 2026-10-16 不认——
     "还是要原始数据，不管主单解析几次"。值本身没被改动：右边 36 列那一格照旧带着徽标。 */
  const EX = (MV.rec && MV.rec.excluded_self) || [];
  const UN = (MV.rec && MV.rec.baseline_dirty) ? (MV.rec.unknown_self || []) : [];
  const rows = lines.map(x => '<div class="j blk"><b>' + (x.i ? x.i + ':' : '') + '</b> ' + esc(x.text)).join("");
  return '<div class="mvtxt"><div class="t2">L1 原文 · 公司主单资料（逐字，未加工）</div>' + rows +
    '<p class="hint hintp flush">主单没有票面版式原件，所以这里是资料文本而非票面图：点右边字段名不能定位高亮。' +
    (EX.length ? '已按原始资料排除 <b>' + EX.length + '</b> 行本机写回的值：那一行既不进资料也不进核查，右边 36 列里看得到、也照旧能改。' : '') +
    (UN.length ? '另有 <b>' + UN.length + '</b> 行分不清来源：这张票连一份可比对的原始资料都没有，要真值请回公司系统看。' : '') +
    ((MV.rec && MV.rec.baseline_dirty) ? '这一张的原始基线是从上一次解析那份补的（不是当场钉下的），边界可能差一行。' : '') +
    '</p></div>';
}
function renderPreview(){
  if (MV){
    /* 键带上解析状态与耗时：检索另一张主单时先出"解析中"空壳、解析完自动填上，
       同一张主单重解析也是——键里只有号的话，状态变了这一栏不重画，
       就会挂着"正在取公司资料"去配一张已经解析完的核对表。 */
    const key = "M|" + MV.mawb + "|" + (MV.rec.state || "") + "|" + (MV.rec.elapsed || 0);
    if (key === PREVIEW.key) return;
    ++_pvSeq;                                   // 作废在飞的票面取回，别让原件盖掉公司资料
    PREVIEW.key = key;
    releasePreview();
    $("#pvName").textContent = "主单 " + MV.mawb + " · L1 原文（公司资料逐行，非票面图）";
    $("#pvTools").hidden = true;
    pvBody(mvPreview());
    return;
  }
  const t = current(), key = previewKey(t);
  /* 定位模式画的是票面页图：切到别的票时必须先退出，否则正常预览会把高亮层冲掉 */
  if (LOC.on && (!t || (t.stem || "") !== LOC.stem)){ exitLocate(); return; }
  if (key === PREVIEW.key) return;
  PREVIEW.key = key;
  loadPreview(t);
}
async function loadPreview(t){
  const seq = ++_pvSeq;
  releasePreview();
  $("#pvName").textContent = t ? (t.filename || "") : "";
  $("#pvTools").hidden = !t;
  if (!t){ pvBody(`<div class="empty">上传分单后显示票面。</div>`); return; }
  const kind = fileKind(t.filename);
  /* 有归档就一律走我们渲染的页图（页图模式没有阅读器侧栏/工具栏，两家浏览器表现一致）；
     /render 拿不到（没勾落盘、或刚上传还没归档）才退回下面这条 blob 老路。 */
  if (t.stem && await showPages(t.stem, seq)) return;
  if (t.file && kind !== "other"){
    showPreview(URL.createObjectURL(t.file), kind);     // 本次会话上传的：直接用浏览器里的文件，不占服务端
    return;
  }
  if (!t.stem){ pvBody(`<div class="empty">正在解析…解析完就能回看票面。</div>`); return; }
  pvBody(`<div class="empty">正在取票面…</div>`);
  try{
    const r = await fetch(BASE + "/source/" + encodeURIComponent(t.stem));
    if (seq !== _pvSeq) return;                          // 期间切到别的票了，这份结果丢掉
    if (!r.ok){
      const j = await r.json().catch(() => ({}));
      pvBody(`<div class="empty">${esc(j.detail || ("取票面失败 HTTP " + r.status))}<br>
        <span class="sub">这台机器上没有它的归档：重传一次这张票就能回看（现在上传都会落盘）。</span></div>`);
      return;
    }
    const blob = await r.blob();
    if (seq !== _pvSeq) return;
    showPreview(URL.createObjectURL(blob),
                (r.headers.get("content-type") || "").includes("pdf") ? "pdf" : "img");
  }catch(e){
    if (seq === _pvSeq) pvBody(`<div class="empty">取票面失败：${esc(String(e.message || e))}</div>`);
  }
}
function saveBlob(blob, name){
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
async function downloadSource(){
  const t = current(); if (!t) return;
  if (t.file && fileKind(t.filename) !== "other"){ saveBlob(t.file, t.filename); return; }
  if (!t.stem){ toast("还没解析完，稍后再下载", "bad"); return; }
  try{
    const r = await fetch(BASE + "/source/" + encodeURIComponent(t.stem) + "?raw=1");
    if (!r.ok){
      const j = await r.json().catch(() => ({}));
      toast(j.detail || ("取原件失败 HTTP " + r.status), "bad"); return;
    }
    saveBlob(await r.blob(), t.filename || (t.stem + ".pdf"));
  }catch(e){ toast("取原件失败：" + (e.message || e), "bad"); }
}

/* ── 字段 → 票面定位：点字段名，把 L1 转录 bbox 叠在页图上、自动放大到看得清 ────
   浏览器内嵌 PDF 阅读器无法按坐标高亮/缩放，所以定位模式用服务端按页渲染的 PNG 打底。
   匹配口径仿后端 fidelity 的 _find_lines 但更宽松：大小写忽略（大小写不符后端已出红旗，
   定位是给人看位置的）；整串对不上就拆词元对（999-0686 5456 vs 999 0686 5456 这类）。 */
const LOC = {on:false, key:"", stem:"", base:"", remote:false, url:"", ll:null,
             page:1, pages:1, hits:[], pagesWithMatch:new Set()};
const _FWJS = {"，":",", "：":":", "；":";", "（":"(", "）":")", "　":" "};
function normFace(s){
  let t = String(s == null ? "" : s).normalize("NFKC");
  t = t.replace(/[，：；（）\u3000]/g, c => _FWJS[c] || " ");
  return t.replace(/\s+/g, " ").trim().toUpperCase();
}
const faceToks = s => normFace(s).split(/[^0-9A-Z&]+/).filter(Boolean);
function matchLines(q, lines){
  const nq = normFace(q);
  if (!nq || !lines || !lines.length) return [];
  const tqs = faceToks(q).filter(t => t.length >= 3);
  const boxed = l => Array.isArray(l.bbox) && l.bbox.length === 4;
  let best = 0, out = [];
  for (const l of lines){
    if (!boxed(l)) continue;
    const nl = normFace(l.text);
    let s = 0;
    if (nl.includes(nq) || (nq.length >= 12 && nl.length >= 6 && nq.includes(nl))) s = 2;
    else if (tqs.length){
      const lt = faceToks(l.text);
      const miss = tqs.filter(t => lt.indexOf(t) < 0).length;
      if (!miss) s = 1;
      else if (tqs.length > 2 && miss <= Math.floor(tqs.length / 3)) s = 1;
    }
    if (!s) continue;
    if (s > best){ best = s; out = [l]; }
    else if (s === best) out.push(l);
  }
  if (!out.length && tqs.length){            // 兜底：按最长词元单挑，至少滚到那一区让人对眼
    const t = tqs.slice().sort((a, b) => b.length - a.length)[0];
    for (const l of lines) if (boxed(l) && normFace(l.text).includes(t)) out.push(l);
  }
  return out;
}
function fieldValCandidates(t, k){
  const out = [];
  for (const v of [txt(t.raw, k), airVal(t, k)]){
    const s = String(v || "").trim();
    if (s && !out.includes(s)) out.push(s);
  }
  return out;
}
/* ── 票面三层：常驻锚点 / 悬停出处 / 点字段那一次的定位框 ─────────────────────
   三层各有各的寿命，混在一层里就会互相擦掉：移开鼠标把刚点出来的定位框一起清掉，
   或者定位完留下一屏没人清的悬停框。坐标一律按"那一页现在实际占的矩形"换算：
   票面能放大能拖，写死百分比就在放大那天跑到别处（bbox 本身是每轴归一到 0-1000）。 */
function pageImg(p){
  const imgs = [...document.querySelectorAll("#pvPages img")];
  if (!imgs.length) return null;
  if (LOC.on) return (p || 1) === LOC.page ? imgs[0] : null;   // 定位模式一次只看一页
  return imgs[(p || 1) - 1] || null;
}
function lyrHtml(ls, cls, tip){
  const stage = $("#pvStage");
  if (!stage) return "";
  const sr = stage.getBoundingClientRect();
  const title = (tip || {}).title || "";
  return (ls || []).map(l => {
    const im = pageImg(l.page || 1);
    if (!im || !Array.isArray(l.bbox) || l.bbox.length !== 4) return "";
    /* 用矩形差而不是 offsetLeft：页面图外面还套着定位模式那一层 .page（它是 relative），
       offsetLeft 会算在那一层上，而浮层的坐标系是舞台。两个 rect 同进同退，差值与滚动无关。 */
    const r = im.getBoundingClientRect();
    const [x1, y1, x2, y2] = l.bbox;
    return `<div class="${cls}" title="${esc((title ? title + " · " : "") + l.text)}" `
      + `style="left:${Math.round(r.left - sr.left + r.width * x1 / 1000)}px;`
      + `top:${Math.round(r.top - sr.top + r.height * y1 / 1000)}px;`
      + `width:${Math.max(6, Math.round(r.width * (x2 - x1) / 1000))}px;`
      + `height:${Math.max(5, Math.round(r.height * (y2 - y1) / 1000))}px"></div>`;
  }).join("");
}
/* 常驻锚点只给"票面印了、模型没取"的那一格（未确认红旗）：出处先取 fidelity.sources，
   没有就从红旗文案里那个 值='…' 回查转录行。两条都找不到就不画——
   猜出来的框比不画更坏，它会让人以为这个位置已经核对过（空栏在真实数据里本来就没出处）。 */
function drawAnchors(){
  const el = $("#pvAnchors"); if (!el) return;
  el.innerHTML = "";
  const t = current();
  if (!t || MV) return;
  if (!t.ll || !(t.ll.lines || []).length){
    /* 从今日台账/主单侧点开的那张票手上没有转录（只有本次会话解析过的才有）：
       去 /layout 补一次，回来再画。补不到就当没有——锚点宁可不画也不猜（在途 Promise 由 layoutFor 去重）。 */
    if (t.stem) layoutFor(t).then(ls => { if (ls && current() === t) drawAnchors(); });
    return;
  }
  const lines = t.ll.lines;
  const src = ((t.qc || {}).fidelity || {}).sources || {};
  const leftMap = flagsByField({flags: effectiveFlags(t)}).map;
  let html = "";
  for (const k of KEYS){
    for (const f of (leftMap[k] || [])){
      html += lyrHtml((L.anchorLines(src, lines, f)[k] || []), "anchor hard",
                      {title: k + " 红旗出处"});
    }
  }
  el.innerHTML = html;
}
/* 悬停：指针进哪一行，就把那一格的转录出处轻轻框出来。只写 hover 那一层，
   并且只认盘上已有的转录——悬停是"顺手一看"，不该为它发请求。 */
const HOVER = {k: ""};
function hoverField(k){
  const el = $("#pvHover"); if (!el) return;
  HOVER.k = k || "";
  el.innerHTML = "";
  const t = current();
  if (!k || !t || MV || !t.ll) return;
  el.innerHTML = lyrHtml(hoverLines(t, k), "hbox");
}
function hoverLines(t, k){
  const ls = (t.ll && t.ll.lines) || [];
  for (const c of fieldValCandidates(t, k)){
    const hit = matchLines(c, ls);
    if (hit.length) return hit;
  }
  const src = (((t.qc || {}).fidelity) || {}).sources || {};
  return (src[k] || []).map(i => ls.find(l => l.i === i)).filter(Boolean);
}
/* 转录来源：本次会话提取的用响应里的 t.ll；本机未保存的编辑载入的从服务端 /layout 取（要归档过才有）。
   在途 Promise 挂在票上共享：连点两个字段时第二个别再发一遍请求、也不会误判"无转录"。 */
async function layoutFor(t){
  const usable = ll => { const ls = (ll && ll.lines) || []; return ls.some(l => l.bbox) ? ls : null; };
  let ls = usable(t.ll); if (ls) return ls;
  if (!t.stem) return null;
  if (!t._llP){
    t._llP = fetch(BASE + "/layout/" + encodeURIComponent(t.stem))
      .then(r => r.ok ? r.json() : null).catch(() => null)
      .then(j => { if (j) t.ll = j; return usable(t.ll); });
  }
  return t._llP;
}
async function enterLocate(t, k){
  if (!t || t.state === "busy" || t.state === "queued"){ toast("还没解析完，解析好才能定位", "bad"); return; }
  const seq = ++_pvSeq;
  const label = (FIELDS.find(f => f[0] === k) || [k, k])[1];
  const ll = await layoutFor(t);
  if (!ll){ toast(`「${label}」没有可用的 L1 转录：这张票早于转录留档上线，重传一次就能定位`, "bad"); return; }
  // 底图一律取服务端按页渲染的 /render。从前"没勾落盘"时只能拿浏览器里那份 Blob 顶一下，
  // 而现在上传必落盘，那份临时文件不再是任何路径的数据来源。
  const base = BASE + "/render/" + encodeURIComponent(t.stem);
  LOC.on = true; LOC.key = (t.stem || t.filename) + "|" + k;
  LOC.stem = t.stem || ""; LOC.base = base; LOC.remote = base.indexOf("/render/") > -1;
  LOC.ll = ll;
  LOC.pages = Math.max(1, ...ll.map(l => l.page || 1));
  const cands = fieldValCandidates(t, k);
  LOC.hits = []; LOC.q = "";
  for (const c of cands){ LOC.hits = matchLines(c, ll); if (LOC.hits.length){ LOC.q = c; break; } }
  LOC.pagesWithMatch = new Set(LOC.hits.map(l => l.page || 1));
  LOC.page = LOC.hits.length ? (LOC.hits[0].page || 1) : 1;
  $("#pvName").textContent = (t.filename || t.stem) + " · 定位：" + label;
  pvSetTools();
  await showLocPage(LOC.page, seq);
  if (!cands.length) toast("该字段值为空，先把整页票面放出来给你看", "ok");
  else if (!LOC.hits.length) toast("票面转录没直接命中这个值：L3 是归一口径（国家两位码/电话 + 号），票面可能印的是全称或分了行", "bad");
  else if (LOC.hits.length < 8)
    toast(`命中 ${LOC.hits.length} 行转录${LOC.pagesWithMatch.size > 1 ? "，跨第 " + [...LOC.pagesWithMatch].sort().join("/") + " 页" : ""}`, "ok");
}
async function showLocPage(page, seq){
  LOC.page = Math.min(Math.max(1, page || 1), LOC.pages);
  let src = LOC.base;
  if (LOC.remote){
    try{
      const r = await fetch(LOC.base + "?page=" + LOC.page + "&scale=2");
      if (!r.ok){
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || ("HTTP " + r.status));
      }
      const pg = +r.headers.get("X-Ticket-Pages");
      if (pg > 0) LOC.pages = pg;
      if (LOC.url) URL.revokeObjectURL(LOC.url);
      src = LOC.url = URL.createObjectURL(await r.blob());
    }catch(e){ pvBody(`<div class="empty">取票面页图失败：${esc(String(e.message || e))}<br>
        <span class="sub">按页渲染读的是归档原件：这台机器上没有它，重传一次就有。</span></div>`); return; }
  }
  if (seq !== _pvSeq) return;                 // 期间又点了别的字段/票，这份丢掉
  pvBody(`<div class="page" id="pvPageBox"><img id="pvImg" src="${src}" alt="票面第 ${LOC.page} 页"></div>`);
  drawLocBoxes();
  $("#pvLocPage").textContent = "第 " + LOC.page + "/" + LOC.pages + " 页";
  $("#pvLocPrev").disabled = LOC.page <= 1;
  $("#pvLocNext").disabled = LOC.page >= LOC.pages;
  const im = $("#pvImg");
  if (im.complete && im.naturalWidth) locFit(); else im.onload = locFit;
}
/* 定位框只写定位那一层：放大/换页之后要照着新的矩形重画一遍，不然框还停在放大前的位置。 */
function drawLocBoxes(){
  $("#pvBoxes").innerHTML = lyrHtml(LOC.hits.filter(l => (l.page || 1) === LOC.page), "bbox");
}
/* 点字段后放不放大、放多大是各人的手感（2026-10-09 用户要的选择权）：只存这台浏览器，
   不该由谁替同事定。默认 auto 就是这功能上线以来的行为。 */
function zoomPref(){
  let p = {};
  try{ p = JSON.parse(localStorage.getItem(LS.locateZoom) || "{}") || {}; }catch(e){ p = {}; }
  return {mode: ["auto", "fixed", "off"].indexOf(p.mode) > -1 ? p.mode : "auto",
          pct: +p.pct > 0 ? Math.round(+p.pct) : 250};
}
function saveZoomPref(p){ localStorage.setItem(LS.locateZoom, JSON.stringify({mode:p.mode, pct:p.pct})); syncZoomCtl(p); }
/* 控件跟着偏好走：只有"固定倍数"那一档填比例有意义，输入框随之启用/禁用。 */
function syncZoomCtl(p){
  const z = p || zoomPref();
  $("#pvLocZoom").value = z.mode;
  $("#pvLocPct").value = z.pct;
  $("#pvLocPct").disabled = z.mode !== "fixed";
}
function locCapPct(){
  const im = $("#pvImg"), pane = $("#pvBody");
  return (im && im.naturalWidth && pane.clientWidth) ? L.maxZoomPct(im.naturalWidth, pane.clientWidth) : 100;
}
/* 点字段后放不放大、放多大由人（见 zoomPref）。null（"不放大"那一档）落到 100%：
   不是"留着上一次放大后的倍数"，那样开关就只挡得住第一次。
   跳页、高亮、把命中行滚到正中照做——定位的价值是"告诉你这格印在哪儿"。 */
function locFit(){
  const im = $("#pvImg"), page = $("#pvPageBox"), pane = $("#pvBody");
  if (!im || !page || !im.naturalHeight) return;
  /* 那一行放到 100% 宽时有多高：bbox 每轴各自归一到 0-1000，(y2-y1)/1000 就是它占整页高的份额。
     从前是回头读第一个框的 inline height（那时是百分比），框改成按像素画之后那个数自己会跟着倍率变，
     再读它就是拿放大后的高度去算放大倍数。 */
  const hit = LOC.hits.filter(l => (l.page || 1) === LOC.page)[0];
  const frac = hit ? Math.max((hit.bbox[3] - hit.bbox[2]) / 1000, 0.004) : 0;
  const bh100 = frac * (im.naturalHeight / im.naturalWidth) * pane.clientWidth;   // 100% 宽时该行的高（px）
  const p = zoomPref();
  const cap = L.maxZoomPct(im.naturalWidth, pane.clientWidth);
  const z = L.locateZoom(p.mode, p.pct, L.autoZoomPct(pane.clientHeight, bh100), cap);
  $("#pvLocPct").max = cap;      // 上限先摆在框子上，别等人填了 9999 才告诉他会被收
  page.style.width = (z || 100) + "%";
  drawLocBoxes();                // 宽度刚变过：框要照着新的矩形重画，不然还停在放大前的位置
  const first = $("#pvBoxes").querySelector(".bbox");
  if (first){
    const fr = first.getBoundingClientRect(), br = pane.getBoundingClientRect();
    /* 巡航用 scrollTo：一次调用同时给两个方向，浏览器自己合成一段滚动。
       不给 #pvBody 写 scroll-behavior:smooth——installPan 每次 pointermove 都在改 scrollLeft/Top，
       那会让"按住拖动"变成果冻。设了系统"减少动态效果"就直给，不滚。 */
    pane.scrollTo({left: pane.scrollLeft + (fr.left + fr.width / 2) - (br.left + br.width / 2),
                   top: pane.scrollTop + (fr.top + fr.height / 2) - (br.top + br.height / 2),
                   behavior: matchMedia("(prefers-reduced-motion:reduce)").matches ? "auto" : "smooth"});
  }
}
function pvSetTools(){
  $("#pvLoc").hidden = !LOC.on;
  $("#pvZoom").hidden = true; $("#pvPdf").hidden = true;
  if (LOC.on) syncZoomCtl();          // 进定位模式就把控件对到存好的偏好上
}
function exitLocate(){
  LOC.on = false; LOC.key = ""; LOC.ll = null; LOC.hits = [];
  if (LOC.url){ URL.revokeObjectURL(LOC.url); LOC.url = ""; }
  PREVIEW.key = "";                            // 强制重画正常票面
  pvSetTools();
  render();
}

/* ── 提取服务 ───────────────────────────────────────────────────────── */
async function probe(){
  try{
    const r = await fetch(BASE + "/health").then(x => x.json());
    /* 版本只写在 config.APP_VERSION，页面照着报：v1.0.0 · V1形态 · 构建日期。
       服务器上没有 .git，所以 built_at（源码最新修改时刻）才是能拿来对账的那一格。 */
    $("#verTag").textContent = "v" + (r.version || "?") + " · " + (r.form || "")
      + " · " + String(r.built_at || "").slice(0, 10);
    $("#verTag").title = "版本 " + (r.version || "?") + "（形态 " + (r.form || "?") + "）\n"
      + "代码构建：" + (r.built_at || "?") + "\n进程启动：" + (r.started_at || "?")
      + (r.commit ? "\ngit：" + r.commit : "\n服务器只收 tar 推的文件，没有 git 仓库");
    /* 服务端跑的是改动前的旧进程时，票面浏览这类新功能会像坏了——直接说清是没重启 */
    const stale = r.stale_files || [];
    $("#svcDot").className = "dot " + (stale.length ? "bad" :
      ((r.key_configured && r.master_key_configured !== false) ? "ok" : "bad"));
    const mMod = r.master_model ? String(r.master_model).split("/").pop() : "";
    $("#svcTxt").innerHTML = "提取服务在线 · 分单模型 <b>" + esc(String(r.model || "").split("/").pop()) + "</b>" +
      (mMod ? " · 主单模型 <b>" + esc(mMod) + "</b>" : "") +
      (r.vision === false ? " · <b class='svc-warn'>纯文本模型：扫描件会失败，电子单/文字层 PDF 可用</b>" : "") +
      (r.key_configured ? "" : " · <b class='svc-bad'>服务端未配当前模型渠道的密钥</b>") +
      (r.master_key_configured === false ? " · <b class='svc-bad'>主单链渠道缺密钥</b>" : "") +
      (stale.length ? " · <b class='svc-bad'>服务端代码改了没重启（" + esc(stale.join("、")) +
        "），票面浏览等功能不生效：关掉服务窗口，重跑 4_启动HTTP服务.bat</b>" : "");
    if (stale.length) toast("提取服务还是改动前的旧进程：关掉服务窗口重跑 4_启动HTTP服务.bat，票面浏览才会生效", "bad");
  }catch(e){
    $("#svcDot").className = "dot bad";
    $("#svcTxt").textContent = "连不上提取服务：先跑 4_启动HTTP服务.bat，再从 http://127.0.0.1:8000/ 打开本页";
  }
}
/* ── 模型切换 ───────────────────────────────────────────────────────── */
async function loadModels(){
  try{
    const r = await fetch(BASE + "/models").then(x => x.json());
    $("#modelSel").innerHTML = modelOpts(r.presets, r.current, r.effective);
    /* 主单链用的是另一个模型（服务端 MASTER_VLM_MODEL）。看不到这一项的话，
       "主单其实换了模型"这件事在界面上完全隐形。 */
    $("#modelSelM").innerHTML = r.master ? modelOpts(r.presets, r.master.choice, r.master.model)
                                         : '<option value="">（服务端旧进程，重启后可见）</option>';
    thinkOpts(r.thinking, r.presets);
  }catch(e){
    $("#modelSel").innerHTML = '<option value="">（服务端旧进程，重启后再试）</option>';
    $("#modelSelM").innerHTML = $("#modelSel").innerHTML;
    $("#thinkSel").innerHTML = '<option value="">（服务端旧进程，重启后再试）</option>';
  }
}
const THINK_MODE = {"": "跟随预设", "0": "关", "1": "开"};
function thinkOpts(th){
  const sel = $("#thinkSel");
  if (!th){ sel.innerHTML = '<option value="">（服务端旧进程，重启后可见）</option>'; sel.disabled = true; return; }
  /* 档位值一律取自服务端这份回报：浏览器自己记一份的话，重启后 .env 说了算，下拉却停在旧档 */
  sel.innerHTML = ["", "0", "1"].map(v =>
    '<option value="' + v + '"' + (v === th.mode ? " selected" : "") + ">" + THINK_MODE[v] + "</option>").join("");
  /* 当前渠道不支持就置灰：能拨却什么都不改的开关，只会被人当成坏了（官方书生多收一个参数就 400） */
  sel.disabled = !th.supported;
  sel.title = th.supported
    ? "当前渠道「" + (th.param || "") + "」生效。开思考更慢、多烧 reasoning token；跟随预设=各家用自己的默认档"
    : "当前渠道不支持思考模式（它不接受这个参数），这个开关拨了也不发任何东西；换到 Qwen 才有用";
}
async function setThinking(sel){
  const v = sel.value;
  sel.disabled = true;
  try{
    const r = await fetch(BASE + "/model/thinking", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({thinking: v})});
    const j = await r.json().catch(() => ({detail:"返回不是 JSON（HTTP " + r.status + "）"}));
    if (!r.ok || !j.ok) throw new Error(j.detail || ("HTTP " + r.status));
    toast("思考档已设为「" + (THINK_MODE[v] || v) + "」" +
          (j.thinking.supported ? (j.thinking.on ? "（开：单票更慢、多烧 reasoning token）" : "（关）")
                               : "（当前渠道不支持，切到 Qwen 才会生效）"), "ok");
  }catch(err){
    toast("切换思考档失败：" + err.message, "bad");
  }finally{
    loadModels();      // 以服务端实际值为准重画（失败了要退回原选项）
  }
}
function modelOpts(presets, cur, effective){
  const list = (presets || []).map(p =>
    '<option value="' + esc(p.key) + '"' + (p.key === cur ? " selected" : "") + ">" + esc(p.label || p.model) + "</option>");
  if (cur && !(presets || []).some(p => p.key === cur)){
    /* .env 里直接写的是原始模型 id：也列出来并选中，否则下拉会显示成另一个模型，看着像切错了 */
    list.unshift('<option value="' + esc(cur) + '" selected>自定义：' + esc(effective || cur) + "</option>");
  }
  return list.join("") || '<option value="">（服务端没给预设）</option>';
}
async function switchModel(sel, chain){
  const v = sel.value;
  if (!v) return;
  sel.disabled = true;
  const who = chain === "master" ? "主单" : "分单";
  try{
    const r = await fetch(BASE + "/model", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({model: v, chain: chain})});
    const j = await r.json().catch(() => ({detail:"返回不是 JSON（HTTP " + r.status + "）"}));
    if (!r.ok || !j.ok) throw new Error(j.detail || ("HTTP " + r.status));
    toast("已切换" + who + "模型：" + (j.label || j.model) +
          (chain === "master" ? "（已缓存的主单解析会按新模型自动重算）" : "") +
          (j.vision ? "" : "（纯文本：扫描件会失败，电子单/文字层 PDF 可用）"), "ok");
  }catch(err){
    toast("切换" + who + "模型失败：" + err.message, "bad");
  }finally{
    sel.disabled = false;
    loadModels(); probe();     // 以服务端实际值为准重画下拉（失败了要退回原选项）
  }
}
$("#modelSel").addEventListener("change", e => switchModel(e.target, "hawb"));
$("#modelSelM").addEventListener("change", e => switchModel(e.target, "master"));
$("#thinkSel").addEventListener("change", e => setThinking(e.target));
async function extract(t){
  const fd = new FormData();
  fd.append("file", t.file, t.filename);
  const r = await fetch(BASE + "/extract", {method:"POST", body:fd});   // 落不落盘不再是前端的事
  const j = await r.json().catch(() => ({ok:false, error:"返回不是 JSON（HTTP " + r.status + "）"}));
  if (r.status === 429) throw new Error((j.detail || j.error || "后端排队已满") + "，稍后再传");
  if (!r.ok && r.status !== 202) throw new Error(j.error || j.detail || ("HTTP " + r.status));
  if (!j.queued) return j;                      // 同步返回的老后端：照旧直接用
  t.job = j.job;                                // 行上那颗 × 要删的是这一张的作业
  /* 浏览器这一侧一进 runQueue 就把票标成"解析中"，可作业号要到这个 202 回来才有。
     有人正好落在那几十毫秒里点了 ×：票已经从列表上没了，上传却已经发出去——
     不补这一发取消，服务器上就多一张没人等的孤儿解析。 */
  if (t.dropped){ await cancelJob(j.job); throw new Error("已从列表里移除"); }
  return await waitJob(j.job, t);
}
/* 解析改成就绪取：入队后轮询 /job。位次变了就刷一行状态，人才知道不是卡住了。
   作业表只在内存里——服务重启后旧作业 404，这里自动重传一次，不再让制单员手动重来。 */
async function waitJob(job, t){
  return await new Promise((res, rej) => {
    poller(async () => {
      if (t.dropped){ rej(new Error("已从列表里移除")); return true; }   // 人把这张删了就别再敲服务器
      const r = await fetch(BASE + "/job/" + encodeURIComponent(job));
      if (r.status === 401){                        // 一直 401 的轮询只会让人以为票丢了：停下并把遮罩亮出来
        $("#gate").hidden = false; rej(new Error("登录已过期，请重新登录后再传")); return true;
      }
      if (r.status === 404){
        if (t._requeued){ rej(new Error("服务重启过，重传后仍未取回结果")); return true; }
        t._requeued = 1;                            // 作业表只在内存里：丢了就自己重传一次，别让人手动重来
        res(await extract(t)); return true;
      }
      const d = await r.json().catch(() => ({}));
      if (d.state === "done"){ res(d.result); return true; }
      if (d.state === "failed"){ rej(new Error(d.error || "解析失败")); return true; }
      t.hint = d.state === "queued" ? ("排队中 · 前面 " + Math.max(0, (d.position || 1) - 1) + " 张") : "";
      if (!t.hint && d.waiting) t.hint = "排队中 · 前面 " + d.waiting + " 张";
      render();
      return false;
    }, {every: 1500, cap: 8000, maxMs: 30 * 60 * 1000,
        giveUp: () => rej(new Error("解析超过 30 分钟未返回（模型或公司接口可能在抖），可点「重试解析」"))});
  });
}
async function runQueue(){
  if (S.running) return;
  S.running = true;
  for (;;){
    const t = S.tickets.find(x => x.state === "queued");
    if (!t) break;
    t.state = "busy"; t.startedAt = Date.now(); render();
    try{
      const j = await extract(t);
      Object.assign(t, {state:"done", stem:j.stem, channel:j.channel, elapsed:j.elapsed,
                        qc:j.qc, raw:j.raw || {}, air:j.air || {}, airE:{}, ackF:[], ackE:[],
                        todoBase:null, ll:j.transcript || null});
      if (S.sel === t.filename) S.sel = t.stem;
      saveDraft(t);
      hintLocate();          // 第一次解析成功时提一次「点字段名能定位」，之后不再啰嗦
    }catch(e){
      if (t.dropped) continue;        // 删掉的这张不再写状态：它已经不在列表里，写成"失败"只会让人以为解析坏了
      t.state = "failed"; t.error = String(e.message || e);
    }
    render();
  }
  S.running = false;
}
async function retryParse(t){
  if (!t.file){ toast("这张票没有原始文件可重试（可能是载入的历史暂存），请重新上传", "bad"); return; }
  t.state = "queued"; t.error = null;
  render(); runQueue();        // 队列在跑就只是补进队列，runQueue 自身幂等
}
function addFiles(files){
  const bad = [];
  for (const f of files){
    const ext = "." + (f.name.split(".").pop() || "").toLowerCase();
    if (!EXTS.includes(ext)){ bad.push(f.name); continue; }
    S.tickets.push({file:f, filename:f.name, state:"queued", raw:{}, air:{}, airE:{}, qc:null});
  }
  if (bad.length) toast("已忽略不支持的类型：" + bad.slice(0, 3).join("、") + (bad.length > 3 ? " 等" : ""), "bad");
  const first = S.tickets.find(t => t.state === "queued");
  if (first && !S.sel) S.sel = first.filename;
  render(); runQueue();
}

/* ── 提交 ───────────────────────────────────────────────────────────── */
/* 暂存：把现在这份核对结果存到服务器上给同事看，并在公司那边占个位（状态 0，可反复改）。
   提交发送那一档永远只有人工点「提交」才走，见 stageMsg 与 submitTicket。
   与提交分开是需求二的核心——两档的门槛本来就不一样：缺号、红旗没清都能存
   （那正是需要同事接着看的票），而提交必须两样都过。姓名只是留个称呼，
   服务器认的是登录会话里的身份。 */
/* 暂存的回执要说两件事：本机这份存没存上（同事能不能接着看）、公司那边占上位没有。
   公司那一发失败不回滚本机这份，所以只补一句原因，不整条报"失败"——
   报失败会让人以为白点了，又去点一次，而白点的其实只是公司那半。
   锁行（状态 1/2）更不是失败：那一行公司本来就不收了，这是**预期结局**，
   说成失败是 2026-10-14 公司那台截图里最误导人的两个字。 */
function stageMsg(n, c){
  const base = "已暂存（改了 " + (n || 0) + " 列）：同事检索这条单号就能看到";
  if (!c) return base;
  if (c.ok && c.mode === "live") return "已暂存并寄到公司（状态 0，还能继续改）：改了 " + (n || 0) + " 列";
  if (c.ok) return base + "；公司那边没发（本机 mock 模式）";
  if (c.locked) return base + "；这条在公司已经离开暂存档（状态 " + (c.status || "?") + "），不再收改动";
  return base + "；" + (c.skipped || "公司暂存失败：" + (c.error || "未知原因"));
}
async function stageTicket(t){
  const who = $("#reviewer").value.trim();
  if (!who){ toast("先填复核人姓名：暂存也要留是谁核对的", "bad"); $("#reviewer").focus(); return; }
  try{
    const r = await fetch(BASE + "/stage", {method:"POST", headers:{"Content-Type":"application/json"},
                                           body:JSON.stringify({tickets:[payloadFor(t, who)]})});
    const j = await r.json().catch(() => ({ok:false}));
    if (!r.ok || j.ok === false){
      const d = j.detail;
      throw new Error((d && (d.message || (typeof d === "string" ? d : ""))) || j.error || ("HTTP " + r.status));
    }
    const n = (j.staged && j.staged[0] && j.staged[0].edited) || 0;
    const c = j.staged && j.staged[0] && j.staged[0].company;
    t.staged = {at: nowTxt(), by: who, edited: n};
    /* 公司锁了行就把它记在票上：下一次不用又点一次提交才发现改不动了。
       锁行那一档气泡走中性色——绿色说"全成了"、红色说"白点了"，两样都不对。 */
    t.locked = c && c.locked ? (c.status || 1) : "";
    render();
    afterWrite(t, false);
    if (t.locked) toast(stageMsg(n, c), "");
    else toast(stageMsg(n, c), "ok");
  }catch(e){ toast("暂存失败：" + (e.message || e), "bad"); }
}
async function submitTicket(t){
  const who = $("#reviewer").value.trim();
  if (!who){ toast("先填复核人姓名：提交要留痕", "bad"); $("#reviewer").focus(); return; }
  // 提交门（前端体验层，服务端另有一道只认主/分单号的硬门）：
  //   1) 主/分单号是"录入员日后按号打开本机原件"的唯一连接键，空号一律不许提交；
  //   2) 还挂着红旗（含缺号那条）也不许提交——补全/清完再走。
  const _air = reviewed(t);
  if (!String(_air.MAWB_NO || "").trim() || !String(_air.HAWB_NO || "").trim()){
    toast("主单号、分单号都得填上才能提交回传公司", "bad"); return;
  }
  const remaining = effectiveFlags(t);
  if (remaining.length){
    toast("还有未清的红旗：先逐条处理完才能提交（" + remaining[0] + (remaining.length > 1 ? " 等 " + remaining.length + " 条" : "") + "）", "bad"); return;
  }
  const p = payloadFor(t, who);
  const body = {tickets:[p], submitted_at:nowTxt(), client:"hawb-review-desk/1.0"};
  t.state = "submitting"; render();
  let pending = false, notes = [];
  try{
    const r = await fetch(SUBMIT_URL, {method:"POST", headers:{"Content-Type":"application/json"},
                                       body:JSON.stringify(body)});
    if (r.status === 404 || r.status === 405) pending = true;
    else {
      const j = await r.json().catch(() => ({ok:false, error:"返回不是 JSON（HTTP " + r.status + "）"}));
      notes = Array.isArray(j.notes) ? j.notes : [];
      if (!r.ok || j.ok === false){
        // 服务端 400 的 detail 可能是字符串（缺号），也可能是 {message, flags}（红旗未清）
        // 或 {message, ack}（主单还没提交发送，2026-10-14 加的第三道门）：不把 message 挖出来，
        // 制单员只会看到光秃秃一句 "HTTP 400"，不知道该改哪。
        const d = j.detail;
        if (d && d.ack === "master_first"){
          /* 顺序这一道（2026-10-14 用户定案：先提交主单，再提交分单）：服务器说这张分单名下
             的主单还没提交发送。这里只负责把人带过去——主单那颗提交永远是人工那一下，
             不许在这条路径上替他点。 */
          t.state = "done"; render();
          if (confirm((d.message || "这张分单的主单还没提交发送。") + "\n\n点「确定」过去交主单，回来再提交这张分单。"))
            gotoMaster(String(d.mawb || reviewed(t).MAWB_NO || ""));
          return;
        }
        if (d && Array.isArray(d.flags) && d.flags.length){
          /* 服务端点名了是哪几条旗：本地 qc.flags 那份可能根本没有它（改了值旧旗文案会变，
             重算出来的是新文案）。接住并并进待确认列表，人才有点得到的机会。 */
          const known = ((t.qc || {}).flags || []).concat(t.srvFlags || []);
          t.srvFlags = (t.srvFlags || []).concat(d.flags.filter(f => !known.includes(f)));
          t.state = "done"; saveDraft(t); render();
          toast((d.message || "还有未清的红旗") + "：" + d.flags[0] +
                (d.flags.length > 1 ? " 等 " + d.flags.length + " 条" : ""), "bad");
          return;
        }
        throw new Error((d && (d.message || (typeof d === "string" ? d : ""))) || j.error || ("HTTP " + r.status));
      }
    }
  }catch(e){ if (!pending) { t.state = "done"; render(); toast("提交失败：" + e.message, "bad"); return; } else pending = false; }
  if (pending){
    t.state = "done"; t.submitted = {mode:"pending", at:null, body:body};
    saveDraft(t); render();
    toast("提交接口尚未开通（" + SUBMIT_URL + "）：结果只留在这台浏览器里，可导出 JSON 兜底", "bad");
    return;
  }
  t.state = "done"; t.submitted = {mode:"server", at:nowTxt()};
  /* 这一发里没确认就发出去的那些空栏，服务器按 skipped_empty 留痕；本机照同一档收起来。
     刚点完提交还催「仍有 N 项待核对」是最容易被当成"按钮没生效"的一种话。 */
  t.skipE = [...new Set((t.skipE || []).concat(ticketTodo(t).todo.keys))];
  dropDraft(t.stem); render(); toast("已提交：" + t.stem, "ok");
  afterWrite(t, true);
  /* 回执里服务端随带的话要露头（比如"没读到这条主单在公司侧的状态，这次按你说的走"）：
     它没让提交失败，但也不等于验过了——不说，人就会以为顺序已经替他把过关。 */
  notes.forEach(n => toast(String(n), ""));
}
function exportTickets(list, name){
  if (!list.length){ toast("没有可导出的票据", "bad"); return; }
  const json = JSON.stringify({reviewer:$("#reviewer").value.trim(), exported_at:nowTxt(),
                               tickets:list.map(t => payloadFor(t, $("#reviewer").value.trim()))}, null, 2);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([json], {type:"application/json"}));
  a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  toast("已导出 " + list.length + " 票", "ok");
}

/* ── 渲染 ───────────────────────────────────────────────────────────── */
function statusOf(t){
  if (t.state === "queued") return ["", "排队中"];
  if (t.state === "busy") return ["busy", t.hint || "解析中…"];
  if (t.state === "failed") return ["bad", "失败"];
  if (t.state === "submitting") return ["busy", "提交中…"];
  /* 刷新后种回来的那一行：完整那份还没取（取它要读三个 JSON），但号与走到哪一步服务器知道。
     标签只能用已载入那些行会说的那几个，否则同一张票点前点后换一套词，人以为换了张票。 */
  if (t.state === "seed") return t.seed.submitted ? ["ok", "已提交"]
       : (t.seed.flags ? ["warn", "需复核"] : ["ok", "干净"]);
  if (t.submitted && t.submitted.mode === "server") return ["ok", "已提交"];
  if (t.submitted) return ["warn", "待提交"];
  if (effectiveFlags(t).length) return ["warn", "需复核"];
  return ["ok", "干净"];
}
/* 「这张还要不要人核」只在这一处判：计数、窄轨、跳下一票用的是同一个答案。
   已经交出去的一律不再算待办——种子行与已载入的行必须同一个判据，否则同一张票点前点后
   「需复核 N」会跟着变，人就看到数字莫名其妙跳一下。 */
function needsReview(t){
  if (t.state === "seed") return !t.seed.submitted && t.seed.flags > 0;
  if (t.state !== "done" || (t.submitted && t.submitted.mode === "server")) return false;
  return ticketTodo(t).todo.n > 0;
}
function current(){ return S.tickets.find(t => t.stem === S.sel) || S.tickets.find(t => t.filename === S.sel); }
/* 行上那颗 × 说的是三种不同的事，标题就得说三种：排队中的还没传出去（本机队列是一张一张发的），
   在跑的那一张服务器掐不掉（模型调用与 LibreOffice 子进程都在 worker 手里，硬杀会把解析到一半
   的票留在盘上），失败的那一张只是别再碍眼。写成一句"已删除"就是三句里两句假话。 */
function dropTitle(t){
  if (t.state === "queued") return "还没开始解析：从列表里拿掉，也不会传去服务器";
  if (t.state === "busy") return "这张已经在解析了：删掉只是不再取它的结果（解析掐不掉，钱已经花了）";
  return "从列表里拿掉（服务器上的解析结果不动，要彻底删除走「数据清理」）";
}
/* 取消一张作业：三种结局（cancelled / running / gone）由服务器说，请求本身失败算第四种。
   这里不写界面话术——调用方有两处，要说的话不一样：行上那颗 × 要报给本人，
   而"删在上传与作业号之间"那一发是补锅，人不该再看到一条消息。 */
async function cancelJob(job){
  try{
    const r = await fetch(BASE + "/job/" + encodeURIComponent(job) + "/cancel", {method: "POST"});
    const j = await r.json().catch(() => ({}));
    return {state: r.ok ? (j.state || "gone") : "error"};
  }catch(e){ return {state: "error"}; }
}
async function dropRow(i){
  const t = S.tickets[i];
  if (!t) return;
  const job = t.job;
  t.dropped = true;                       // 轮询靠它收手：不然这张的 poller 会一直敲到 30 分钟上限
  S.tickets.splice(i, 1);
  if (S.sel === (t.stem || t.filename))
    S.sel = S.tickets.length ? (S.tickets[Math.max(0, i - 1)].stem || S.tickets[Math.max(0, i - 1)].filename) : null;
  render();
  if (!job) return;                       // 还没传出去的（本机队列一张一张发）：删了就是删了
  const j = await cancelJob(job);
  /* 三句话三张皮（类别只认 .toast.bad/.toast.ok）：掐掉了是好消息；
     "在跑、掐不掉"要停留得够久——它纠正的是人刚刚形成的误解。 */
  if (j.state === "cancelled") toast("已取消那张排队中的解析", "ok");
  else if (j.state === "running") toast("这张正在解析，掐不掉——已从列表里拿掉，结果不再取", "bad");
  else if (j.state === "error") toast("已从列表里拿掉，但没能通知服务器取消这一张的解析", "bad");
  else toast("已从列表里拿掉（服务器上早就没有这一张作业）");
}
function render(){
  const done = S.tickets.filter(t => t.state === "done" || t.state === "seed").length;
  const rev = S.tickets.filter(needsReview).length;
  $("#cnt").textContent = S.tickets.length + " 票 · 已解析 " + done + (rev ? " · 需复核 " + rev : "");
  const nq = $("#tabQueueN"); if (nq) nq.textContent = rev ? " " + rev : "";   // 页签上的数字＝还要复核几票
  $("#list").innerHTML = S.tickets.length ? S.tickets.map((t, i) => {
    const [c, label] = statusOf(t), n = editedList(t).length;
    const isCur = (t.stem && t.stem === S.sel) || (!t.stem && t.filename === S.sel);
    /* 还没成票的那三档（排队 / 在跑 / 失败）在行上直接给一颗删除：列队里点错了、
       或者一次塞了十张想先做别的，不该只能干等它排到。已经解析好的走操作条那颗
       「从列表移除」——那一格改过值，随手 × 掉太容易丢东西。
       button 不能套 button，所以删除是行的兄弟节点，不是它的孩子。 */
    const x = ["queued", "busy", "failed"].includes(t.state)
      ? `<button type="button" class="rowx" data-drop="${i}" title="${dropTitle(t)}">×</button>` : "";
    return `<span class="rowline"><button type="button" class="row${isCur ? " sel" : ""}" data-i="${i}"${isCur ? ' aria-current="true"' : ""}>
      <span class="dot ${c}" aria-hidden="true"></span>
      <span class="nm">${esc(t.filename)}<br><span class="m">${esc(t.stem || "等待解析")}</span></span>
      <span class="st">${label}${n ? `<br><span class="st-edited">改 ${n} 项</span>` : ""}</span></button>${x}</span>`;
  }).join("") : `<div class="empty">尚无票据。</div>`;
  renderRail();
  /* 停在一张有 stem 的票上才记：刚上传还没解析完的那一行是按文件名选中的，
     记下它等于下次刷新去问服务器要一个不存在的 stem。 */
  const cur = current();
  if (cur && cur.stem) localStorage.setItem(LS.open, cur.stem);
  renderMain(); renderDrafts(); renderPreview();
}
/* ── 状态窄轨（§3.3）：一票一个点 + 本机"已提交/经手"进度 ─────────────────────
   换票动作从"回侧栏滚动找那一行"变成点一下；≡ 展开成覆盖式列表（只有单号与状态，
   不是侧栏那张卡的复制品——那张有"改 N 项/未提交"这些窄轨不需要的东西）。 */
const RAIL = {open: false};
function railLabel(t){
  return (t.hawb || t.stem || t.filename) + "：" + statusOf(t)[1];
}
function renderRail(){
  const box = $("#railDots"); if (!box) return;
  const done = S.tickets.filter(t => t.submitted).length;
  const prg = $("#railPrg");
  prg.textContent = S.tickets.length ? (done + "/" + S.tickets.length) : "—";
  prg.title = "本机今天：已提交 " + done + " / 经手 " + S.tickets.length + " 张";
  box.innerHTML = S.tickets.map((t, i) => {
    const cur = (t.stem && t.stem === S.sel) || (!t.stem && t.filename === S.sel);
    return `<button type="button" class="rd" data-i="${i}"${cur ? ' aria-current="true"' : ""}
      aria-label="${esc(railLabel(t))}"><span class="dot ${statusOf(t)[0]}" aria-hidden="true"></span></button>`;
  }).join("");
  const old = document.querySelector(".railList"); if (old) old.remove();
  $("#railExpand").setAttribute("aria-expanded", String(RAIL.open));
  if (!RAIL.open) return;
  const panel = document.createElement("div");
  panel.className = "railList";
  panel.innerHTML = S.tickets.length ? S.tickets.map((t, i) => {
    const cur = (t.stem && t.stem === S.sel) || (!t.stem && t.filename === S.sel);
    return `<button type="button" class="rr" data-i="${i}"${cur ? ' aria-current="true"' : ""}>
      <span class="dot ${statusOf(t)[0]}" aria-hidden="true"></span>
      <span class="nm">${esc(t.hawb || t.stem || t.filename)}</span>
      <span class="st">${esc(statusOf(t)[1])}</span></button>`;
  }).join("") : '<div class="empty">尚无票据。</div>';
  $("#rail").appendChild(panel);
}
/* 上一票 / 下一票：核完一张顺着走。换票要先退出主单视图，否则核对区还停在主单那张 36 列表上，
   人以为"点了没反应"。到头了要说一声——静默不动最容易被当成坏了。 */
function stepTicket(d){
  if (!S.tickets.length){ toast("列表里还没有票", ""); return; }
  const i = S.tickets.indexOf(current());
  if (i < 0){ mvOff(); S.sel = S.tickets[0].stem || S.tickets[0].filename; render(); return; }
  const j = i + d;
  if (j < 0 || j >= S.tickets.length){ toast(d < 0 ? "已经是第一张" : "已经是最后一张", ""); return; }
  mvOff();
  S.sel = S.tickets[j].stem || S.tickets[j].filename;
  render();
  $("#main").scrollIntoView({block: "nearest"});
}
/* 交出去一张，下一张**票**在哪儿不该让人自己在列表里找（2026-10-15 用户：点完暂存或提交，
   这里一定得更新，不然都不知道下一票处理哪个了）。顺着队列找第一张还要核的——解析中/排队中
   的那张不算"要核"（它还没好），一张都没有就明说一句，别让人以为按钮没反应。
   名字与 gotoNextTodo（一张票里跳到下一个待办**字段**）是两件事，别混成一个。 */
function gotoNextTicket(){
  const i = S.tickets.indexOf(current());
  for (let k = 1; k <= S.tickets.length; k++){
    const t = S.tickets[(i + k + S.tickets.length) % S.tickets.length];
    if (!needsReview(t)) continue;
    mvOff();
    S.sel = t.stem || t.filename;
    render();
    toast("下一票：" + (t.hawb || t.stem || t.filename) + "（" + statusOf(t)[1] + "）", "");
    return;
  }
  toast("队列里没有还要核的票了", "ok");
}
/* 名下分单那张表读的是服务器：公司的行 + 盘上的暂存与提交。本机的状态改不动它，
   所以交出去一张之后要按那个主单号再检索一次，人才看得见这一条从「公司暂存（未发送）」
   变成「已提交发送·等外围」。检索一次 = 一次只读查询，解析那边命中缓存不再花钱。 */
function mstRefresh(){
  if (!MST_LAST || !MST_LAST.mawb) return;
  $("#mstNo").value = MST_LAST.mawb;
  mstSearch();
}
/* 暂存/提交之后要回账的两处：台账（在这一屏时才重画）与打开着的那张名下分单表。
   判据是「这张票的主单号就是表上那个号」——不对得上就别替人敲公司接口。 */
function afterWrite(t, sent){
  if (VIEW.cur === "day") loadDay(false);
  const mawb = String((t && ((t.air || {}).MAWB_NO || t.mawb)) || "");
  if (MST_CARD_ON && MST_LAST && L.normNo(MST_LAST.mawb) === L.normNo(mawb)) mstRefresh();
  if (sent) gotoNextTicket();
}
function renderDrafts(){
  draftFlush();                       // 这一栏画的是"盘上那份"：先落盘再画，屏上才等于留痕
  const d = drafts();
  const keys = Object.keys(d).filter(k => !S.tickets.some(t => t.stem === k));
  $("#drafts").innerHTML = keys.length ? keys.map(k => {
    const t = d[k], n = Object.keys(t.airE || {}).length;
    return `<button type="button" class="row" data-load="${esc(k)}">
      <span class="dot warn" aria-hidden="true"></span>
      <span class="nm">${esc(k)}<br><span class="m">${esc(t.savedAt || "")} · 改 ${n} 项 · 未提交</span></span>
      <span class="st">载入</span></button>`;
  }).join("") : `<div class="empty">编辑会自动存在本机浏览器，防刷新丢失；提交成功这一条就删掉了，要看交出去的去今日归档。</div>`;
  const nb = $("#tabDraftN"); if (nb) nb.textContent = keys.length ? " " + keys.length : "";
}
/* 左栏三页签（队列 / 主单检索 / 未保存的编辑）：从前这三张卡竖着堆，要看下面那张得先把上面滚走，
   而这三样本来就不同时用。页签只决定哪一块看得见，上传、检索、草稿的逻辑一行没动。 */
function railTab(name){
  RAILTAB.cur = name;
  for (const k of ["queue", "mawb", "draft"]){
    const cap = k[0].toUpperCase() + k.slice(1), on = k === name;
    const pan = $("#tab" + cap), btn = $("#railTab" + cap);
    if (pan) pan.hidden = !on;
    if (btn) btn.setAttribute("aria-selected", String(on));   // 读屏器靠它念"当前在哪一页"
  }
}
function control(t, k){
  const v = airVal(t, k);
  /* 一律 textarea：原生斜角只长在 textarea 上，短字段从前用 input 标签就没有可拖的角
     （2026-10-09 用户："可以自由拖拽改变大小的"）。单行那些标 .one：挡回车、按内容长高。 */
  const one = LONG.has(k) ? "" : ' class="one"';
  return `<textarea data-k="${k}" rows="1"${one}` + (NUM[k] ? ` inputmode="decimal"` : "") + `>${esc(v)}</textarea>`;
}
/* 单行那些换成了 textarea，就得把回车挡住：这个值原样写进公司系统，换行混进 MAWB 号或件重
   就是脏数据，而且在单行框里换行看不见（改完的人以为只有一行）。
   回车另外兼一件事：跳到下一个待办——核对是一格一格走的，手不该离开键盘。
   Shift+Enter 留给出真要换行的那一格；回车绝不提交：提交不可逆，只能点按钮那一下。 */
function blockEnter(e){
  if (e.key !== "Enter" || e.shiftKey) return;
  if (!e.target.classList.contains("one")) return;      // 长文本那几格：回车就是换行
  e.preventDefault();
  const k = e.target.dataset.k;
  if (!k) return;                                        // 主单表（data-mk）那一套待办口径不同，不接
  const t = current(); if (!t) return;
  const st = L.fieldStatus({flags: 0, acked: false, raw: txt(t.raw, k), air: airVal(t, k)});
  if (st.s === "empty"){                                 // 回车=「这一格我看过了」：空着的就顺手确认掉
    t.ackE = t.ackE || [];
    if (!t.ackE.includes(k)) t.ackE.push(k);
  }
  saveDraft(t);
  gotoNextTodo(t, k);
}
/* 长文本框高度跟着内容走：以前按 "\n" 数行数，收发货人整串是一整段没有换行，
   再长也只有 1 行高，看着别扭；改成量实际渲染高度（封顶后出滚动条）。
   一批格子走 growAll：写 auto → 读 scrollHeight → 写回 分三段做。逐格三步的话每读一次
   都逼一次同步布局，一次渲染 38 格＝38 次 reflow（2026-10-16 效率审查 F3）；
   先全部写完再统一读，整批只逼一次。 */
function growAll(els){
  const fh = fieldHeights();          // 本机列高偏好：一次渲染读一遍，不随格子数重读
  const need = [];
  els.forEach(el => {
    /* 宽不是单格的属性（斜角拖那一下改的是整列），所以每次量高度前先把 inline 宽交还给列：
       留着它，这一格就从此钉死在自己那个宽度上，别人再调列宽也调不到它。 */
    el.style.width = "";
    /* 人拖过的那一列以拖的为准：从前这里每次渲染与每一下输入都按内容重设高度，
       拖完一打字就弹回去——"能拖"在用户那儿等于"不能自定义"（2026-10-09）。 */
    const k = el.dataset.k || el.dataset.mk || "";
    const rec = k ? fh[k] : 0;
    if (rec) el.style.height = L.fieldHeight(rec, 0) + "px";
    else { el.style.height = "auto"; need.push(el); }
  });
  const hs = need.map(el => L.fieldHeight(0, el.scrollHeight + 2));
  hs.forEach((h, i) => { need[i].style.height = h + "px"; });
}
function autoGrow(el){ growAll([el]); }
/* 自定义的框高按列记（不分票）：地址那格想一直高一点，不该每张票重拖一次。 */
function fieldHeights(){
  let o = {};
  try{ o = JSON.parse(localStorage.getItem(LS.fieldH) || "{}") || {}; }catch(e){ o = {}; }
  return (o && typeof o === "object" && !Array.isArray(o)) ? o : {};
}
function saveFieldHeight(k, h){
  const o = fieldHeights();
  if (h) o[k] = h; else delete o[k];
  localStorage.setItem(LS.fieldH, JSON.stringify(o));
}
/* 原生 resize 不触发任何事件：只能按下时把宽高与栏位尺寸一起记下、松开时比对，变了就是人拖的。
   斜角拖的看起来是"这一格"，但值是一整列：宽那一下换算成整列份额（票面栏让路），随后把 inline 宽擦掉——
   留着它这一格从此不跟列宽变，下次调宽就把它丢在原处（2026-10-09 用户要"可以自由拖拽改变大小的"）。 */
let FH_DRAG = null;
document.addEventListener("pointerdown", e => {
  const el = e.target.closest && e.target.closest("textarea[data-k], textarea[data-mk]");
  if (el) FH_DRAG = [el, el.offsetHeight, el.offsetWidth, deskGeom()];
});
document.addEventListener("pointerup", () => {
  if (!FH_DRAG) return;
  const el = FH_DRAG[0], h0 = FH_DRAG[1], w0 = FH_DRAG[2], geo = FH_DRAG[3];
  FH_DRAG = null;
  const k = el.dataset.k || el.dataset.mk;
  if (!k) return;
  let touched = false;
  if (el.offsetHeight !== h0){ saveFieldHeight(k, el.offsetHeight); touched = true; }
  const dw = el.offsetWidth - w0;
  if (dw){
    el.style.width = "";
    saveDeskShare(L.boxWidthPct(geo[0], dw, geo[1], 260));
    touched = true;
  }
  if (touched) syncFhReset();
});
/* 恢复的出口：放在待办条 / 主单工具条上，不用双击格子——双击在文本框里是"选词"，
   拿它当复位会把人正在改的内容顺手弄回自动高度。 */
function fhResetHtml(){
  const n = Object.keys(fieldHeights()).length;
  return n ? `<button class="btn sm" type="button" data-fhreset title="把 ${n} 列的框高恢复成按内容自动撑">框高恢复（${n}）</button>` : "";
}
/* 表头那道把手：拖的是「字段名」这一列的宽，整列 40 行一起变（表格是 fixed 布局，
   一格一格横向拖只会溢出自己的格子，别骗人）。 */
/* 拖完当场把槽位刷出来：不重画整张表（那会弄丢正在编辑那一格的光标），只补这一小块。 */
function syncFhReset(){
  const html = fhResetHtml();
  document.querySelectorAll(".fhslot").forEach(el => { el.innerHTML = html; });
}
function gripColHtml(){
  return '<div class="grip gripCol" role="separator" aria-orientation="vertical" tabindex="0" '
    + 'title="按住左右拖：调字段名列的宽窄，整列一起变（方向键同样可以）"></div>';
}
/* 值格右边界那道：用户 2026-10-09 明确要"单纯地改变文本框的宽度"，别让他去找页面上别的把手。
   原生 resize 给不了——它对 <input> 不生效，而值列里一半是 input：长文本能拖宽、短文本不能，
   比都不能拖更让人以为坏了。所以自己画一条，textarea 与 input 都一样有。 */
function edgeGripHtml(){
  return '<div class="edg" role="separator" aria-orientation="vertical" tabindex="0" '
    + 'title="按住左右拖：这一列的框拉宽/推窄（宽是从票面栏那头要的，←/→ 同样可以）"></div>';
}
function resetFieldHeights(){
  const n = Object.keys(fieldHeights()).length;
  localStorage.removeItem(LS.fieldH);
  growAll(document.querySelectorAll("textarea[data-k], textarea[data-mk]"));
  render();
  toast(n ? `已恢复 ${n} 列的框高为自动` : "没有自定义过的框高", "ok");
}
/* 一列的六态（口径全在 logic.js 的 fieldStatus 里，这里只负责把它喂对）。
   flags 给的是"这一列红旗总数（含人已确认的）"，acked 给的是"该确认的都确认了"：
   补好的缺号那类自己消掉的旗不算 acked——没人确认过它，不该显示成「已确认」，更不该配一颗撤回。 */
function fieldRows(t, ef){
  const all = flagsByField(t.qc).map, left = flagsByField({flags: ef || effectiveFlags(t)}).map;
  const ackF = t.ackF || [], ackE = t.ackE || [], skipE = t.skipE || [];
  return FIELDS.map(([k, lab, g]) => {
    const list = all[k] || [], out = left[k] || [];
    const remain = out.length;
    const manual = list.filter(f => ackF.includes(f)).length;
    const hasFlag = remain + manual;
    /* 输入原样留在行上（flags/acked/raw/air）：logic.js 那套 todoOf/progress 是拿同一批输入重算的，
       只带一个算完的 st 进去，它会把这些行统统当成"空值待办"——进度分母当场变成 38（浏览器里量出来的）。
       fl/fa 是旗面原文：顶部那排 chip 撤了之后，"这条旗到底说了什么"只剩行上这一格会说。
       skip 是重开一张提交过的票时才有的那一档：这一列当时空着、人点第二下硬发了出去。 */
    const o = {k, lab, g, flags: hasFlag,
               fl: out.filter(f => !ackF.includes(f)), fa: list.filter(f => ackF.includes(f)),
               acked: hasFlag ? remain === 0 && manual > 0 : ackE.includes(k),
               skip: !hasFlag && !ackE.includes(k) && skipE.includes(k),
               raw: txt(t.raw, k), air: airVal(t, k)};
    o.st = L.fieldStatus(o);
    return o;
  });
}
/* 一张票"还剩哪些事"只有这一个算法入口：行上的旗 + 空栏 + 没有列可挂的旗。
   从前同样两行抄了五处（提交按钮、武装那一下、待办条、跳下一个、跳首个），
   少改一处就是待办条说的和提交门数的不是同一件事。 */
function ticketTodo(t){
  const ef = effectiveFlags(t), rows = fieldRows(t, ef);
  return {ef, rows, todo: L.todoOf(rows, flagsByField({flags: ef}).rest)};
}
const BADGE_CLS = {flag: "warn", confirmed: "conf", empty: "emptyb", emptyok: "conf",
                   skipped: "same", miss: "miss", same: "same"};
function rowCls(r, t){
  const s = r.st.s;
  return s === "flag" ? "flag" : s === "empty" ? "emptyrow"
       : s === "confirmed" || s === "emptyok" ? "acked"
       : s === "miss" ? "missrow" : (r.k in t.airE) ? "edited" : "";
}
/* 段头是"本段核到什么程度"，粘在自己那张表的 thead 里（<tr> 上写 sticky 粘不住，包含块就是那一行）。
   红旗段比空值段重：两处都有事时段头保持红，不许被抹平成琥珀。 */
function segHeadHtml(seg){
  const warn = seg.hard ? `<span class="sflag">⚑ ${seg.hard} 处红旗要核票面</span>`
             : seg.soft ? `<span class="ssoft">${seg.soft} 个空栏待确认</span>`
             : `<span class="sok">✓ 本段已清</span>`;
  const folded = FOLD.has(seg.g);
  return `<tr class="grp ${seg.state}">`
    + `<th class="kcol"><span class="in"><button class="fold" type="button" data-fold="${seg.g}"`
    + ` aria-expanded="${!folded}" title="折叠/展开这一段">${folded ? "▸" : "▾"}</button>`
    + `<span class="gn">${esc(GROUPS[seg.g] || seg.g)}</span>${gripColHtml()}</span></th>`
    + `<th><span class="in"><span class="cnt">${seg.cleared}/${seg.tot}</span>${warn}</span></th>`
    + `<th class="scol">` + (seg.todo ? `<button class="btn sm" type="button" data-gack="${seg.g}"`
        + ` title="这一段里的红旗与空栏都逐条对照票面看过了 → 一次点完（留痕与逐条点完全一样）">整段核对 ${seg.todo} 项</button>` : "")
    + `</th></tr>`;
}
function statusCell(t, r){
  const k = r.k, s = r.st.s;
  const ck = checks(k, r.raw, r.air);
  const acts = [];
  if (s === "flag") acts.push(`<button class="btn sm danger" type="button" data-ackf="${esc(k)}" title="这一列的红旗全部确认（与「整张票」那一格逐条确认同一个留痕）">红旗已核</button>`);
  else if (s === "confirmed") acts.push(`<button class="btn sm gh" type="button" data-unackf="${esc(k)}" title="撤回：这一列的红旗回到待办">撤回</button>`);
  if (s === "empty" || s === "skipped") acts.push(`<button class="btn sm g" type="button" data-acke="${esc(k)}" title="票面本来就没印这一格 → 确认「本就为空」，它不再算待办，提交时按 acked_empty 留痕">本就为空</button>`);
  else if (s === "emptyok") acts.push(`<button class="btn sm gh" type="button" data-une="${esc(k)}" title="撤回：这一格回到待办">撤回</button>`);
  const edits = (k in t.airE) ? `<div class="note"><span class="e">已改</span>
      <button type="button" class="rs" data-restore="${esc(k)}">还原</button></div>` : "";
  const notes = ck.slice(0, 3).map(x => `<div class="note"><span class="${x[1] === "bad" ? "b" : "q"}">${esc(x[0])}</span></div>`).join("");
  return `<span class="badge ${BADGE_CLS[s]}">${esc(r.st.label)}</span>`
    + (acts.length ? `<div class="acts">${acts.join("")}</div>` : "") + edits + notes;
}
/* 红旗原文就写在它指的那一格底下（2026-10-13 用户：以后上面都不要了，要改的红圈都放在字段后面）。
   确认过的也留着、灰一档：人确认过什么，界面上就该看得见什么。 */
function flagLines(r){
  const now = (r.fl || []).map(f => `<div class="flagline">${esc(f)}</div>`).join("");
  const done = (r.fa || []).map(f => `<div class="flagline ack">已确认：${esc(f)}</div>`).join("");
  return `<div class="flags">${now}${done}</div>`;
}
/* 一条列名都不点的红旗（说的是整张票）没有哪一行挂得上去，但它**必须有落点**：
   看不见又点不到的旗就是死旗，提交门照样会因为它退回来（v1.17.2 为这个修过一次，
   v1.20.0 又修过一次）。所以在字段表末尾给它留一段「整张票」，确认/撤回与原来那颗一模一样。
   顶部那排 chip 撤掉之后，这里是它唯一的家。 */
function orphanRows(t, rest, ackF){
  return rest.map(f => {
    const done = ackF.includes(f);
    return `<tr class="${done ? "acked" : "flag"}" data-hov="orphan">
      <td class="k"><span class="kk">整张票</span><span class="ky">不落在某一列</span></td>
      <td class="c"><div class="flagline${done ? " ack" : ""}">${done ? "已确认：" : ""}${esc(f)}</div></td>
      <td class="s"><div class="acts">` + (done
        ? `<button class="btn sm gh" type="button" data-unack="${esc(f)}" title="撤回这条确认：它回到待办">撤回</button>`
        : `<button class="btn sm danger" type="button" data-ack="${esc(f)}"`
          + ` title="票面已核对无误 → 确认这条提示，提交放行（留痕进台账）">确认无误</button>`) + `</div></td></tr>`;
  }).join("");
}
function orphanTable(t, ef){
  const ackF = t.ackF || [];
  const out = flagsByField({flags: ef}).rest;
  const done = ((((t.qc || {}).flags) || []).filter(f => ackF.includes(f)))
    .filter(f => !L.flagKeys(f, KEYS).length && out.indexOf(f) < 0);
  if (!out.length && !done.length) return "";
  return `<table class="gt" id="gt-orphan">`
    + `<colgroup><col class="kcol"><col class="vcol"><col class="scol"></colgroup>`
    + `<thead><tr class="grp ${out.length ? "hasflag" : ""}"><th class="kcol"><span class="in">`
    + `<span class="gn">整张票</span></span></th><th><span class="in">`
    + `<span class="${out.length ? "sflag" : "sok"}">${out.length ? "⚑ " + out.length + " 条不落在某一列" : "✓ 都确认过了"}</span>`
    + `</span></th><th class="scol"></th></tr></thead>`
    + `<tbody>${orphanRows(t, out, ackF)}${orphanRows(t, done, ackF)}</tbody></table>`;
}
function fieldRowHtml(t, r, hide){
  return `<tr id="f-${r.k}" class="${rowCls(r, t)}" data-hov="${r.k}" data-todo="${r.st.todo ? 1 : 0}"${hide ? " hidden" : ""}>
      <td class="k loc"><button type="button" class="kbtn" data-jump-field="${r.k}" title="点击：在左边票面上定位并放大这个值"><span class="kk">${esc(r.lab)}</span><span class="ky">${esc(r.k)}</span></button></td>
      <td class="c v${r.k in t.airE ? " edited" : ""}" data-cell="air">${control(t, r.k)}${flagLines(r)}${edgeGripHtml()}</td>
      <td class="s" data-st="${r.k}">${statusCell(t, r)}</td></tr>`;
}
/* 三态提交按钮。红旗那一档与"跳过空栏"是两件事，界面上不许混成一颗按钮：
   红＝还有未确认红旗（点了只跳到第一处，绝不发送；服务端同一道硬门，绕不过去），
   琥珀＝红旗清完了但还有空栏没人确认（点第一下只是武装，再点才发，跳过的列进台账），
   绿＝没有待办。外部交接稿那句"强制提交跳过 N 项"从来不该作用于红旗，这里也没让它作用。
   只有一张模板：整页只留一份提交按钮的 id，写两处事件就只会绑上第一个。 */
function drawSubmitBtn(t, ef, n){
  let cls = "okGo", txt = "提交本票", tip = "没有待办了：这一票可以回传公司";
  if (ef.length){ cls = "badGo"; txt = "还有 " + ef.length + " 个红旗未确认";
    tip = "还有未确认的红旗：这一颗只会带你跳到第一处，不发送"; }
  else if (n && DESK.armed){ cls = "badGo"; txt = "再按一次：跳过 " + n + " 个空栏提交";
    tip = "再点一次就真的发给公司：这 " + n + " 个空栏没人确认过，提交时按 skipped_empty 逐列留痕"; }
  else if (n){ cls = "warnGo"; txt = "仍有 " + n + " 项待核对";
    tip = "点了不直接发送：先武装一次，再点才发。有未确认红旗时这一档根本不出现"; }
  return `<button class="btn ${cls}" id="submit" type="button" title="${esc(tip)}">${txt}</button>`;
}
function drawSubmitCtl(t){
  const old = $("#submit"); if (!old) return;
  const {ef, todo} = ticketTodo(t);
  const tmp = document.createElement("div");
  tmp.innerHTML = drawSubmitBtn(t, ef, todo.n);
  old.replaceWith(tmp.firstChild);
  $("#submit").addEventListener("click", () => submitClick(t));
}
/* 二次确认做在按钮上，不另立禁令：第一下只把自己变红并记下"武装了"，2.4 秒不点就收回去。
   与作废、彻底删同一规矩——第一下不发请求。 */
function armSubmit(t){
  DESK.armed = true;
  const n = ticketTodo(t).todo.n;
  drawSubmitCtl(t);
  toast("再点一次就提交这 " + n + " 个空栏（没人确认过的会逐列记进台账）。红旗不在这一档里：它必须一条一条确认", "");
  clearTimeout(armSubmit.timer);
  armSubmit.timer = setTimeout(() => { DESK.armed = false; drawSubmitCtl(t); }, 2400);
}
function submitClick(t){
  const {ef, todo} = ticketTodo(t);
  if (ef.length){
    const k = firstFlagKey(t, ef);
    if (k) jumpToField(t, k);
    toast("还有 " + ef.length + " 个红旗未确认：" + (k ? "已跳到第一处，对着票面核完再回来点确认" : "本票红旗没指到具体字段，逐条对照票面看") + "。这一颗不会替你发送", "bad");
    return;
  }
  if (todo.n && !DESK.armed){ armSubmit(t); return; }
  DESK.armed = false;
  submitTicket(t);
}
function firstFlagKey(t, ef){
  const map = flagsByField({flags: ef || effectiveFlags(t)}).map;
  return KEYS.find(k => (map[k] || []).length) || "";
}
function renderMain(){
  const el = $("#main"), t = current();
  el.classList.remove("has-todo");     // 只有分单核对区有置顶待办条
  /* 刷新后种回来的那一行身上只有号与状态：选中它就先去取完整那份，取回来再画这张表。
     不挡这一道，核对区会拿着一份空 air 画出 38 行空格子——看着像解析坏了。 */
  if (t && t.state === "seed"){
    el.innerHTML = '<div class="empty">正在取这张票的完整结果…</div>';
    openHouse(t.stem);
    return;
  }
  /* 主单视图：36 列可编辑 + 红旗逐条确认 → 提交发送回公司（mawb3）。列名/中文名/分组由后端
     fields 表给（单一真源），红旗在提交时由服务端按当前值重算——前端只负责让人看清并确认。 */
  if (MV){
    const rec = MV.rec || {}, qc = rec.qc || {}, flags = mvFlags();
    const vals = mvValues(), left = flags.filter(f => !MV.acked.includes(f));
    const missed = (qc.missed_cols) || [];
    const MVG = rec.groups || {};
    /* 主单侧同样"旗挂在它点名的那一格上"（顶部那排 chip 已经撤了）。一条旗点了两格就两格都亮；
       一格都不点的（"资料里有税号，填进同主体的 EORI 列"就是这种）落在表尾「整张票」那一格里，
       照样能确认——看不见又点不到的旗会卡住提交门，v1.17.2 为这类死旗修过一次。 */
    const MKEYS = (rec.fields || []).map(f => f[0]);
    /* 与 L1 那一栏同一份清单（后端算的）：这一列的资料值是本机上次发回去的，
       那一格的「有值」就不能当证据用——换成「本机回填」，人一看就知道该去问制单员/看原件。 */
    const BACK = new Set(rec.self_filled || []);
    const byKey = {}, orphan = [];
    for (const f of flags){
      const ks = L.flagKeys(f, MKEYS);
      if (ks.length) for (const k of ks) (byKey[k] = byKey[k] || []).push(f); else orphan.push(f);
    }
    const mvLine = f => {
      const done = MV.acked.includes(f);
      return `<div class="flagline${done ? " ack" : ""}">${done ? "已确认：" : ""}${esc(f)}` +
        (done ? `<button class="ackb" type="button" data-munack="${esc(f)}" title="撤回这条确认：它回到待办">撤回</button>`
              : `<button class="ackb" type="button" data-mack="${esc(f)}" title="票面已核对无误 → 确认这条提示，提交放行（留痕进台账）">确认无误</button>`) + `</div>`;
    };
    const metaBy = {};
    (rec.meta || []).forEach(m => { (metaBy[m.group] = metaBy[m.group] || []).push(m); });
    let rows = "", last = "";
    const openGroup = g => {
      if (g === last) return;
      rows += `<tr class="grp"><td colspan="3">${esc(MVG[g] || g)}</td></tr>`; last = g;
      (metaBy[g] || []).forEach(m => {
        rows += `<tr><td class="k"><div class="kk">${esc(m.label)}</div><div class="ky">${esc(m.col)}</div></td>
          <td class="c"><div class="j cell">${esc(String(m.value == null ? "" : m.value))}</div></td>
          <td class="s"><span class="badge">只读</span></td></tr>`;
      });
    };
    for (const [k, lab, g] of (rec.fields || [])){
      openGroup(g);
      const v = vals[k] == null ? "" : String(vals[k]);
      const fl = byKey[k] || [], hit = fl.length;
      const miss = missed.includes(k);
      rows += `<tr id="mf-${k}" class="${hit || miss ? "flag" : ""}">
        <td class="k"><div class="kk">${esc(lab)}</div><div class="ky">${esc(k)}</div></td>
        <td class="c v${k in MV.edit ? " edited" : ""}"><textarea data-mk="${k}" rows="1"${MV_LONG.has(k) ? "" : ' class="one"'}>${esc(v)}</textarea><div class="flags">${fl.map(mvLine).join("")}</div>${edgeGripHtml()}</td>
        <td class="s">${miss ? `<span class="badge miss">漏取</span>`
          : hit ? `<span class="badge warn">红旗 ${hit}</span>`
          : (v ? (BACK.has(k) ? `<span class="badge same" title="资料里这一格与本机上次发给公司的值相同，多半是自己回填的：它不能当独立证据">本机回填</span>`
                              : `<span class="badge same">有值</span>`)
               : `<span class="badge">资料没提</span>`)}</td></tr>`;
    }
    if (orphan.length)
      rows += `<tr class="grp"><td colspan="3">整张票（不落在某一列）</td></tr>` + orphan.map(f =>
        `<tr class="flag"><td class="k"><div class="kk">整张票</div><div class="ky">不落在某一列</div></td>`
        + `<td class="c"><div class="flags">${mvLine(f)}</div></td>`
        + `<td class="s"><span class="badge warn">红旗 1</span></td></tr>`).join("");
    const st = rec.state === "done" ? (left.length ? "解析完成，有红旗待处理" : "解析完成，无红旗")
             : rec.state === "parsing" ? "解析中（约 7-20 秒）…" : "解析失败";
    const fid = qc.fidelity;
    el.innerHTML = `
      <div class="hd">
        <span class="dot ${rec.state === "failed" ? "bad" : rec.state === "parsing" ? "busy"
          : (left.length ? "warn" : "ok")}"></span>
        <div><div class="nm">主单 ${esc(MV.mawb)} · 公司 AMS 解析</div>
          <div class="meta">${st} · 模型 ${esc(rec.model || "—")} · ${rec.elapsed || 0}s
            ${fid ? ` · 保真 ${fid.passed}/${fid.checked} 列有资料出处` : ""}
            ${Object.keys(MV.edit).length ? ` · 已改 ${Object.keys(MV.edit).length} 列` : ""}
            ${MV.submitted ? ` · 已回传 ${esc(MV.submitted.at)}` : ""}</div></div>
        <span class="sp"></span>
        <button class="btn sm" id="mvReparse">重新解析</button>
        <button class="btn sm" id="mvBack">返回分单核对</button>
      </div>
      ${MV.conflicts.length ? `<div class="chips"><span class="chip bad">本机未保存的编辑与最新解析对不上：${esc(MV.conflicts.map(mvLabel).join("、"))} —— 这几列改的还是重解析前的值，请对着左栏原文重改</span></div>` : ""}
      ${MV.staged ? `<div class="chips"><span class="chip">服务器上有 ${esc(L.roleCn(MV.staged.role))} ${esc(MV.staged.by)} 暂存的核对结果（${esc(MV.staged.at)}），已按那份显示</span></div>` : ""}
      ${MV.locked ? `<div class="chips"><span class="chip">这条主单在公司已经离开暂存档（状态 ${esc(MV.locked)}），公司不再收改动</span></div>` : ""}
      ${(MV.localDiff || []).length ? `<div class="chips"><span class="chip bad">${esc(MV.localDiff.map(mvLabel).join("、"))} 你这台机器上的改动和服务器暂存的不一致，已按服务器版本显示；你那份没删，还在本机未保存的编辑里</span></div>` : ""}
      ${rec.state === "parsing" ? `<div class="empty" id="mvParsingEmpty">主单 ${esc(MV.mawb)} 解析中（约 7-20 秒）：解析完成后这里出 36 列核对表，左栏也先空着等原文。</div>`
      : `${rec.state === "failed" ? `<div class="empty empty-err">解析失败：${esc(rec.error || "未知错误")}<br>
        <span class="sub">等一会儿点上面「重新解析」重试；连续失败就换个模型再看。</span></div>` : ""}
      <div class="tools"><span class="hint">改完直接点提交：红旗由服务端按当前值重算，未清的会退回来让逐条确认。
        主单表没有件重/航路/税号列，<b>税号按公司口径统一填同主体的 EORI 列</b>；
        同一主体有多个号（EORI 与 VAT 并存那种）就用 <b>/</b> 拼在同一格，两个都留。</span></div>
      <table><thead><tr>
        <th class="kcol">字段${gripColHtml()}</th><th class="g">公司 AMS 列 · 可改</th><th class="num scol">状态</th>
      </tr></thead><tbody>${rows}</tbody></table>
      <div class="ft bottom">
        <button class="btn" id="mvStage" type="button" title="把这 36 列存到服务器给同事核对，同时在 j9 那边占个位（状态 0，还能反复改）；只有「提交主单回公司」才会真的发送">暂存到公司</button>
        ${ME && ME.role === "inputter" ? `<button class="btn" id="mvReviewed" type="button" title="只记一笔「${esc(ME.name)} 看过了，先不发」：不写这 36 列、不碰公司、不改任何值">我核过了，先不发</button>` : ""}
        <button class="btn pri" id="mvSubmit">提交主单回公司</button>
        <span class="sp"></span>
        <span class="fhslot">${fhResetHtml()}</span>
        <span class="hint" id="mvMsg">${esc(MV.msg || (left.length ? "红旗 " + left.length + " 条待确认"
          : (Array.isArray(rec.flags_now) ? "红旗按公司现值算，没有（提交于 " + (rec.flags_now_at || "?") + "）" : "")))}</span>
      </div>`}`;
    el.querySelectorAll("[data-mk]").forEach(inp => {
      inp.addEventListener("change", () => mvTouch(inp));
      inp.addEventListener("keydown", blockEnter);
      autoGrow(inp);
    });
    el.querySelectorAll("[data-mack]").forEach(b => b.addEventListener("click", () => {
      MV.acked.push(b.dataset.mack); MV.msg = ""; mvSave(); render();
    }));
    el.querySelectorAll("[data-munack]").forEach(b => b.addEventListener("click", () => {
      MV.acked = MV.acked.filter(x => x !== b.dataset.munack); mvSave(); render();
    }));
    $("#mvBack").addEventListener("click", () => { mvOff(); render(); });
    $("#mvReparse").addEventListener("click", mvReparse);
    /* 解析中那一档没有底部按钮（mvParsingEmpty），这几颗的接线必须先认存在再挂——
       裸挂在 null 上会把整场 render 炸掉，票面栏就停在上一张票的画面里（浏览器里才看得见）。 */
    const mst = $("#mvStage"); if (mst) mst.addEventListener("click", mvStage);
    const mrv = $("#mvReviewed");
    if (mrv) mrv.addEventListener("click", () => markReviewed("mawb", MV.mawb));
    const msb = $("#mvSubmit"); if (msb) msb.addEventListener("click", () => mvSubmit());
    return;
  }
  if (!t){
    el.innerHTML = `<div class="empty empty-hero">上传分单后在这里逐字段核对。</div>`;
    return;
  }
  if (t.state === "busy" || t.state === "queued"){
    el.innerHTML = `<div class="empty empty-hero">正在解析 ${esc(t.filename)}…<br>
      <span class="sub">扫描件约 7-20 秒，Excel 通常更快。</span></div>`;
    return;
  }
  if (t.state === "failed"){
    el.innerHTML = `<div class="hd"><span class="nm">${esc(t.filename)}</span></div>
      <div class="empty empty-err">解析失败：${esc(t.error || "未知错误")}<br><br>
      <button class="btn pri" id="retryParse">重试解析</button><br><br>
      <span class="sub">超时多是模型临时拥堵：等一会儿点「重试解析」，不用重新上传。
      其它：登录过期（401，重新登录）、文件超限（413）、原件读不出。</span></div>`;
    $("#retryParse").addEventListener("click", () => retryParse(t));
    return;
  }
  const ef = effectiveFlags(t), [c, label] = statusOf(t);
  /* 重画一次就收回"武装"那一下：提交按钮的红态只代表"这最后一次点击会真的发送"，
     中间人又点了别的（确认、改值、换票）就等于改了主意，不该还留着。 */
  DESK.armed = false;
  const {rows, todo} = ticketTodo(t);
  /* 进度分母＝刚进这张票时抓的那份待办快照。拿 38 列当分母，一眼看去全是"完成度 58%"的假象
     （22 列本来就没印）；0/0 是 100%，不是 NaN。 */
  if (t.todoBase == null) t.todoBase = todo.n;
  const hard = {}, soft = {};
  for (const r of rows){ if (r.st.s === "flag") hard[r.k] = 1; if (r.st.s === "empty") soft[r.k] = 1; }
  const segs = L.segState(rows, hard, soft);
  const pr = L.progress(t.todoBase, todo.n);
  const nSoft = rows.filter(r => r.st.s === "empty").length;
  const fid = t.qc && t.qc.fidelity;
  const byG = {};
  for (const r of rows) (byG[r.g] = byG[r.g] || []).push(r);
  const segHtml = segs.map(seg => {
    const fs = byG[seg.g] || [], folded = FOLD.has(seg.g);
    /* 每段一张表：段头只有在自己那张表的 thead 里才粘得住（<tr> 上写 sticky 是假的，包含块就是那一行）；
       列宽由 colgroup 给（fixed 表格只认列定义，写在 td 上不生效——这两条都是浏览器里量出来的）。 */
    const body = folded ? `<tr class="foldrow"><td colspan="3">${seg.tot} 列折叠中：点段头的 ▸ 展开看值</td></tr>`
      : fs.map(r => fieldRowHtml(t, r, DESK.onlyTodo && !r.st.todo && !(r.k in t.airE))).join("");
    return `<table class="gt" id="gt-${seg.g}"${DESK.onlyTodo && !seg.todo ? " hidden" : ""}>`
      + `<colgroup><col class="kcol"><col class="vcol"><col class="scol"></colgroup>`
      + `<thead>${segHeadHtml(seg)}</thead><tbody>${body}</tbody></table>`;
  }).join("");
  el.classList.add("has-todo");
  el.innerHTML = `
    <div class="hd">
      <span class="dot ${c}" aria-hidden="true"></span>
      <div><div class="nm">${esc(t.filename)}</div>
        <div class="meta">${esc(t.stem || "")} · ${esc(t.channel || "?")} 通道 · ${t.elapsed || 0}s
          ${t.staged ? " · " + esc(L.roleCn(t.staged.role)) + " " + esc(t.staged.by) + " 已暂存（改 " + t.staged.edited + " 列）" : ""}${t.locked ? " · 公司已离开暂存档（状态 " + esc(t.locked) + "），不再收改动" : ""}${t.restored === "server" ? " · 服务器载入" : (t.restored ? " · 本机未保存的编辑载入" : "")}${label ? " · " + label : ""}
          ${t.flagsNow ? " · 红旗按公司现值算（提交于 " + esc(t.flagsNow) + "）" : ""}
          · 保真回查 ${fid ? `${fid.passed}/${fid.checked} 字段有票面转录出处` : "本次无记录"}</div></div>
    </div>
    <div class="todo">
      <span class="lbl">待办</span>
      <button class="t-flag" type="button" data-jump-first="flag" id="cFlag">红旗 ${ef.length}</button>
      <button class="t-soft" type="button" data-jump-first="empty" id="cEmpty">空值 ${nSoft}</button>
      <span class="t-edit" id="cEdit">已改 ${Object.keys(t.airE).length}</span>
      <button class="btn sm" id="jumpFirst" type="button">跳到首个待办</button>
      <label><input type="checkbox" id="onlyTodo"${DESK.onlyTodo ? " checked" : ""}> 只看待核对</label>
      <span class="bar" aria-hidden="true"><i id="barFill"></i></span>
      <span class="t-mut" id="progTxt">${pr.done}/${pr.total}</span>
      <span class="fhslot">${fhResetHtml()}</span>
      <span class="sp"></span>
      <span class="hint" title="中列就是航空口径 L3（可直接改）；指针停在一行上，左边票面会框出它的转录出处；点字段名定位并放大；回车跳下一个待办（Shift+Enter 才是换行）">点字段名＝定位 · 回车＝下一个</span>
    </div>
    ${segHtml}${orphanTable(t, ef)}
    <div class="ft bottom">
      <button class="btn" id="stage" type="button" title="把现在的核对结果存到服务器给同事看，同时在 j9 那边占个位（状态 0，还能反复改）；缺号只存本机。只有「提交本票」才会真的发送">暂存到公司</button>
      ${ME && ME.role === "inputter" && !t.submitted ? `<button class="btn" id="markReviewed" type="button" title="只记一笔「${esc(ME.name)} 看过了，先不发」：不写核对结果、不碰公司、不改任何值。要交给同事点「暂存到公司」，要发走点「提交本票」">我核过了，先不发</button>` : ""}
      ${drawSubmitBtn(t, ef, todo.n)}
      <button class="btn" id="expOne">导出本票 JSON</button>
      <button class="btn" id="expAll">导出全部已解析</button>
      <span class="sp"></span>
      <button class="btn sm" id="prevOne" type="button" title="上一张票">‹ 上一票</button>
      <button class="btn sm" id="nextOne" type="button" title="下一张票">下一票 ›</button>
      <button class="btn gh" id="dropOne">从列表移除</button>
    </div>`;
  wireMain(t);
  measureChrome();
  growAll($("#main").querySelectorAll("textarea"));
  $("#barFill").style.width = pr.pct + "%";    // 宽度只能在这儿写：模板里不许出现内联样式（守卫盯着）
  drawAnchors();                                // 确认一条红旗，票面上那个常驻锚点就要当场没
}
/* 吸顶高度是量出来的，不是抄下来的：顶栏在 1020-1460px 会折成两排、待办条会随红旗多少换行，
   写死一次就在折行那天压住第一行字段（--todoH 有过这个教训，--hdh 从前干脆只是个静态令牌）。
   两个都由这里写回，ResizeObserver 负责"变了再量一次"，renderMain 负责"刚画完就先量一次"。 */
function measureChrome(){
  const r = document.documentElement, hdr = $(".hdr"), tb = $("#main .todo");
  if (hdr) r.style.setProperty("--hdh", hdr.offsetHeight + "px");
  if (tb) r.style.setProperty("--todoH", tb.offsetHeight + "px");
}
if (window.ResizeObserver){
  const ro = new ResizeObserver(measureChrome);
  ro.observe($(".hdr"));
  ro.observe($("#main"));        // 待办条是渲染出来的、节点会换，观察整栏：内容一改高度就重算
}
function setTxt(sel, s){ const el = $(sel); if (el) el.textContent = s; }
/* 待办条、五段段头、提交按钮：这三处都是"数一数还剩什么"，改一格就要一起重算。
   只换 these 三块的innerHTML，绝不重建行（行里有正在编辑的 textarea：重建就丢焦点、打断输入法）。 */
function refreshChrome(t){
  const {ef, rows, todo} = ticketTodo(t);
  const hard = {}, soft = {};
  for (const r of rows){ if (r.st.s === "flag") hard[r.k] = 1; if (r.st.s === "empty") soft[r.k] = 1; }
  for (const seg of L.segState(rows, hard, soft)){
    const th = $("#gt-" + seg.g + " thead"); if (th) th.innerHTML = segHeadHtml(seg);
  }
  const pr = L.progress(t.todoBase == null ? todo.n : t.todoBase, todo.n);
  setTxt("#cFlag", "红旗 " + ef.length);
  setTxt("#cEmpty", "空值 " + rows.filter(r => r.st.s === "empty").length);
  setTxt("#cEdit", "已改 " + Object.keys(t.airE).length);
  setTxt("#progTxt", pr.done + "/" + pr.total);
  const bar = $("#barFill"); if (bar) bar.style.width = pr.pct + "%";
  drawSubmitCtl(t);
}
function refreshRow(t, k){
  const tr = $("#f-" + CSS.escape(k)), ef = effectiveFlags(t), rows = fieldRows(t, ef);
  const r = rows.find(x => x.k === k);
  if (tr && r){
    tr.className = rowCls(r, t);
    tr.dataset.todo = r.st.todo ? "1" : "0";
    tr.querySelector("[data-st]").innerHTML = statusCell(t, r);
    const fx = tr.querySelector('[data-cell="air"] .flags');
    if (fx) fx.outerHTML = flagLines(r);       // 旗面原文跟着改：确认掉的那条要当场从红转灰
    tr.querySelector('[data-cell="air"]').classList.toggle("edited", k in t.airE);
  }
  const row = document.querySelector(`.row[data-i="${S.tickets.indexOf(t)}"] .st`);
  if (row) row.innerHTML = statusOf(t)[1] + (editedList(t).length ? `<br><span class="st-edited">改 ${editedList(t).length} 项</span>` : "");
  refreshChrome(t);
}
/* 跳到某一列：段是折着的先展开，"只看待核对"会把目标挡掉就临时取消勾选，然后再滚过去闪一下。
   这两处不清，跳过去的结果就是"点了没反应"——比不点更糟。 */
function jumpToField(t, k){
  if (!k) return false;
  const g = (FIELDS.find(f => f[0] === k) || [])[2];
  const r = fieldRows(t, effectiveFlags(t)).find(x => x.k === k);
  const hidden = (g && FOLD.has(g)) || (DESK.onlyTodo && r && !r.st.todo && !(k in t.airE));
  if (hidden){
    FOLD.delete(g);
    if (DESK.onlyTodo){ DESK.onlyTodo = false; const cb = $("#onlyTodo"); if (cb) cb.checked = false; }
    render();
  }
  const tr = $("#f-" + CSS.escape(k)); if (!tr) return false;
  tr.scrollIntoView({behavior:"smooth", block:"center"});
  tr.classList.remove("hit");
  void tr.offsetWidth;                 // 先重排一次：连着点同一个字段第二次也要真的闪
  tr.classList.add("hit");
  const inp = tr.querySelector("[data-k]"); if (inp) inp.focus();
  return true;
}
function gotoNextTodo(t, from){
  const ks = ticketTodo(t).todo.keys;
  const k = L.nextTodo(ks, from);
  if (!k){ toast("待办清零：可以提交了", "ok"); return; }
  if (!jumpToField(t, k)) toast("这一列在别的段里，先展开才看得到", "bad");
}
function wireMain(t){
  /* 委托挂 #main（renderMain 每次换的是它的 innerHTML，节点不毁）：只接一次，
     否则重画一回就多挂一份监听，点一下字段名会并发跑一遍定位 */
  if (!$("#main")._locWired){
    $("#main")._locWired = true;
    $("#main").addEventListener("click", e => {
      const b = e.target.closest("[data-jump-field]"); if (!b) return;
      const t2 = current(); if (!t2) return;
      enterLocate(t2, b.dataset.jumpField);
    });
    /* 悬停那一层也走委托：五段是五张表，逐个 tr 挂监听既容易漏又要每次重画都补一遍 */
    $("#main").addEventListener("mouseover", e => {
      const tr = e.target.closest("tr[data-hov]");
      const k = tr ? tr.dataset.hov : "";
      if (k !== HOVER.k) hoverField(k);
    });
    $("#main").addEventListener("mouseleave", () => hoverField(""));
    /* 确认 / 撤回 / 还原 / 折叠 / 整段核对这一排走 #main 委托，不逐节点挂。
       为什么：改一个值只重画那一格（refreshRow 换 [data-st] 与旗面原文）和那一段的段头
       （refreshChrome 换 thead），新节点身上没有监听——从前"点「红旗已核」没反应，
       先勾一下「只看待核对」再点才生效"就是这么来的（2026-10-16 用户实测）。
       委托只挂一次，整页重画、局部重画都不影响它。 */
    $("#main").addEventListener("click", e => {
      const b = e.target.closest("[data-ack],[data-unack],[data-ackf],[data-unackf]," +
                                "[data-acke],[data-une],[data-restore],[data-fold],[data-gack]");
      if (!b) return;
      const t = current(); if (!t) return;
      const d = b.dataset;
      let msg = "";
      if (d.restore !== undefined){ delete t.airE[d.restore]; }
      else if (d.fold !== undefined){ if (FOLD.has(d.fold)) FOLD.delete(d.fold); else FOLD.add(d.fold); }
      else{
        t.ackF = t.ackF || []; t.ackE = t.ackE || [];
        if (d.ack !== undefined){
          if (!t.ackF.includes(d.ack)) t.ackF.push(d.ack);
          msg = "已确认：" + d.ack.slice(0, 40) + (d.ack.length > 40 ? "…" : "") + "（提交时留痕进台账）";
        } else if (d.unack !== undefined){
          t.ackF = t.ackF.filter(f => f !== d.unack);
        } else if (d.ackf !== undefined){
          const list = flagsByField({flags: effectiveFlags(t)}).map[d.ackf] || [];
          list.forEach(f => { if (!t.ackF.includes(f)) t.ackF.push(f); });
          msg = "已确认「" + d.ackf + "」这一列的 " + list.length + " 条红旗（逐条留痕进台账，可撤回）";
        } else if (d.unackf !== undefined){
          const list = flagsByField(t.qc).map[d.unackf] || [];
          t.ackF = t.ackF.filter(f => !list.includes(f));
        } else if (d.acke !== undefined){
          if (!t.ackE.includes(d.acke)) t.ackE.push(d.acke);
          msg = "已确认「" + d.acke + "」票面本来就没印（提交时按 acked_empty 留痕）";
        } else if (d.une !== undefined){
          t.ackE = t.ackE.filter(x => x !== d.une);
        } else if (d.gack !== undefined){
          /* 整段核对：与逐条点完全同一套留痕（一列一列记进 ackF/ackE），不是"一键放行整段" */
          const g = d.gack, rows = fieldRows(t, effectiveFlags(t)).filter(r => r.g === g);
          const map = flagsByField({flags: effectiveFlags(t)}).map;
          rows.forEach(r => (map[r.k] || []).forEach(f => { if (!t.ackF.includes(f)) t.ackF.push(f); }));
          rows.forEach(r => { if (r.st.s === "empty" && !t.ackE.includes(r.k)) t.ackE.push(r.k); });
          msg = "本段「" + (GROUPS[g] || g) + "」" + rows.length + " 列已整段核对（留痕逐列记，可撤回）";
        }
      }
      saveDraft(t); render();
      if (msg) toast(msg, "ok");
    });
  }
  $("#main").querySelectorAll("[data-k]").forEach(inp => {
    const apply = () => {
      setEdit(t, inp.dataset.k, inp.value);
      refreshRow(t, inp.dataset.k);
      if (inp.tagName === "TEXTAREA") autoGrow(inp);
    };
    inp.addEventListener("input", apply);
    /* 失焦那一下不再从头写一遍：值早在 input 里记脏了，这里只把它落盘。
       从前 input 与 blur 各调一次 apply = 同一次编辑写两遍 localStorage。 */
    inp.addEventListener("blur", () => { apply(); draftFlush(); });
    inp.addEventListener("keydown", blockEnter);
  });
  const mrv = $("#markReviewed");
  if (mrv) mrv.addEventListener("click", () => markReviewed("hawb", t.stem));
  /* 确认 / 撤回 / 还原这一排不再在这里逐节点挂：见上面 #main 的委托块。
     从前它们挂在这里，而改一个值只重画那一格（refreshRow），新按钮没监听——
     制单员就撞上"点「红旗已核」毫无反应，勾一下「只看待核对」再点才生效"。 */
  $("#jumpFirst").addEventListener("click", () => {
    const ks = ticketTodo(t).todo.keys;
    if (!ks.length){ toast("这一票没有待办了：可以提交", "ok"); return; }
    jumpToField(t, ks[0]);
  });
  $("#main").querySelectorAll("[data-jump-first]").forEach(b => b.addEventListener("click", () => {
    const rows = fieldRows(t, effectiveFlags(t));
    const r = rows.find(x => x.st.s === (b.dataset.jumpFirst === "flag" ? "flag" : "empty"));
    if (!r){ toast(b.dataset.jumpFirst === "flag" ? "红旗都确认过了" : "空栏都确认过了", "ok"); return; }
    jumpToField(t, r.k);
  }));
  $("#onlyTodo").addEventListener("change", e => { DESK.onlyTodo = e.target.checked; render(); });
  $("#stage").addEventListener("click", () => stageTicket(t));
  $("#submit").addEventListener("click", () => submitClick(t));
  $("#expOne").addEventListener("click", () => exportTickets([t], (t.stem || t.filename) + ".审核结果.json"));
  $("#expAll").addEventListener("click", () => exportTickets(
    S.tickets.filter(x => x.state === "done"), "分单审核_" + new Date().toISOString().slice(0, 10) + ".json"));
  $("#prevOne").addEventListener("click", () => stepTicket(-1));
  $("#nextOne").addEventListener("click", () => stepTicket(1));
  $("#dropOne").addEventListener("click", () => {
    const i = S.tickets.indexOf(t);
    S.tickets.splice(i, 1);
    S.sel = S.tickets.length ? (S.tickets[Math.max(0, i - 1)].stem || S.tickets[Math.max(0, i - 1)].filename) : null;
    render();
  });
}

/* ── 事件挂载与启动 ─────────────────────────────────────────────────── */
$("#pick").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", e => { addFiles([...e.target.files]); e.target.value = ""; });
const dz = $("#drop");
for (const ev of ["dragenter", "dragover"]) dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("hot"); });
for (const ev of ["dragleave", "drop"]) dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("hot"); });
dz.addEventListener("drop", e => addFiles([...e.dataTransfer.files]));
$("#pvIn").addEventListener("click", () => { PREVIEW.zoom = Math.min(400, PREVIEW.zoom + 25); applyZoom(); });
$("#pvOut").addEventListener("click", () => { PREVIEW.zoom = Math.max(25, PREVIEW.zoom - 25); applyZoom(); });
$("#pvFit").addEventListener("click", () => { PREVIEW.zoom = 100; applyZoom(); });
installPan();
/* passive:false 是必须的：默认 passive 监听里 preventDefault 会被浏览器忽略，滚轮就还是去滚页面 */
$("#pvBody").addEventListener("wheel", wheelZoom, {passive: false});
$("#pvPage").addEventListener("click", () => {
  if (PREVIEW.mode === "pages"){
    const pct = +($("#pvPage").dataset.pct || 100);
    PREVIEW.zoom = Math.abs(PREVIEW.zoom - pct) < 3 ? 100 : pct;   // 再点一次回适应宽度，别做一颗第二次点了没反应的按钮
    applyZoom();
    return;
  }
  const nxt = L.nextView(PREVIEW.cycle, PREVIEW.view);
  if (!nxt) return;
  PREVIEW.view = nxt.k;
  pdfFrame();
});
$("#pvDl").addEventListener("click", downloadSource);
$("#pvLocExit").addEventListener("click", exitLocate);
$("#pvLocPrev").addEventListener("click", () => showLocPage(LOC.page - 1, ++_pvSeq));
$("#pvLocNext").addEventListener("click", () => showLocPage(LOC.page + 1, ++_pvSeq));
/* 换档/改比例都当场重画当前这张票：改完还要再点一次字段才生效，人就以为这控件没起作用 */
$("#pvLocZoom").addEventListener("change", e => {
  const p = zoomPref();
  saveZoomPref({mode: e.target.value, pct: p.pct});
  locFit();
});
$("#pvLocPct").addEventListener("change", e => {
  const p = zoomPref(), want = Math.round(+e.target.value || 0), cap = locCapPct();
  const used = L.locateZoom("fixed", want, 0, cap);
  saveZoomPref({mode: p.mode, pct: used});
  locFit();
  if (used < want) toast(`已按这张票面图的真实分辨率限制在 ${used}%（填的 ${want}% 只会糊）`, "bad");
});
$("#pvNew").addEventListener("click", () => {
  const u = (LOC.on && LOC.url) || PREVIEW.url;
  if (!u){ toast("还没有可打开的票面", "bad"); return; }
  window.open(u, "_blank");       // blob URL 直接开新标签：图片能看，PDF 走阅读器
});
$("#list").addEventListener("click", e => {
  const x = e.target.closest("[data-drop]");
  if (x){ dropRow(+x.dataset.drop); return; }      // 那颗 × 是行的兄弟节点：先认它，再认整行
  const row = e.target.closest(".row"); if (!row) return;
  const t = S.tickets[+row.dataset.i]; if (!t) return;
  mvOff();                       // 选中分单就退出"主单资料"视图，回到该票自己的核对与提交
  S.sel = t.stem || t.filename; render();
});
$("#drafts").addEventListener("click", e => {
  const row = e.target.closest("[data-load]");
  if (!row) return;
  const t = loadDraft(row.dataset.load);
  if (t){ S.tickets.push(t); S.sel = t.stem; render(); toast("已载入本机未保存的编辑：" + t.stem, "ok"); }
});
$("#railTabs").addEventListener("click", e => {
  const b = e.target.closest("[data-rt]"); if (!b) return;
  railTab(b.dataset.rt);
});
/* 「清空」在卡片标题栏里，不在 #drafts 列表内——挂在列表上的委托收不到它的点击（曾因此点了没反应） */
$("#clearDrafts").addEventListener("click", () => {
  const n = Object.keys(drafts()).length;
  if (!n){ toast("本机没有未保存的编辑", "bad"); return; }
  if (!confirm(`清空本机浏览器里未保存的 ${n} 条编辑？（它们从没上过服务器；已落盘的 JSON 结果不受影响）`)) return;
  draftPending = {}; clearTimeout(draftTimer);      // 待写那份也要撤，不然清完又被合并写回来
  localStorage.removeItem(LS.draft); renderDrafts();
  toast(`已清空本机未保存的编辑 ${n} 条`, "ok");
});
/* ── 登录 / 角色 / 账号管理 ─────────────────────────────────────────── */
function acctErr(msg, clear){ const el = $("#acctErr"); if (clear){ el.hidden = true; return; } el.textContent = msg; el.hidden = false; }

function applyRole(){
  const admin = !!ME && ME.role === "admin";
  $("#gate").hidden = !!ME;                 // 已登录：收起登录遮罩
  // 遮罩只挡视觉，不挡键盘焦点与点击穿透前的探索：未登录时把背后整片标成 inert，
  // 这样 Tab 进不去、点也点不动，不会让人以为"按钮坏了"。
  [document.querySelector(".hdr"), $("#wrap"), $("#mstHawbCard"), $("#dayCard")].forEach(el => {
    if (el) el.inert = !ME;
  });
  $("#whoWrap").hidden = !ME;
  $("#btnLogout").hidden = !ME;
  if (!ME) return;
  $("#whoName").textContent = ME.name;
  $("#whoRole").textContent = L.roleCn(ME.role);
  $("#btnAcct").hidden = !admin;             // 账号管理仅管理员
  $("#btnStats").hidden = !admin;            // 统计仅管理员（/stats 与 /stats/export 后端也是这道门）
  $("#btnPurge").hidden = !admin;            // 数据清理同样仅管理员；藏按钮只是遮眼睛，门在服务端
  $("#btnSet").hidden = !admin;              // 收纳入口本身：非管理员连「设置」都不该看见
  if (admin) purgeBadgeLoad();               // 开门就看见有几张到期了，但删除仍要人点下去
  $("#pwWarn").hidden = !(admin && PW_DEFAULT);   // 默认口令没改：把话放在管理员天天看得见的地方
  $("#btnSetDot").hidden = !(admin && PW_DEFAULT); /* 告警收进设置之后，顶栏留一个圆点：
      不能要求管理员先想到点开设置才知道"门禁等于没上"。完整那句在面板的账号节里。 */
  $("#btnMawb").hidden = !ME;                // 主单检索已并入本页：登录即可用，入口只在做登录时藏
  $("#fModel").hidden = !admin;              // 模型下拉仅管理员
  $("#fModelM").hidden = !admin;             // 主单链那条也是
  $("#fThink").hidden = !admin;              // 思考档同样是改提取结果，跟模型一个门（后端也只认管理员会话）
  const rv = $("#reviewer");                 // 复核人 = 登录身份，锁定不让手写（提交留痕即本人）
  rv.value = ME.name; rv.readOnly = true;
}
function startDesk(){
  if (startDesk._on) return;
  startDesk._on = true;
  pruneSubmittedDrafts();   // 老版本留下的已提交副本先清掉，再画那一栏（不然数字还是骗人）
  probe(); loadModels(); render();
  restoreQueue();           // 服务器那份「今天你动过的票」补回桌面，刷新不等于从零开始
}
async function checkAuth(){
  try{
    const j = await fetch(BASE + "/api/me").then(x => x.json());
    ME = j.authenticated ? j.user : null;
    PW_DEFAULT = !!j.admin_pw_default;
  }catch(e){ ME = null; PW_DEFAULT = false; }
  applyRole();
  if (ME) startDesk();
}
$("#loginForm").addEventListener("submit", async e => {
  e.preventDefault();
  const name = $("#lgName").value.trim(), pw = $("#lgPass").value, err = $("#lgErr");
  err.hidden = true;
  if (!name || !pw){ err.textContent = "请输入账号和密码"; err.hidden = false; return; }
  $("#lgBtn").disabled = true;
  try{
    const r = await fetch(BASE + "/login", {method:"POST", headers:{"Content-Type":"application/json"},
                                           body:JSON.stringify({name, password:pw})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.detail || ("HTTP " + r.status));
    await checkAuth();               // 登录后统一走 /api/me：ME 与「口令未改」提示同一个来源，别两处各设一遍
    $("#lgPass").value = "";
    toast("已登录：" + ME.name + (ME.role === "admin" ? "（管理员）" : ""), "ok");
  }catch(e2){ err.textContent = "登录失败：" + (e2.message || e2); err.hidden = false; }
  finally{ $("#lgBtn").disabled = false; }
});
$("#btnLogout").addEventListener("click", async () => {
  try{ await fetch(BASE + "/logout", {method:"POST"}); }catch(e){ /* 清不掉也要回登录页 */ }
  location.reload();
});
async function getAdmin(path){
  try{
    const r = await fetch(BASE + path);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    return j;
  }catch(e){ $("#acctList").innerHTML = `<div class="empty">读取失败：${esc(e.message || e)}</div>`; return null; }
}
async function loadAcct(){
  $("#acctList").innerHTML = '<div class="empty">加载中…</div>';
  const j = await getAdmin("/admin/users"); if (!j) return;
  $("#acctList").innerHTML = (j.users || []).map(u => `<div class="arow">
      <span class="an">${esc(u.name)}</span>
      <span class="tag ${u.role === "admin" ? "admin" : ""}">${esc(L.roleCn(u.role))}</span>
      <span class="${u.has_password ? "" : "nopw"}">${u.has_password ? "已设口令" : "未设口令"}</span>
      <button class="btn sm" data-pw="${esc(u.name)}">重置口令</button>
      <button class="btn sm gh" data-del="${esc(u.name)}">删除</button>
    </div>`).join("") || '<div class="empty">还没有账号。</div>';
}
async function saveAcct(name, password, role){
  acctErr("", true);
  try{
    const r = await fetch(BASE + "/admin/users", {method:"POST", headers:{"Content-Type":"application/json"},
                                                 body:JSON.stringify({name, password, role: role || ""})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.detail || ("HTTP " + r.status));
    toast("已保存账号：" + name, "ok"); loadAcct();
    if (ME && name === ME.name) await checkAuth();   // 改的是自己的口令：顶栏那颗提示要当场灭掉
  }catch(e){ acctErr("保存失败：" + (e.message || e)); }
}
/* 设置浮层：管理员入口的收纳处。开合只管这一层，里面每颗按钮打开的还是原来那张窗口。 */
function openSet(){ $("#setWin").hidden = false; $("#btnSet").setAttribute("aria-expanded", "true"); }
function closeSet(){ $("#setWin").hidden = true; $("#btnSet").setAttribute("aria-expanded", "false"); }
$("#btnSet").addEventListener("click", () => { $("#setWin").hidden ? openSet() : closeSet(); });
$("#setClose").addEventListener("click", () => { closeSet(); $("#btnSet").focus(); });
/* 从设置里点开任何一张窗口，都要先把设置那一层收掉：两层都是全屏遮罩、同一个 z-index，
   叠着开就是新窗口压在设置底下——点了「解析正确率」却还看着设置，比不点更像坏了（浏览器里量到的）。 */
function openGate(id){
  closeSet();
  const w = $("#" + id);
  w.hidden = false;
  return w;
}
$("#btnAcct").addEventListener("click", () => { openGate("acct"); loadAcct(); });
$("#pwWarn").addEventListener("click", () => $("#btnAcct").click());   // 话说到哪儿，入口就在哪儿
/* 浮层从前只有「关闭」那颗按钮能出去：键盘按 Esc 没反应，人以为页面卡住。
   清单从 DOM 里现捞（多一张窗口自动管上，不抄手写清单），叠着开时只关最上面那张；
   焦点还给打开它的按钮，否则下一次 Tab 要从页面顶上重走一遍。
   登录遮罩 #gate 不在内：真正的门在服务端（未登录什么接口都调不动），但"Esc 一掀就露出页面"
   看着像门禁失效，这个错觉不给。 */
const GATE_BACK = {setWin: "#btnSet", statsWin: "#btnStats", purgeWin: "#btnPurge", acct: "#btnAcct"};
function closeTopGate(){
  const open = [...document.querySelectorAll(".gate:not(#gate)")].filter(g => !g.hidden);
  if (!open.length) return false;
  const top = open[open.length - 1];
  top.hidden = true;
  if (top.id === "purgeWin") PURGE.armed = false;
  if (top.id === "setWin") closeSet();
  /* 焦点要落在看得见的那颗入口上：子窗口的入口（统计/清理/账号）此刻藏在已经收起来的设置面板里，
     焦点给它等于丢失；退一步落到顶栏「设置」，键盘用户还能继续。 */
  const sel = GATE_BACK[top.id], entry = sel ? $(sel) : null;
  const back = (entry && entry.offsetParent) ? entry : $("#btnSet");
  if (back){ back.focus(); }
  return true;
}
document.addEventListener("keydown", e => { if (e.key === "Escape" && closeTopGate()) e.preventDefault(); });
$("#btnMawb").addEventListener("click", () => {   // 检索已并入本页：按钮只做"切到那一页 + 滚过去 + 聚焦输入框"
  setView("desk");                                // 在今日台账里点它，要先回到工作台才看得见那张卡
  railTab("mawb");                                // 不先切页签，#mstNo 藏在 hidden 的面板里，滚动与聚焦都是空动作
  const box = $("#mstNo");
  box.scrollIntoView({behavior: "smooth", block: "center"});
  box.focus();
});
$("#acctClose").addEventListener("click", () => { $("#acct").hidden = true; });
$("#acAdd").addEventListener("click", async () => {
  const n = $("#acName").value.trim(), p = $("#acPass").value, role = $("#acRole").value;
  if (!n){ acctErr("请填写账号名"); return; }
  await saveAcct(n, p, role); $("#acName").value = ""; $("#acPass").value = "";
});
$("#acctList").addEventListener("click", async e => {
  const pw = e.target.closest("[data-pw]"), del = e.target.closest("[data-del]");
  if (pw){
    const np = prompt("为「" + pw.dataset.pw + "」设置新口令（≥6 位）：");
    if (np !== null) await saveAcct(pw.dataset.pw, np, "");
  } else if (del){
    if (!confirm("删除账号「" + del.dataset.del + "」？该账号将无法再登录。")) return;
    try{
      const r = await fetch(BASE + "/admin/users/" + encodeURIComponent(del.dataset.del), {method:"DELETE"});
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.detail || ("HTTP " + r.status));
      toast("已删除", "ok"); loadAcct();
    }catch(err){ acctErr("删除失败：" + (err.message || err)); }
  }
});
/* ── 主单检索（2026-09-26 由录入员检索台并入）：入口 + 名下分单 + 原件 ── */
function mstCard(j, mawb){
  /* 检索卡片只留入口：外层原始字段（JOB_ID/MASTER_NO/几坨拼接资料块）不再摊在这儿——
     内容和解析结果都在主单核对页看，这里重复一遍只会把左栏撑成一屏流水账（2026-09-28 用户定案）。 */
  const mo = j.mawb_order || {};
  const src = j.source_available
    ? '<a href="' + BASE + '/mawb/source/' + encodeURIComponent(mawb) + '" target="_blank">查看主单原件</a>'
    : '<span class="hint">本机暂无主单原件（可放 output/mawb_source/ 对应主单号目录）</span>';
  const st = ({parsing:"解析中…", done:"已解析", failed:"解析失败"})[j.master && j.master.state] || "待解析";
  const use = Object.keys(mo).length
    ? '<button class="btn sm" data-mv="' + esc(mawb) + '">送入主单核对</button>'
    : '<span class="hint">公司侧没有这条主单的资料</span>';
  return '<div class="t2">主单 ' + esc(mawb) + ' · ' + st + '　' + src + '　' + use + '</div>';
}
/* 公司侧 SEND_STATUS 字典（2026-10-09 IT 文档）：0=暂存（公司在等我们点提交发送）、
   1=已提交发送（还没发出去，等外围程序取数）、2=外围已发送成功。
   1 与 2 都锁死不能再改——从前把 1 写成"已发送"、2 写成"已改·待重发"，
   人会以为发出去了（其实没有），还会去覆盖一条已经发成功的记录。 */
const SEND_TXT = {0:"公司暂存（未发送）", 1:"已提交发送·等外围", 2:"公司已发送成功（锁定）"};
/* 名下分单表里"公司还不认识"的那些行，来源是检索响应里的 pending（服务器从盘上现算）。
   从前这里是自己数浏览器状态（pendingUnder 读 S.tickets + localStorage），那份名单只对
   当前这个人、这台机器成立——制单员上午传的票，录入员下午查同一张主单仍是"没有分单"。
   行由 L.hawbRows 按分单号合并好再交进来：公司的行与本机的副本说的是同一张票时，
   这里只有一行（2026-10-13 用户实测：BJS7080646 三行）。 */
function mstTable(rows){
  rows = rows || [];
  if (!rows.length)
    return '<p class="hint hintp">这条主单下公司还没有分单记录。' +
           '<b>解析不等于提交</b>：有人点「暂存」，核对结果就存在服务器上给同事看；' +
           '点「提交」才会回传公司，这里才会出现公司的行。</p>';
  const tr = rows.map(r => {
    const many = r.copies > 1
      ? '；这个号在本机有 ' + r.copies + ' 份，点开的是'
        + (r.via === "company" ? "公司已提交的那一份" : "最新解析的那一份")
        + (r.source_name ? "（" + esc(r.source_name) + "）" : "") : "";
    /* 号没解析出来的那一格不许是空白：空白看着像"这行没内容"，可它其实是个能点开的按钮
       （2026-10-15 用户问"分单号是空的为什么会是空的"——那几张就是模型没读出号的票，
       点开就能在号那一格里补上）。话要写在格子里，别做成人看不见的热点。 */
    const nm = r.hawb || "没解析出分单号";
    const no = (r.stem ? '<button type="button" class="lk" data-open="' + esc(r.stem) +
                         '" title="' + (r.hawb ? "点开这张分单的核对页"
                                               : "点开这张票：分单号没解析出来，在号那一格里补上")
                         + many + '">' + esc(nm) + '</button>'
                       : esc(nm))
      + (many ? ' <span class="hint">本机 ' + r.copies + ' 份</span>' : '');
    const who = r.company ? esc(r.reviewer || "—")
      : (r.stager ? esc(r.stager) + (r.edited ? ' <span class="st-edited">改 ' + r.edited + ' 列</span>' : '')
                  : '<span class="hint">只解析，没人暂存</span>');
    return "<tr><td>" + no + "</td><td>" + esc(r.mawb) +
      "</td><td>" + who + "</td><td>" +
      (r.company
        ? (r.submitted_here ? esc(r.submitted_at || "—") : '<span class="hint">不在本台提交</span>')
        : (r.staged_at ? esc(r.staged_at) : '<span class="hint">—</span>')) +
      "</td><td>" + (r.company
        ? esc(r.send_status === null ? "—" : (SEND_TXT[r.send_status] || r.send_status))
        : '<span class="hint">公司侧还没有</span>') + "</td><td>" +
      (r.has_original ? '<a href="' + BASE + '/source/' + encodeURIComponent(r.stem) +
                        '" target="_blank">查看原件</a>'
                      : '<span class="hint">本机无归档</span>') + "</td></tr>";
  }).join("");
  const mine = rows.filter(r => !r.company).length;
  return '<table><colgroup><col style="width:19%"><col style="width:15%"><col style="width:12%">' +
    '<col style="width:18%"><col style="width:16%"><col></colgroup><thead><tr>' +
    '<th>分单号</th><th>主单号</th><th>提交人</th><th>提交时间</th><th>公司发送状态</th><th>票面原件</th>' +
    '</tr></thead><tbody>' + tr + '</tbody></table>' +
    '<p class="hint hintp">「提交人/提交时间」是本审核台提交回公司的留痕；' +
    '「公司发送状态」是公司有没有把这条分单发给航司。' +
    '待公司发送 = 公司已收到、还没往航司发，不是说本台没提交。' +
    (mine ? '另有 ' + mine + ' 张公司还不认识：它们是这台机器上解析/暂存过的，' +
     '「改 N 列」是暂存时相对模型输出改了多少。' : '') +
    '同一个分单号在本机传过几次，这里只给一行：号上标「本机 N 份」，点开的是哪一份看停在号上的提示。</p>';
}
/* 一份结果两处落位：左栏只留入口那一行，名下分单表进页面底部整宽卡（264px 里放不下五列） */
function mstRender(j, mawb){
  $("#mstResult").innerHTML = mstCard(j, mawb);
  const rows = L.hawbRows(j.hawb_orders || [], j.pending || []);
  MST_CARD_ON = !!rows.length;
  syncCards();
  /* 卡头上的张数与表里的行数必须是同一套算术：从前写「公司 1 张 · 本台待提交 2 张」，
     数的是两份名单的长度，而那是同一张票（用户：这里重复出现一个分单就该只有一条信息）。 */
  $("#mstHawbCnt").textContent = rows.length
    ? ("主单 " + mawb + " · 分单 " + rows.length + " 张 · 公司 " +
       rows.filter(r => r.company).length +
       (rows.some(r => !r.company) ? " · 本台未提交 " + rows.filter(r => !r.company).length : ""))
    : "";
  $("#mstHawbBox").innerHTML = mstTable(rows);
}
/* 点分单号 = 从主单侧回到那张分单自己的核对页：服务器上落盘的 L2/L3/质检直接装回票据列表，
   不需要制单员重新上传原件。改完还是走 /submit，红旗由服务端按当前值重算。 */
async function openHouse(stem){
  const hit = S.tickets.find(t => t.stem === stem);
  /* 种子行（刷新后从服务器种回来的那批）身上只有号与状态，点开它才去取完整那份。 */
  if (hit && hit.state !== "seed"){ mvOff(); S.sel = stem; render(); $("#view").scrollIntoView({behavior:"smooth", block:"start"}); return; }
  try{
    const r = await fetch(BASE + "/ticket/" + encodeURIComponent(stem));
    if (!r.ok){
      const j = await r.json().catch(() => ({}));
      throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    }
    const d = await r.json(), dr = drafts()[stem] || {};
    /* `/ticket` 给的是模型口径（air/raw/qc），人最后定下的那一版在两台机器之外：提交台账的
       air_sent 或暂存的 air_final。只认模型那一版会把人改过的值当没改过（2026-10-15 现场：
       提交过的票重开，发货人整串退回模型那一版，「变形了」），而确认过的红旗与空栏也会整片
       回潮（同一张票按铮写「仍有 13 项待核对」）。谁新用谁，算式在 L.serverState 一处。 */
    let st = null;
    try{
      const sr = await fetch(BASE + "/staged/" + encodeURIComponent(stem));
      if (sr.ok) st = await sr.json();
    }catch(e){ st = null; }
    const sf = L.serverState(d.submitted, st);
    const srvEdits = {}; sf.edits.forEach(k => { srvEdits[k] = 1; });
    const mg = L.stagedMerge(sf.values, dr.airE || {}, srvEdits);
    const uni = (a, b) => [...new Set((a || []).concat(b || []))];
    const t = {file:null, filename:d.filename || stem, stem:d.stem, state:"done", restored:"server",
               channel:d.channel, elapsed:d.elapsed,
               qc: Object.keys(d.qc || {}).length ? d.qc : null,
               raw:d.raw || {}, air:d.air || {}, airE:mg.airE,
               ackF:uni(dr.ackF, sf.acked), ackE:uni(dr.ackE, sf.ackedEmpty),
               skipE:sf.skippedEmpty,
               todoBase:null, ll:null,
               staged: st && !st.submitted_at ? {by:st.stager, role:st.stager_role,
                                                 at:st.staged_at, edited:Object.keys(st.edits || {}).length} : null,
               submitted: d.submitted ? {mode:"server", at:d.submitted.submitted_at,
                                         reviewer:d.submitted.reviewer} : null};
    /* 交出去的票：红旗改用后端按公司那一版终值现算的那份（flags_now），不再回放解析时对着
       模型值算的旧清单——人都改完发出去了、公司库里没那些问题，重开还催第二遍就是白催
       （2026-10-16 用户："已经提交的分单和主单为什么我第二次点进去还是提示我有红旗"）。
       落盘那份 qc 后端照旧原样发回：解析质量分读的是它，两处不许混成一个。 */
    if (Array.isArray(d.flags_now)){
      t.qc = Object.assign({}, t.qc || {}, {flags: d.flags_now, needs_review: d.flags_now.length > 0});
      t.flagsNow = d.flags_now_at || "";
    }
    if (hit) S.tickets[S.tickets.indexOf(hit)] = t; else S.tickets.push(t);
    mvOff(); S.sel = stem; render();
    $("#view").scrollIntoView({behavior:"smooth", block:"start"});
    const from = sf.from === "submit" ? "已提交过，载入发出去的那一版（改 " + sf.edits.length + " 列），改动需再点提交"
      : (st ? "载入 " + L.roleCn(st.stager_role) + " " + st.stager + " 暂存的核对结果（改 "
                 + sf.edits.length + " 列）" : "本机模型结果，改完点提交");
    toast("已载入分单 " + (t.air.HAWB_NO || stem) + "（" + from + "）", "ok");
    if (mg.conflicts.length){
      toast("注意：" + mg.conflicts.join("、") + " 你这台机器上的改动和服务器上的暂存不一致，" +
            "已按服务器版本显示；你那份没删，还在下面的「本机未保存的编辑」里", "bad");
    }
  }catch(e){ toast("打开这张分单失败：" + (e.message || e), "bad"); }
}
/* 刷新不该把一桌子的票变没（2026-10-15 用户：刷新页面啥都没了）。这一屏本来就是
   「本人今天在服务器上动过的票」，服务器算得出来，那就问它要回来：行先按号与状态种上，
   点开哪一张才去 /ticket 取完整那份。一开页面就把每张票的三个 JSON 全读一遍是白重的活。 */
function seedRow(r){
  return {file:null, filename:r.source_name || r.stem, stem:r.stem, hawb:r.hawb || "",
          mawb:r.mawb || "", state:"seed", raw:{}, air:{}, airE:{}, qc:null,
          ackF:[], ackE:[], skipE:[], srvFlags:[],
          seed:{flags:Number(r.flags) || 0,
                submitted:r.submitted ? {mode:"server", at:r.submitted.at,
                                         reviewer:r.submitted.reviewer} : null}};
}
async function restoreQueue(){
  let rows = [];
  try{
    const r = await fetch(BASE + "/day");
    const j = await r.json().catch(() => ({}));
    if (!r.ok) return;
    rows = (j.rows || []).filter(x => x.kind !== "master" && x.stem && !x.voided);
  }catch(e){ return; }              // 拿不到就照旧：空列表加一句「尚无票据」，别在开门时弹窗
  if (!rows.length) return;
  /* 这几百毫秒里人可能已经点开了本机未保存的编辑、或刚丢上去一张：那几张不能被补桌整个盖掉。
     同一个 stem 以服务器种的那一行为准（点开就是完整那份），其余原样留着。 */
  const keep = S.tickets.filter(t => !t.stem || !rows.some(r => r.stem === t.stem));
  S.tickets = rows.map(seedRow).concat(keep);
  const last = localStorage.getItem(LS.open) || "";
  const pick = S.tickets.some(t => t.stem === last) ? last : S.tickets[0].stem;
  await openHouse(pick);             // 回到离开时那一张：这一张取完整那份，其余点开再取
}
async function mstSearch(force){
  const m = $("#mstNo").value.trim(), st = $("#mstStatus");
  if (!m){ st.textContent = "请先填主单号"; return; }
  st.textContent = "检索中…";
  /* 打开着的若是另一张主单，先换成空壳：解析中那两栏（L1 原文、36 列核对表）要空着等新票，
     不能让上一张的内容冒充它。force 是「重新解析」那一路——同一张票自己的重跑，不清。 */
  if (!force && MV && L.normNo(m) !== L.normNo(MV.mawb)){ mvSave(); MV = mvShell(m); render(); }
  try{
    const r = await fetch(BASE + "/company/mawb?mawb=" + encodeURIComponent(m) + (force ? "&force=1" : ""));
    if (r.status === 401){ $("#gate").hidden = false; throw new Error("登录已过期，请重新登录"); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    st.textContent = "（" + j.mode + "）";
    MST_LAST = j;
    mstRender(j, m);
    if (j.master && j.master.state === "parsing") mstPoll(m);   // 解析在后台跑，卡片跟着刷新状态
    else if (MV && MV.mawb === j.mawb && MV.rec && MV.rec.state === "parsing" && j.master){
      mvSetRec(j.master); render();                             // 检索回来时已是现成的：空壳直接填上
    }
  }catch(e){ st.textContent = "检索失败：" + (e.message || e); $("#mstResult").innerHTML = ""; }
}
/* 解析中每 2 秒取一次结果：入口状态、以及（如果已经打开）主单核对区一起更新 */
function mstPoll(mawb){
  if (mstPoll._h) mstPoll._h.stop();               // 换一张主单：上一路轮询必须先停，否则两张卡的状态互相盖
  mstPoll._h = poller(async () => {
    const r = await fetch(BASE + "/master/" + encodeURIComponent(mawb));
    if (r.status === 401){                          // 登录过期后服务器只会一直回 401：停下来把遮罩亮出来
      $("#gate").hidden = false; toast("登录已过期，请重新登录", "bad"); return true;
    }
    if (!r.ok) return false;                        // 取不到就按退避再试，别在这里自己递归
    const rec = await r.json();
    if (!MST_LAST || MST_LAST.mawb !== mawb) return true;
    MST_LAST.master = rec;
    $("#mstResult").innerHTML = mstCard(MST_LAST, mawb);
    if (MV && MV.mawb === mawb){ mvSetRec(rec); render(); }
    return rec.state !== "parsing";
  }, {every: 2000, cap: 10000, maxMs: 3 * 60 * 1000,
      giveUp: () => toast("主单解析三分钟内没回话：稍后重新检索，或点「重新解析」再试", "bad")});
}
/* ── 主单核对与提交 ─────────────────────────────────────────────────── */
function mvValues(){
  const rec = MV.rec || {};
  return Object.assign({}, rec.ams || {}, MV.edit || {});
}
function mvFlags(){
  /* 交出去的主单读后端按公司终值现算的那份（flags_now），没发出去的照旧读解析时那批——
     与分单侧 `/ticket` 同一条口径（2026-10-16 用户："已经提交的分单和主单为什么我第二次
     点进去还是提示我有红旗"）。qc.flags 原样留着给解析质量分用，两处不许混。 */
  const rec = MV.rec || {};
  const base = Array.isArray(rec.flags_now) ? rec.flags_now : ((rec.qc && rec.qc.flags) || []);
  return [...new Set(base.concat(MV.extraFlags || []))];
}
/* 主单 36 列的改动和红旗确认同样是人的工作，不能刷新就丢：按 mawb 存本地。
   算式在 logic.js（L.draft*），这里只做 localStorage 与 MV 的接线；
   不和分单草稿共用一把键——分单存 airE/ackF，主单存 edit/acked，混着读会读出半截字段。 */
function mstDrafts(){ try { return JSON.parse(localStorage.getItem(LS.masterDrafts) || "{}"); } catch (e) { return {}; } }
function mvSave(){
  if (!MV || !MV.mawb) return;
  const entry = L.draftEntry(MV.editBase, MV.edit, MV.acked, MV.extraFlags, nowTxt());
  localStorage.setItem(LS.masterDrafts, JSON.stringify(L.draftPut(mstDrafts(), MV.mawb, entry)));
}
function mstDraftClear(mawb){
  localStorage.setItem(LS.masterDrafts, JSON.stringify(L.draftPut(mstDrafts(), mawb, null)));
}
function mvLabel(k){
  const f = ((MV && MV.rec && MV.rec.fields) || []).find(x => x[0] === k);
  return f ? f[1] : k;
}
function mvRestore(rec){
  const m = L.draftMerge(mstDrafts()[MV.mawb], (rec || {}).ams || {});
  /* 服务器上有别人（或自己上次）暂存的核对结果时，那份赢；本机未存的改动不删，冲突列点名。
     和分单侧同一条规则：静悄悄盖掉任何一边，人都会拿着错的版本往下核。 */
  const mg = L.stagedMerge(rec && rec.ams_final, m.edit, rec && rec.edits);
  MV.edit = mg.airE;
  /* 确认过哪些红旗也认服务器那份（顶上那一档）：只靠这台浏览器的草稿，刷新一次或换台机器
     再检索同一张主单，昨天逐条点过的确认今天又红一遍（2026-10-15 与分单侧同一个病）。 */
  MV.acked = [...new Set((rec && rec.acked_flags || []).concat(m.acked))];
  MV.extraFlags = m.extraFlags;
  MV.editBase = m.editBase; MV.conflicts = m.conflicts; MV.localDiff = mg.conflicts;
  MV.staged = (rec && rec.ams_final && !rec.submitted_at)
    ? {by: rec.stager || "?", role: rec.stager_role || "", at: rec.staged_at || ""} : null;
}
/* 换解析记录的唯一入口：重解析、轮询、提交后都走这里，绕开它就等于把刚恢复的草稿弄丢。 */
function mvSetRec(rec){
  if (!MV) return;
  MV.rec = rec;
  mvRestore(rec);
  mvSave();
}
function mvTouch(inp){
  const k = inp.dataset.mk;
  const m = L.draftTouch(MV.edit, MV.editBase, MV.conflicts, k, inp.value, (MV.rec.ams || {})[k]);
  MV.edit = m.edit; MV.editBase = m.editBase; MV.conflicts = m.conflicts;
  inp.closest("td").classList.toggle("edited", k in MV.edit);
  autoGrow(inp);
  mvSave();
  const msg = $("#mvMsg");
  if (msg) msg.textContent = Object.keys(MV.edit).length
    ? "已改 " + Object.keys(MV.edit).length + " 列（提交时服务端按当前值重算红旗）" : "";
}
async function mvReparse(){
  if (!MV) return;
  MV.msg = "重新解析中…"; render();
  await mstSearch(true);                       // force=1：越过缓存，换模型后也能重跑
  if (MV && MST_LAST && MST_LAST.mawb === MV.mawb){ mvSetRec(MST_LAST.master || MV.rec); render(); mvPoll(); }
}
function mvPoll(){
  if (MV_POLL) MV_POLL.stop();
  if (!MV || !MV.rec || MV.rec.state !== "parsing") return;
  MV_POLL = poller(async () => {
    const r = await fetch(BASE + "/master/" + encodeURIComponent(MV.mawb));
    if (r.status === 401){ $("#gate").hidden = false; toast("登录已过期，请重新登录", "bad"); return true; }
    if (!r.ok) return false;
    const rec = await r.json();
    if (!MV) return true;                          // 核对区已经关了：这一轮拿到什么都不要写回去
    mvSetRec(rec); render();
    return rec.state !== "parsing";
  }, {every: 2000, cap: 10000, maxMs: 3 * 60 * 1000,
      giveUp: () => toast("主单解析三分钟内没回话：可点「重新解析」再试", "bad")});
}
/* 关掉主单视图：轮询句柄必须跟着停，否则它会继续刷新一张已经不存在的核对区。 */
function mvOff(){ if (MV_POLL){ MV_POLL.stop(); MV_POLL = null; } MV = null; }
/* 检索另一张主单时的空壳：停在解析中那一档，两栏都先空着——上一张的原文与核对表要是继续
   挂着，看上去就像新票已经解析完了（2026-10-15 现场）。旧票的改动不受影响：mvSave 逐键落了盘。 */
function mvShell(mawb){
  return {mawb: mawb, order: {}, rec: {state: "parsing"}, editBase: {}, edit: {}, acked: [],
          extraFlags: [], conflicts: [], localDiff: [], staged: null, locked: "", submitted: null};
}
/* 主单暂存：把这 36 列存进服务器上的这条主单记录，并在公司那边占个位（状态 0，可反复改）。
   与分单同一个道理——从前 MV.edit 只在浏览器里，换台机器就什么都没交出去。 */
async function mvStage(){
  if (!MV) return;
  try{
    const r = await fetch(BASE + "/master/stage", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({mawb: MV.mawb, ams: mvValues(), acked_flags: MV.acked})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false){
      const d = j.detail;
      throw new Error((d && (d.message || (typeof d === "string" ? d : ""))) || j.error || ("HTTP " + r.status));
    }
    /* 锁行不是失败：气泡走中性色（绿色=全成了，红色=白点了，两样都不对）。
       toast 的第二参只许写字面量类别，所以这里分成两条写，不用三元拼。 */
    MV.locked = j.company && j.company.locked ? (j.company.status || 1) : "";
    if (MV.locked) toast(stageMsg(j.edited, j.company), "");
    else toast(stageMsg(j.edited, j.company), "ok");
    render();
    afterWrite({mawb: MV.mawb}, false);
  }catch(e){ toast("主单暂存失败：" + (e.message || e), "bad"); }
}
async function mvSubmit(){
  const who = $("#reviewer").value.trim();
  if (!who){ toast("先填复核人姓名：提交要留痕", "bad"); $("#reviewer").focus(); return; }
  const left = mvFlags().filter(f => !MV.acked.includes(f));
  if (left.length){
    toast("还有未确认的红旗：逐条点「确认无误」才能回传公司（" + left[0] +
          (left.length > 1 ? " 等 " + left.length + " 条" : "") + "）", "bad");
    return;
  }
  const btn = $("#mvSubmit"); if (btn) btn.disabled = true;
  try{
    const payload = {mawb: MV.mawb, reviewer: who, ams: mvValues(), acked_flags: MV.acked};
    const r = await fetch(BASE + "/master/submit", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)});
    const j = await r.json().catch(() => ({}));
    const d = j.detail || {};
    if (!r.ok){
      if (r.status === 400 && d.flags){        // 服务端重算出新红旗：接过来让人逐条确认
        MV.extraFlags = d.flags;
        MV.msg = "服务端重算后仍有 " + d.flags.length + " 条红旗，确认后再提交";
        mvSave();                              // 退回的红旗也是工作，刷新不能又丢一遍
        toast(MV.msg, "bad");
      } else throw new Error((typeof d === "string" ? d : d.message) || ("HTTP " + r.status));
    } else {
      MV.submitted = {at: nowTxt(), mode: j.mode};
      MV.edit = {}; MV.acked = []; MV.extraFlags = []; MV.conflicts = [];
      mstDraftClear(MV.mawb);                  // 已回传公司：草稿再留着，下次打开同一主单会把交过的改动重新盖上来
      mvSetRec(Object.assign({}, MV.rec, {ams: mvValues()}));
      MV.msg = ""; toast("主单 " + MV.mawb + " 已回传公司（" + (j.action || "ok") + "）", "ok");
      /* 主单交了，名下那些分单就从「被主单挡着」变成「可以提交了」：那张表必须按服务器重取一次，
         不然它还写着公司还没有这条分单记录。人接着做分单，所以这里不替他跳票。 */
      afterWrite({mawb: MV.mawb}, false);
    }
  }catch(e){ toast("提交失败：" + (e.message || e), "bad"); MV.msg = "提交失败：" + (e.message || e); }
  finally{ if (btn) btn.disabled = false; render(); }
}
$("#mstGo").addEventListener("click", () => mstSearch(false));
$("#mstHawbBox").addEventListener("click", e => {
  const a = e.target.closest("[data-open]"); if (!a) return;
  e.preventDefault(); openHouse(a.dataset.open);
});
$("#mstNo").addEventListener("keydown", e => { if (e.key === "Enter") mstSearch(false); });
/* 打开主单核对页的唯一入口：卡片里那颗「送入主单核对」与「主单还没提交发送」那道门的跳转
   共用这一份。两处各抄一遍，迟早一边改了另一边没改（这块已经为同一件事修过三次）。 */
function openMvFromMst(mawb){
  if (!MST_LAST || !mawb) return false;
  if (LOC.on) exitLocate();          // 定位模式的页图会被资料文本顶掉，先退干净免得留着半张高亮层
  MV = {mawb: mawb, order: MST_LAST.mawb_order || {}, rec: {},
        edit: {}, editBase: {}, acked: [], extraFlags: [], conflicts: [], msg: "", submitted: null};
  mvSetRec(MST_LAST.master || {});
  render(); mvPoll();
  $("#view").scrollIntoView({behavior: "smooth", block: "start"});   // 停在票面栏：窄屏两栏堆叠时核对区在它下面，滚到核对区会让人以为票面栏没动静
  toast((Object.keys(MV.edit).length
          ? "已载入主单 " + MV.mawb + "：本机未提交的 " + Object.keys(MV.edit).length + " 列改动已恢复"
            + (MV.conflicts.length ? "（其中 " + MV.conflicts.length + " 列与最新解析对不上，见下方红条）" : "")
          : "已载入主单 " + MV.mawb + " 的解析结果")
        + "（36 列可改，提交前由服务端重算红旗）", "ok");
  return true;
}
/* 分单提交被"先主单后分单"那道门挡下来时走这里：把人带到那条主单的核对页，
   过去由他自己点主单那颗提交。这个函数里出现 /master/submit 就是越界——那一发只能是人工。 */
async function gotoMaster(mawb){
  if (!mawb){ toast("这张分单没写主单号：先补上主单号", "bad"); return; }
  setView("desk");
  $("#mstNo").value = mawb;
  await mstSearch(false);
  /* 检索没回来（号在公司查不到、或回来的根本不是这条）就别开页：
     开一张空核对页，人只会当成系统坏了。 */
  if (!MST_LAST || L.normNo(String(MST_LAST.mawb || "")) !== L.normNo(mawb)){
    toast("主单 " + mawb + " 没检索回来：先去「主单检索」看清原因，再提交这张分单", "bad");
    return;
  }
  openMvFromMst(mawb);
}
$("#mstResult").addEventListener("click", e => {
  const use = e.target.closest("[data-mv]");
  if (use) openMvFromMst(use.dataset.mv);
});
/* ── 三个视图（§3.3）：核对工作台 / 今日台账 / 统计，同一页互切，票据与草稿都不重载 ─────
   今日台账读 /day：那一天这台机器经手过哪些票、谁提交的、还有几张没回公司。
   统计读 /stats：模型那一版与人最后定下的那一版逐列比（数只在 stats.py 算一处）。
   公司的发送状态不在这两张表里（要按主单去公司查，有限流），所以指路去「主单检索」。 */
const VIEW = {cur: "desk", rows: [], date: "", lastDate: "", machineLast: "",
              scope: "mine", others: 0, me: "", q: ""};
/* 台账看哪一批：只活在这一轮会话里，**不写本机偏好**——记住「全部」的下一次，
   人会把全局的数当成自己的数去报。默认那一档按角色给：录入员一进来就该看见活
   （待我复核），别人还是「我的」。 */
const DAY = {scope: ""};
const dayScopeDefault = () => (ME && ME.role === "inputter") ? "toreview" : "mine";
const ST = {rows: [], summary: {}, seg: "all"};
let MST_CARD_ON = false;
function syncCards(){
  const v = VIEW.cur;
  $("#wrap").hidden = v !== "desk";
  $("#dayCard").hidden = v !== "day";
  $("#mstHawbCard").hidden = v !== "desk" || !MST_CARD_ON;
}
function setView(v){
  VIEW.cur = (v === "day") ? v : "desk";
  ["desk", "day"].forEach(k => {
    const b = $("#view" + k.charAt(0).toUpperCase() + k.slice(1));
    b.classList.toggle("on", VIEW.cur === k);
    b.setAttribute("aria-selected", String(VIEW.cur === k));
  });
  syncCards();
  dayConfirm = "";                      // 换页就把「确认作废？」问句收掉：回来得重新点一次，不会留着等人误点
  if (VIEW.cur === "day") loadDay(true);
}
const todayStr = () => {
  const n = new Date(), p = x => String(x).padStart(2, "0");
  return n.getFullYear() + "-" + p(n.getMonth() + 1) + "-" + p(n.getDate());
};
/* fade=true 才是"换了一批数据"（切页/换日期/点刷新）——只有这时行才挂 dvin 播入场淡入。
   作废完重画传 false：那一屏还是同一批票，重播一遍淡入就是坑 9 说的"列表频繁闪"。 */
async function loadDay(fade){
  const box = $("#dayBox");
  box.innerHTML = '<div class="empty">正在读今天的台账…</div>';
  try{
    const picked = $("#dayDate").value;
    const kw = ($("#dayQ").value || "").trim();
    /* 输了号就回到「全部」这一档：搜索框问的是"这台机器上有没有这个号"，
       在当前那一档里筛出来的空页会写成"查无此号"，那是假话（票在，只是不在这档里）。
       哪一档亮着由服务端的 scope 说，这里不自己猜。 */
    let s = DAY.scope || dayScopeDefault();
    if (kw && (s === "toreview" || s === "reviewed")) s = "all";
    const q = [];
    /* 每一档都交给服务端去筛：在浏览器里筛的那批行本来就是服务端按人给出来的，
       筛不出别人的票，页上却写着"待你复核"——那就是这一页在说谎。 */
    if (kw) q.push("q=" + encodeURIComponent(kw));
    if (s === "all") q.push("all=1");
    else if (s === "toreview" || s === "reviewed") q.push("scope=" + s);
    else if (picked && picked !== VIEW.date) q.push("date=" + encodeURIComponent(picked));
    const r = await fetch(BASE + "/day" + (q.length ? "?" + q.join("&") : ""));
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    VIEW.date = j.date; VIEW.rows = j.rows || []; VIEW.lastDate = j.last_date || "";
    VIEW.machineLast = j.machine_last_date || ""; VIEW.others = Number(j.others_today) || 0;
    VIEW.me = j.me || ""; VIEW.q = j.q || "";
    /* 档认服务端说的那一份，不认刚才点了什么：角色拿不到的那一档服务端会退回自己的行，
       界面报的数必须和服务端发的是同一件事。 */
    VIEW.scope = ["mine", "all", "toreview", "reviewed"].includes(j.scope) ? j.scope : "mine";
    dayScopeSync();
    if (j.date) $("#dayDate").value = j.date;   // 以服务端那天为准：服务器与本机时区可能不同。
                                                // 跨天那几档回的是空日期，别把框清成一片空白
    renderDay(fade !== false);
  }catch(e){
    box.innerHTML = `<div class="empty empty-err">读台账失败：${esc(e.message || e)}</div>`;
  }
}
function dayScopeSync(){
  const box = $("#dayScope"), role = ME && ME.role;
  box.hidden = role === "reviewer";        // 制单员这一页只有自己的台账（服务端是同一判据，两边不许各判一次）
  /* 录入员不必再要一颗「我的」：他在这台机器上经手的票，全部都在那两档复核清单里。
     管理员四颗都要——他平时就是替两边看数的人。 */
  box.querySelectorAll("[data-ds]").forEach(b => {
    b.hidden = role === "inputter" && b.dataset.ds === "mine";
    const on = b.dataset.ds === VIEW.scope;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  const flat = VIEW.scope === "toreview" || VIEW.scope === "reviewed" || !!VIEW.q;
  const dw = $("#dayDateWrap");
  if (dw) dw.hidden = flat;                // 这几档跨所有日子，留着日期框会让人以为它还在起作用
}
/* 录入员那颗：只留一条"我看过了"的痕。它**不是**暂存——暂存会把核对结果写进服务器并顺手
   在公司那边占位（状态 0），而"先放着"这张票很可能还要回去问制单员。三件事各一颗：
   留痕 / 交给同事（暂存）/ 发走公司（提交），按钮的 title 就把这个分工写在原地上。 */
async function markReviewed(kind, key){
  const r = await fetch(BASE + "/review", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({kind: kind, key: key})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok){
    toast("留痕失败：" + (typeof j.detail === "string" ? j.detail : ("HTTP " + r.status)), "bad");
    return;
  }
  toast("已记下「" + ME.name + " 核过，先不发」——没有暂存，也没有发公司", "ok");
}
/* 空着的一页必须自己回答"是没票还是坏了"（用户："今天台账里面为什么没有内容了"）。
   选哪一句由 L.dayEmpty 判（四类各有用例），这里只把话画出来：四种各占一支，
   少一支就是有人问了"是不是丢了"而这一页不答。 */
function dayEmptyHtml(){
  const toAll = $("#dayScope").hidden
    ? "要查别人经手的票请找管理员。"
    : '也可以看全部：<button type="button" class="lk" data-ds="all" title="切到这台机器所有人的台账">切到「全部」</button>。';
  const link = d => `<button type="button" class="lk" data-day="${esc(d)}" title="翻到那一天">${esc(d)}</button>`;
  const SAY = {
    others: () => `<div class="empty">这一天你没有动过票。这台机器这一天有 ${esc(VIEW.others)} 张是别人经手的。` + toAll + "</div>",
    last: () => `<div class="empty">${VIEW.scope === "mine" ? "这一天你没动过票。你最近动过票的是 "
        : "这一天这台机器没经手过票。最近有票的是 "}` + link(VIEW.lastDate)
        + "（点一下就过去；换日期也可以用上面那个日期框）。</div>",
    never: () => '<div class="empty">你在这台机器上还没有过台账记录。这台机器最近有票的是 '
        + link(VIEW.machineLast) + "。" + toAll + "</div>",
    clean: () => '<div class="empty">没有待复核的票：这台机器上别人暂存的，要么已经核过、要么已经发走公司了。'
        + '核过还没发的在 <button type="button" class="lk" data-ds="reviewed"'
        + ' title="这些不需要再看第二遍，只等谁点提交">「我核过·没提交」</button> 那一档。</div>',
    nohit: () => `<div class="empty">这台机器上没有号是 ${esc(VIEW.q)} 的票（带不带连字符都算同一个号）。`
        + "台账是本机各记各账的：另一台机器上提交过的票，在这里查不到。</div>",
    none: () => '<div class="empty">这台机器还没有过任何台账记录。</div>'
  };
  return (SAY[L.dayEmpty(VIEW)] || SAY.none)();
}
/* 今日归档：分单与主单合一张高密度表（行上带 kind），按主单聚簇、点箭头原地平滑折叠。
   交接原型 archive-dense-view.html 第 5 节那九条坑点各有用例钉着（tests/test_day_dense_view.py）：
   折叠不重建 DOM、过渡里不掺 min-height、收尾先关过渡再清内联值、定时器按组分开存、
   入场淡入只在真的换了一批数据时播。改这块之前先跑那一组。
   作废是"从台账与统计里收起来"的软闸门：票面原件、暂存记录、提交台账与复合键索引一律不动。
   dayConfirm 只活在内存里：刷新页面就该问第二遍，记进 localStorage 反而会把上一轮的确认
   留到下一次误点。 */
const DV = {filter: "all", sort: "cluster", collapsed: new Set(), timers: {}};
const DV_ANIM = 250;        // 收尾比 CSS 那条 --dv-fold(.24s) 晚一点：早了会把过渡掐在半路
/* 两面小图标都写 stroke="currentColor"，颜色交给 CSS：desk.js 里出现一个色值就是回归
   （test_server_http.test_color_literals_only_live_in_tokens）。 */
const DV_FLAG_SVG = '<svg width="10" height="10" viewBox="0 0 14 14" fill="none" aria-hidden="true">'
  + '<path d="M3.5 1.5v11M3.5 2h7.6l-1.6 2.4 1.6 2.4H3.5" stroke="currentColor" stroke-width="1.5"'
  + ' stroke-linecap="round" stroke-linejoin="round"/></svg>';
const DV_CHEV_SVG = '<svg width="10" height="10" viewBox="0 0 12 12" fill="none" aria-hidden="true">'
  + '<path d="M2.5 4.5L6 8l3.5-3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"'
  + ' stroke-linejoin="round"/></svg>';
let dayOnlyVoid = false, dayConfirm = "";
const KIND_CN = {hawb: "分单", mawb: "主单"};
function dvRowH(){
  /* 目标行高从令牌读回来，不在这里另写一个 31：CSS 用 --dv-row 决定静态行高，
     动画又要知道该收到多少，两边各写一份的话改一次密度就动画到一半停住。 */
  const cozy = document.body.dataset.density === "cozy";
  const v = getComputedStyle(document.body).getPropertyValue(cozy ? "--dv-row-cozy" : "--dv-row");
  return parseFloat(v) || 31;
}
/* 密度是本机偏好（各人屏幕不一样，不该互相将就），存 LS.dayDensity 不存账号。
   行高只有 CSS 一处说了算：切档改 body[data-density]，浏览器自己换 min-height，不重渲染整表。 */
function dvInitDensity(){
  const d = localStorage.getItem(LS.dayDensity) === "cozy" ? "cozy" : "compact";
  document.body.dataset.density = d;
  document.querySelectorAll("#dayDensity button")
    .forEach(b => b.classList.toggle("on", b.dataset.d === d));
}
function dvGroup(pk){
  const box = $("#dayBox");
  return {master: box.querySelector('tr.dvm[data-dvm="' + CSS.escape(pk) + '"]'),
          subs: Array.prototype.slice.call(box.querySelectorAll('tr.dvs[data-dvp="' + CSS.escape(pk) + '"]'))};
}
function dvAnimate(pk, collapsing){
  const g = dvGroup(pk);
  if (!g.master) return;
  const targetH = dvRowH();
  g.master.classList.toggle("show-hint", collapsing);
  const chev = g.master.querySelector(".dvchev");
  if (chev) chev.classList.toggle("closed", collapsing);
  if (!g.subs.length) return;      // 当前筛法下这一组没有分单行：只更新箭头与「已折叠」提示
  clearTimeout(DV.timers[pk]);
  g.subs.forEach(row => {
    const inners = Array.prototype.slice.call(row.querySelectorAll(".dvci"));
    /* 起点量当前实际高度：动画中途再点要从半路接续，写死 0 或目标值都会跳一下。
       收起来时取 max(实测, 目标)——舒适档下人刚拖过的行可能比令牌值高。 */
    const from = collapsing
      ? Math.max(inners[0].offsetHeight, targetH)
      : (row.classList.contains("is-collapsed") ? 0 : inners[0].offsetHeight);
    if (!collapsing) row.classList.remove("is-collapsed");
    row.classList.add("folding");
    inners.forEach(el => {
      el.style.minHeight = "0px";
      el.style.height = from + "px";
      el.style.opacity = from === 0 ? "0" : "1";
    });
    void row.offsetWidth;                        // 强制 reflow：不隔一帧两次改动会被合并成一次
    requestAnimationFrame(() => inners.forEach(el => {
      el.style.height = (collapsing ? 0 : targetH) + "px";
      el.style.opacity = collapsing ? "0" : "1";
    }));
  });
  DV.timers[pk] = setTimeout(() => {
    g.subs.forEach(row => {
      row.classList.remove("folding");
      if (collapsing) row.classList.add("is-collapsed");
      /* 顺序不能反：先关过渡、强制 reflow，再清内联值交还 CSS，否则残留属性变化会触发
         第二次动画（"展开到 31px 又掉回去再爬回来"那个抖动的根因）。 */
      row.querySelectorAll(".dvci").forEach(el => {
        el.style.transition = "none";
        el.style.height = ""; el.style.minHeight = ""; el.style.opacity = "";
      });
    });
    void $("#dayBox").offsetWidth;
    g.subs.forEach(row => row.querySelectorAll(".dvci").forEach(el => { el.style.transition = ""; }));
  }, DV_ANIM + 20);
}
function dvToggle(pk){
  const collapsing = !DV.collapsed.has(pk);
  if (collapsing) DV.collapsed.add(pk); else DV.collapsed.delete(pk);
  dvAnimate(pk, collapsing);
  dvSyncCollapse();
}
function dvGroups(){
  const box = $("#dayBox"), withSubs = new Set();
  box.querySelectorAll("tr.dvs[data-dvp]").forEach(tr => withSubs.add(tr.dataset.dvp));
  return Array.prototype.slice.call(box.querySelectorAll("tr.dvm[data-dvm]"))
    .map(tr => tr.dataset.dvm).filter(pk => withSubs.has(pk));
}
function dvAnyOpen(){ return dvGroups().some(pk => !DV.collapsed.has(pk)); }
function dvSyncCollapse(){
  $("#dayCollapse").textContent = dvAnyOpen() ? "全部折叠" : "全部展开";
}
function dvStatHtml(b){
  /* 红旗那一格报的是"有红旗的票数"，不是红旗标记总数（交接文档第 3 节点名了这一条）；
     总数放在悬停提示里，两个口径都留着，别让人以为它们在互相打脸。 */
  return `<span>共 <em>${b.total}</em> 票</span>`
    + `<span>主单 <em>${b.masters}</em></span>`
    + `<span class="dvr" title="红旗标记一共 ${b.flags} 条">红旗 <em>${b.flagged}</em> 票</span>`
    + `<span>未提交 <em>${b.pending}</em></span>`
    + (b.failed ? `<span>失败 <em>${b.failed}</em></span>` : "")
    + (b.voided ? `<span>已作废 <em>${b.voided}</em></span>` : "");
}
function voidCell(r){
  /* 操作列只有 96px，两步确认竖着排（desk.css 里 td.c-op 那道 flex-wrap）：
     横着排会被裁掉一半，装不下的确认按钮等于没有确认。
     作废与恢复只对管理员开放（2026-10-14 用户改口径）：非管理员这一格什么都不画。
     "为什么这行没有按钮"写在「只看作废」那颗勾选的说明里，不在每一行上重复。 */
  const admin = !!ME && ME.role === "admin";
  if (!admin) return "";
  const k = r.kind + "|" + (r.kind === "mawb" ? r.mawb : r.stem);
  if (r.voided)
    return '<button class="btn sm" type="button" data-undo="' + esc(k) + '" title="当初作废：' +
           esc(r.voided.by || "?") + " " + esc(r.voided.at || "") + '">恢复</button>';
  if (dayConfirm === k)
    return '<button class="btn sm danger" type="button" data-void="' + esc(k) +
           '" title="作废这一行？票面原件、暂存记录与提交台账都不动，随时能恢复">确认作废</button>' +
           '<button class="btn sm" type="button" data-vno="1" title="取消，什么都不动">取消</button>';
  return '<button class="btn sm gh" type="button" data-void="' + esc(k) +
         '" title="从台账与统计里收起来，随时能恢复；票面原件、暂存与提交台账一律不动">作废</button>';
}
function dvRowHtml(r, subs, fade){
  const m = r.kind === "mawb", pk = L.dayKey(r), closed = DV.collapsed.has(pk);
  const no = m ? (r.mawb || "没取到主单号") : (r.hawb || r.stem || "没取到分单号");
  /* 箭头排在最前面，而且必须是真按钮：伪链接协议那种写法键盘到不了、读屏器也念不出来。
     没有分单的那一组给占位箭头（visibility:hidden）保住单号的对齐，但不给点。 */
  const chev = !m ? ""
    : (subs[pk] > 0
      ? `<button type="button" class="dvchev${closed ? " closed" : ""}" data-dvk="${esc(pk)}"`
        + ` title="折叠 / 展开名下 ${subs[pk]} 张分单" aria-expanded="${closed ? "false" : "true"}">${DV_CHEV_SVG}</button>`
      : `<button type="button" class="dvchev dvnone" tabindex="-1" aria-hidden="true">${DV_CHEV_SVG}</button>`);
  const doc = m
    ? `<span class="dvdoc">${chev}<span class="dvk m">主单</span>`
      + `<button type="button" class="dvno m" data-mopen="${esc(r.mawb)}"`
      + ` title="回工作台按这个主单号检索并打开核对区">${esc(no)}</button>`
      + `<span class="dvfold">分单 ${subs[pk] || 0} 已折叠</span></span>`
    : `<span class="dvdoc dvind"><span class="dvk s">分单</span>`
      + (r.has_original
        ? `<button type="button" class="dvno s" data-open="${esc(r.stem)}"`
          + ` title="回到工作台接着核这张票">${esc(no)}</button>`
        : `<span class="dvno s" title="原件没归档，回不去核对页">${esc(no)}</span>`)
      + `<span class="dvpm">${esc(L.normNo(r.mawb) || "无主单号")}</span></span>`;
  const st = r.failed
    ? `<span class="dvst bad" title="${esc(r.error || "")}"><i></i>解析失败</span>`
    : r.submitted
      ? `<span class="dvst ok"><i></i>已提交${m ? ` <small>${r.filled}/${r.cols_total}</small>` : ""}</span>`
      : `<span class="dvst no"><i></i>未提交${m ? ` <small>${r.filled}/${r.cols_total}</small>` : ""}</span>`;
  const flag = (r.flags || 0) > 0
    ? `<span class="dvfc" title="这一票有 ${r.flags} 条红旗标记">${DV_FLAG_SVG}×${r.flags}</span>`
    : '<span class="dvfc none">—</span>';
  const sub = r.submitted;
  const rec = sub
    ? `<span class="dvrec" title="${esc((sub.reviewer || "?") + " · " + (sub.at || "")
        + (sub.action ? " · " + sub.action : ""))}">${esc(sub.reviewer || "?")} `
      + `<span class="dvt">${esc(L.shortTime(sub.at))}</span>${sub.action ? " " + esc(sub.action) : ""}</span>`
    : '<span class="dvdash">—</span>';
  const snap = `<span class="dvsnap" title="${esc("解析渠道 " + (r.channel || "?") + " · 耗时 "
      + (r.elapsed || 0) + "s · " + (r.processed_at || ""))}">${esc(r.channel || "?")} · ${r.elapsed || 0}s</span>`;
  const att = r.has_original
    ? `<a class="dvatt" href="${BASE}${m ? "/mawb/source/" + encodeURIComponent(r.mawb)
                                          : "/source/" + encodeURIComponent(r.stem)}"`
      + ' target="_blank" rel="noopener" title="新标签页打开归档的原件">原件</a>'
    : '<span class="dvdash" title="这一票没有归档原件">—</span>';
  const cls = ["dvm", "dvs"][m ? 0 : 1]
    + ((r.flags || 0) > 0 ? " flagged" : "") + (r.voided ? " voided" : "") + (fade ? " dvin" : "")
    + (m ? (closed ? " show-hint" : "") : (DV.collapsed.has(pk) ? " is-collapsed" : ""));
  const key = m ? ` data-dvm="${esc(pk)}"` : ` data-dvp="${esc(pk)}"`;
  return `<tr class="${cls}"${key} title="${esc(KIND_CN[r.kind] || r.kind)}`
    + `${r.voided ? " · 已作废（" + esc(r.voided.by || "?") + "）" : ""}`
    + `${r.uploader ? " · 上传 " + esc(r.uploader) : ""}${r.stager ? " · 暂存 " + esc(r.stager) : ""}">`
    + `<td><div class="dvci">${doc}</div></td>`
    + `<td><div class="dvci">${st}</div></td>`
    + `<td class="c-flag"><div class="dvci">${flag}</div></td>`
    + `<td><div class="dvci">${rec}</div></td>`
    + `<td><div class="dvci">${snap}</div></td>`
    + `<td><div class="dvci">${att}</div></td>`
    + `<td class="c-op"><div class="dvci">${voidCell(r)}</div></td></tr>`;
}
function renderDay(fade){
  const live = L.dayVisible(VIEW.rows, dayOnlyVoid);
  $("#dayStats").innerHTML = dvStatHtml(L.dayBar(VIEW.rows));
  const subs = L.daySubCounts(live);
  const rows = L.dayOrder(L.dayFilter(live, DV.filter), DV.sort);
  if (!rows.length){
    $("#dayBox").innerHTML = VIEW.rows.length
      ? '<div class="empty">' + (dayOnlyVoid ? "这一天没有作废的票。"
          : "当前筛选下没有票。换个档位，或把「只看作废」勾上翻回作废的那些。") + '</div>'
      : dayEmptyHtml();
    dvSyncCollapse();
    return;
  }
  /* 折叠态是全局的（以归一化主单号为键），换筛选/排序/密度都保留：重画时按 DV.collapsed
     把 is-collapsed、箭头那 90° 与「分单 N 已折叠」提示一起还原，否则人一筛就丢了刚才收好的那几组。 */
  $("#dayBox").innerHTML = `<table><thead><tr>
      <th class="w-doc">单据</th><th>状态</th><th class="w-flag">红旗</th>
      <th class="w-rec">提交记录</th><th class="w-snap">缩影</th><th class="w-att">附件</th>
      <th class="w-op">操作</th></tr></thead>
      <tbody>${rows.map(r => dvRowHtml(r, subs, fade)).join("")}</tbody></table>
      <p class="hint hintp">「公司发送状态」要按主单去公司系统查（限流 10 次/秒）：点顶栏「主单检索」查那张主单下的全部票。</p>`;
  dvSyncCollapse();
}
/* 统计页：数全部来自 /stats 的 summary（Python 一处算），这里只排版与换算百分号。
   为什么不让前端也算一遍：同一套算术写两遍迟早给出两个百分比，而这些数要拿去做决定
   ——今日台账那页就是为这件事专门留了一条用例。 */
/* 百分比拆成「数」与「单位」两段：看板上大号数字 25px、单位 13px 灰（交接文档 §4.2）。
   整串返回的写法会把 % 一起放大到 25px。没有分母时仍是破折号——不许编一个 100% 出来。 */
function pctParts(v){
  return (v === null || v === undefined) ? {n: "—", u: ""} : {n: (v * 100).toFixed(1), u: "%"};
}
/* 改动字段那一格：只显示前 2 个标签，其余收 +n，全部字段名进 title。
   原版把整串字段名塞进一格换行，一行能占掉三行的高度——那是这一页最难看的一处。 */
function stTagCell(list, label){
  const all = list || [], tg = L.statsTags(all);
  if (!tg.shown.length) return '<span class="stempty">—</span>';
  return `<span class="sttags" title="${esc(label + all.length + " 列：" + all.join("、"))}">`
    + tg.shown.map(f => `<span class="sttag">${esc(f)}</span>`).join("")
    + (tg.more ? `<span class="sttag more">+${tg.more}</span>` : "")
    + "</span>";
}
/* 六张卡的顺序、语义色、单位与那句灰色解释语都按交接文档 §3.2 钉在这儿。
   值一律取服务器算好的那一份，这里只管摆——同一套算术写两遍迟早给出两个百分比。 */
const ST_KPI = [
  {k: "acc_field", lb: "逐字段准确率", ds: "模型本身读得准不准", rate: true, v: "blue", bar: "blue"},
  {k: "acc_after_review", lb: "复核后准确率", ds: "人还剩多少活；能否对接看这个", rate: true, v: "green", bar: "green"},
  {k: "clean_rate", lb: "一次通过率", ds: "一张没改的票占比", rate: true, v: "amber", bar: "amber"},
  {k: "edits_doc", lb: "制单员改的列", ds: "人修正模型的字段数", u: " 列", v: "", bar: ""},
  {k: "edits_inp", lb: "录入员改的列", ds: "录入环节的二次改动", u: " 列", v: "green", bar: ""},
  {k: "unreviewed", lb: "未复核就发出", ds: "未经录入员复核", u: " 张", v: "red", bar: ""}];
function statsQS(){
  /* 只拼查询串，URL 前缀留在调用点上写死带 BASE —— 页面挂在 /hawb/ 下，
     test_server_http 那条守卫盯的就是调用点第一个词不是 BASE 的写法，别绕开它。 */
  const q = [];
  if ($("#stFrom").value) q.push("from=" + encodeURIComponent($("#stFrom").value));
  if ($("#stTo").value) q.push("to=" + encodeURIComponent($("#stTo").value));
  if ($("#stGroup").value) q.push("group_by=" + encodeURIComponent($("#stGroup").value));
  return q.length ? "?" + q.join("&") : "";
}
async function loadStats(){
  const box = $("#stBox");
  box.innerHTML = '<div class="empty">正在比模型那一版与人定下的那一版…</div>';
  try{
    const r = await fetch(BASE + "/stats" + statsQS());
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    ST.rows = j.rows || []; ST.summary = j.summary || {};
    renderStats();
  }catch(e){
    box.innerHTML = `<div class="empty empty-err">读统计失败：${esc(e.message || e)}</div>`;
  }
}
function renderStats(){
  /* 看板骨架：口径条 → 六张卡 → 左条形图 + 右明细表。只有这一层播入场动画，
     明细 tbody 交给 renderStatsRows 单独重画——否则每点一次分段筛选，六张卡就跟着淡入一遍。 */
  const s = ST.summary, box = $("#stBox");
  const cnt = L.statsCounts(ST.rows);
  const cl = L.statsClosure(s, ST.rows);
  const ok = cl.rowsMatch && cl.colsMatch;
  /* 口径条那句「不进分母」不是装饰：少了它，「可算 4 张」会被读成「这个窗口里一共只有 4 张票」。
     两条恒等式对不上时整条变脸——那时候画出来的百分比与明细不是同一批票，不能给人用。 */
  const scope = `<div class="stscope${ok ? "" : " broken"}" id="stScope" role="status">`
    + `<span>可算 <b>${s.tickets || 0}</b> 张</span>`
    + `<span>可比 <b>${s.fields_comparable || 0}</b> 列</span>`
    + `<span>历史无逐字段留痕 <b>${s.legacy || 0}</b> 张</span>`
    + `<span class="note">（无留痕单据不进入准确率分母，仅在下方明细备查）</span>`
    + (ok ? "" : `<span class="bad">口径与明细对不上：可算 ${s.tickets || 0} + 无留痕 ${s.legacy || 0}`
        + ` ≠ 明细 ${ST.rows.length} 行，可比列合计 ${s.fields_comparable || 0}。`
        + `下面的百分比先别用，刷新一次看看。</span>`)
    + `</div>`;
  const kpis = '<div class="stkpis" id="stKpis">' + ST_KPI.map(c => {
    const raw = s[c.k];
    const p = c.rate ? pctParts(raw) : {n: String(raw || 0), u: c.u};
    return `<div class="stkpi${c.v ? " v-" + c.v : ""}"><div class="lb">${esc(c.lb)}</div>`
      + `<div class="ds">${esc(c.ds)}</div>`
      + `<div class="vl">${esc(p.n)}<small>${esc(p.u)}</small></div>`
      + (c.bar ? `<div class="sttrack ${c.bar}"><i></i></div>` : "")
      + `</div>`;
  }).join("") + "</div>";
  /* 条形图只画前 12 列（与改版前同一个上限）：by_field 已经按被改次数降序，切掉尾巴不影响
     "最长那根 = 100%" 的比例。剩下的在底部那块归属信息里报个数，不悄悄吞掉。 */
  const all = s.by_field || [];
  const bars = L.statsBars(all.slice(0, 12));
  const barsPanel = '<div class="stpanel"><div class="stph"><h2>高频改动列</h2>'
    + '<p>该修提示词还是码表，看这里</p></div><div class="stbars" id="stBars">'
    + (bars.length
      ? bars.map(b => `<div class="stbar"><div class="top">`
          + `<span class="fn" title="${esc(b.field)}">${esc(b.field)}</span>`
          + `<span class="n">×${b.edits}</span></div>`
          + `<div class="stbtrack"><div class="stbfill"></div></div></div>`).join("")
      : '<div class="stempty">窗口内没有任何改动：模型读的与人定下的完全一致。</div>')
    + '</div><div class="stcolscope">'
    /* 原型在这儿写死了「涉及 1 票 VCE4373」。真实的 by_field 只有 {field,edits,tickets}，
       不带是哪几张票——要点名票就得改后端，而交接文档 §9 说本次不动后端，所以只报聚合的张数。 */
    + `<span class="stchip">涉及 ${cnt.changed} 票</span>`
    + `<span class="stchip">${all.length} 列被改过</span>`
    + (all.length > bars.length ? `<span class="stchip">另有 ${all.length - bars.length} 列未显示</span>` : "")
    + "</div></div>";
  const segs = [["all", "全部"], ["changed", "有改动"], ["clean", "改动 0"], ["trace", "无留痕"]];
  const detail = '<div class="stpanel"><div class="stph"><h2>逐张明细</h2>'
    + '<div class="stseg" id="stSeg" role="group" aria-label="明细筛选">'
    + segs.map(g => `<button type="button" data-sf="${g[0]}"`
        + `${ST.seg === g[0] ? ' class="on" aria-pressed="true"' : ' aria-pressed="false"'}>`
        + `${esc(g[1])} <span class="c">${cnt[g[0]]}</span></button>`).join("")
    + '</div></div><div class="stwrap"><table id="stTable"><colgroup>'
    /* 列宽走 col 元素：那条「内联样式只放行两种」的守卫认的就是这一种，
       而 fixed 布局之下也只有 col 给的宽度是真宽度（长字段名撑不开列）。 */
    + '<col style="width:126px"><col style="width:150px"><col style="width:136px"><col style="width:88px">'
    + '<col style="width:74px"><col style="width:66px"><col style="width:250px"><col style="width:150px">'
    + '</colgroup><thead><tr><th>主单号</th><th>分单号</th><th>上传人</th><th>暂存人</th>'
    + '<th>可比</th><th>改动</th><th>制单员改的</th><th>录入员改的</th></tr></thead>'
    + '<tbody id="stRows"></tbody></table></div></div>';
  let html = scope + kpis + '<div class="stmain" id="stMain">' + barsPanel + detail + "</div>";
  if (!s.tickets){
    html += '<div class="empty stbelow">窗口内没有可算的票：点过「暂存」或「提交」之后才有逐字段留痕。'
      + '<span class="sub">本功能上线前的台账只存了字段名与哈希，比不出值，已计入上面「历史无逐字段留痕」。</span></div>';
  }
  /* 分组那一块是接好后端的四个轴（模型/角色/上传人/列），原型里没有——但删掉它就是趁改版砍功能。 */
  if ((s.groups || []).length){
    const ks = Object.keys(s.groups[0]);
    html += '<div class="stpanel stbelow" id="stGroups"><div class="stph"><h2>分组看</h2></div>'
      + '<div class="stwrap"><table class="list"><thead><tr>'
      + ks.map(c => `<th>${esc(c)}</th>`).join("") + "</tr></thead><tbody>"
      + s.groups.map(g => "<tr>" + ks.map(c => `<td>${esc(String(g[c]))}</td>`).join("") + "</tr>").join("")
      + "</tbody></table></div></div>";
  }
  html += '<p class="stfoot"><b>读法：</b>上面六张卡速读准确率与人的工作量，左边条形图定位「最该修的列」，'
    + '右边明细用「有改动 / 无留痕」分段切换，<b>无留痕行自动弱化</b>而不是满屏刷屏。'
    + '两个数不能混：<b>逐字段准确率</b>问模型读得准不准；<b>复核后准确率</b>问录入员还剩多少活'
    + '——能不能把提取结果直接喂给平台，看后者。没暂存记录就直发的票，改动全记在制单员档'
    + '（那些值确实是核对台上的人改的），「未复核就发出」那一张卡是它的对照组。'
    + '想知道某张票另外那几个被折叠的列是什么，把鼠标停在标签上。</p>';
  box.innerHTML = html;
  stPaint(bars);
  renderStatsRows();
}
/* 进度条宽度、条形宽度与生长延迟都是"值当场算出来"的数据，但内联样式只放行 left 与 col 两种写法，
   所以一律等骨架进了 DOM 再由这里写回元素。条形只写 width（比例），长度动画交给 CSS 的 scaleX——
   动画化 width 每帧都触发布局，列一多就掉帧。 */
function stPaint(bars){
  const box = $("#stBox"), s = ST.summary;
  const fills = box.querySelectorAll(".sttrack i");
  let fi = 0;
  ST_KPI.forEach(c => {
    if (!c.bar) return;
    const v = s[c.k];
    const w = (v === null || v === undefined) ? 0 : Math.max(0, Math.min(1, v)) * 100;
    if (fills[fi]) fills[fi].style.width = w.toFixed(1) + "%";
    fi += 1;
  });
  const bf = box.querySelectorAll(".stbfill");
  (bars || []).forEach((b, i) => {
    if (!bf[i]) return;
    bf[i].style.width = (b.pct * 100).toFixed(1) + "%";
    bf[i].style.animationDelay = (0.05 + i * 0.07).toFixed(2) + "s";   // 0.07s 一档依次长出来
  });
}
/* 明细 tbody 单独重画：换分段只换这一段，六张卡与条形图都不重建，所以入场动画不会重播。
   行底色三档（有改动铺暖色 / 无留痕弱化 / 其余正常）由 L.statsSeg 的归属决定，与筛选同一个判据。 */
function renderStatsRows(){
  const tb = $("#stRows");
  if (!tb) return;
  tb.innerHTML = L.statsFilter(ST.rows, ST.seg).map(r => {
    /* 两个号各占一列（2026-10-09 用户："挤在一格里眼花缭乱"）：连成一格时列一窄就断在点号后面，
       每格变成"半行 + 孤零零一行"，竖着看全是锯齿。一个号都没解析出来时才退回票名——
       整列都写"缺号"反而认不出是哪张；票名与原始文件名留在悬停提示里，找回那张 PDF 全靠它。 */
    const tip = (r.stem || "") + (r.source_name && r.source_name !== r.stem ? " · " + r.source_name : "");
    const m = r.mawb ? esc(r.mawb) : (r.hawb ? "—" : esc(r.stem));
    const seg = L.statsSeg(r);
    const person = r.uploader
      ? `<span class="stperson">${esc(r.uploader)}${r.uploader_role ? ` <small>（${esc(L.roleCn(r.uploader_role))}）</small>` : ""}</span>`
      : '<span class="stempty">—</span>';
    /* 无留痕那一格写清楚是「无留痕」，不是空着让人以为没数；改动那一格同理给破折号。 */
    const cmp = r.legacy ? '<span class="stcmp notrace">无留痕</span>'
      : `<span class="stcmp">${r.comparable}</span>`;
    const diff = (r.total === null || r.total === undefined) ? '<span class="stempty">—</span>'
      : `<span class="stdiff ${r.total > 0 ? "has" : "zero"}">${r.total}</span>`;
    return `<tr class="${seg === "trace" ? "sttrace" : (seg === "changed" ? "stchanged" : "")}">`
      + `<td class="tno"><div class="stci"><span class="stnum stmno" title="${esc(tip)}">${m}</span></div></td>`
      + `<td class="tno"><div class="stci"><span class="stnum stsno">${esc(r.hawb || "—")}</span></div></td>`
      + `<td><div class="stci">${person}</div></td>`
      + `<td><div class="stci">${r.stager ? `<span class="stperson">${esc(r.stager)}</span>` : '<span class="stempty">—</span>'}</div></td>`
      + `<td><div class="stci">${cmp}</div></td>`
      + `<td><div class="stci">${diff}</div></td>`
      + `<td><div class="stci">${stTagCell(r.doc, "制单员改了 ")}</div></td>`
      + `<td><div class="stci">${stTagCell(r.inp, "录入员改了 ")}</div></td></tr>`;
  }).join("");
}
/* ── 数据清理（仅管理员）──────────────────────────────────────────────────────
   这一页删的是不可逆的东西，所以三条规矩钉死在这里：名单只由服务端算（搜索框只过滤、不放宽）、
   第一下只把按钮换成"确认"而不发任何请求、执行结果逐张点名而不是一句"成功"。
   界面上绝不出现服务器绝对路径或"要删哪些文件"——那是服务端 plan 的事。
   v1.15.0 起这份名单同时是自动清理的名册：判据是「最后一次被碰之后安静满 N 天」，
   定时器（另一个进程，见 deploy/hawb-purge.timer）每天删一轮，人这一页只剩"要提前删"这一个动作。
   所以默认一张都不勾：不可逆操作不该开门就全选、点两下抹光。 */
const PURGE = {items: [], q: "", f: "all", sel: {}, armed: false, days: 30, run: null};
function purgeAged(){ return PURGE.items.filter(it => it && it.aged === true); }
function purgeShown(){ return L.purgeFilter(PURGE.items, PURGE.q, PURGE.f); }
function purgePicked(){ return purgeShown().filter(it => PURGE.sel[it.key] === true); }
/* 到期张数写在顶栏「设置」那颗按钮上（入口收进设置之后，徽标得跟着走——
   否则"今天有没有票到期"只能靠记得点开设置才知道）。0 张就清空，不留一个假的 0。 */
function purgeBadge(n){ const b = $("#btnSetCnt"); if (b) b.textContent = n ? " " + n : ""; }
function drawPurgeRun(){
  /* 「上次自动清理」来自服务端读的那份状态文件：定时器跑在另一个进程里，web 进程既不知道
     它跑没跑、也不知道它失败过。没有这本书就说「从没跑过」，不能留一个空日期看着像跑过。 */
  const r = PURGE.run, b = $("#purgeRun");
  if (!r || !r.at){ b.innerHTML = "上次自动清理：<b>从没跑过</b>"
    + '<span class="sub">定时器那一份（deploy/hawb-purge.timer）还没装。装上之前，躺够的票只进这份名册，不会自己消失。</span>'; return; }
  const bad = Number(r.failed) || 0;
  b.innerHTML = "上次自动清理：" + esc(L.shortTime(r.at)) + " · 查了 " + (Number(r.checked) || 0)
    + " 张，删掉 " + (Number(r.removed) || 0) + " 张"
    + (bad ? ' · <b class="pkr-b">' + bad + ' 张没删掉</b>' : " · 没有失败")
    + (bad && (r.errors || []).length ? '<span class="sub">' + esc(r.errors.join("；")) + "</span>" : "")
    + '<span class="sub">窗口：安静满 ' + (Number(r.days) || 0) + " 天</span>";
}

async function loadPurge(){
  /* 加载态写进 #purgeList，不能写 #purgeBox：那颗盒子里装着搜索框、总数、全选和删除键，
     往它 innerHTML 上写一句话等于把面板自己的控件删干净（曾经就是这么死的）。 */
  const list = $("#purgeList");
  list.innerHTML = '<div class="empty">在算哪些票到期了…</div>';
  $("#purgeSum").textContent = "";
  try{
    /* all=1：面板要的是"全部票 + 各自还有几天到期"。徽标不跟着变——它数的还是到期的张数，
       否则"有票到期该清了"就悄悄变成"这台机器一共有几张票"。 */
    const r = await fetch(BASE + "/purge/candidates?all=1");
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    PURGE.items = j.items || []; PURGE.days = j.days || 30;
    PURGE.run = j.last_run || null;
    PURGE.sel = {}; PURGE.armed = false;      // 换一批数据就把勾选清掉：留着会指向已经不存在的票
    $("#purgeDays").value = PURGE.days;
    renderPurge();
    purgeBadge(purgeAged().length);
  }catch(e){
    /* 读不到就当没有：留着上一批的话，人会把一次没成功的读过去当成"这些票还排着等删"。
       徽标同理清掉——算不出来的数字不该继续挂在那儿。上次自动清理那一句也一样：
       这一次没读到，就不能拿上一批的状态接着说"跑过、删了几张"。 */
    PURGE.items = []; PURGE.sel = {}; PURGE.armed = false; PURGE.run = null;
    list.innerHTML = `<div class="empty empty-err">读候选失败：${esc(e.message || e)}</div>`;
    $("#purgeRun").innerHTML = "上次自动清理：<b>读不到</b>";
    $("#purgeNote").hidden = true;
    purgeDrawGo();
    purgeBadge(0);
  }
}
async function purgeBadgeLoad(){
  try{
    const r = await fetch(BASE + "/purge/candidates?brief=1");
    if (r.ok) purgeBadge(((await r.json()).items || []).length);
  }catch(e){ /* 徽标算不出来就别显示数字，别拿一个假数糊弄人 */ }
}
function renderPurge(){
  const shown = purgeShown(), picked = purgePicked(), sum = L.purgeSum(picked);
  const agedN = purgeAged().length;
  drawPurgeRun();
  $("#purgeNote").hidden = !PURGE.items.length;      // 没得可删时这句是噪音，但只要有候选就常驻
  if (!PURGE.items.length){
    $("#purgeSum").textContent = "";
    $("#purgeList").innerHTML = '<div class="empty">这台机器上还没有任何票。'
      + "<b>进这份名册只看一件事：这张票最后一次被碰（上传、复核、暂存、回传）之后，又安静躺满了几天</b>"
      + "——躺够了由定时器删，没人点过作废也一样会进来。"
      + '<span class="sub">一张都没有不等于清理坏了：看上面那句「上次自动清理」到底跑过没有。</span></div>';
    purgeDrawGo();
    return;
  }
  $("#purgeSum").textContent = `全部 ${PURGE.items.length} 张 · 已到期 ${agedN} 张 · `
    + `选中 ${picked.length} / ${shown.length} 张 · 本机 ${L.purgeBytes(sum.bytes)}`;
  if (!shown.length){
    $("#purgeList").innerHTML = '<div class="empty">按这个过滤词与筛选档没有票。过滤只往下筛：'
      + "还没满安静期的号，在这里搜也不会把它变成自动清理要删的那一张（要现在删得勾它那一行）。</div>";
    purgeDrawGo();
    return;
  }
  /* 一张都没到期时那句老实话还是要说，但列表照旧画出来：这一版的目的就是让人看见
     "每张还要等多久"，而不是给一个空盒子。 */
  const none = agedN ? "" : '<div class="pknone">没有一张到安静期（当下窗口 '
    + esc(PURGE.days) + ' 天）：下面这些都要等倒计时走完，定时器才会动它们。'
    + "要现在就删，勾它那一行——确认时会多问一句，并在删除凭据里单独记一笔。</div>";
  $("#purgeList").innerHTML = none + '<table class="purge-t"><colgroup>'
    + '<col style="width:42px"><col><col style="width:124px"><col style="width:96px">'
    + '<col style="width:152px"><col style="width:72px"><col style="width:84px">'
    + '</colgroup><thead><tr><th></th><th>票</th><th>主单号</th><th>分单号</th>'
    + '<th>为什么在这儿 · 作废</th><th>回传</th><th>本机占用</th></tr></thead><tbody>'
    + shown.map(it => {
      const on = PURGE.sel[it.key] === true;
      /* 「回传」那一列是这页最要紧的一格：已回传的票，删掉的就是"这张票发过什么"的凭据
         （删除时它会转成 purged.jsonl 里的一行号与文件名，票面内容不留）。 */
      const ev = it.in_ledger
        ? '<span class="pke" title="这张票回传过公司。删掉之后本机只剩一行删除凭据（号与文件名），收发货人与货物内容一律不留">已回传</span>'
        : '<span class="pkn2">未回传</span>';
      /* 第一格回答"这张为什么能删"：判据就是安静了多少天，所以它必须在行上，
         而不是藏在 tooltip 里——作废那一格降成纯信息，没人点过也要如实写出来。 */
      const quiet = `<span class="pkq" title="最后一次被碰：${esc(it.quiet_at || "—")}">`
        + "安静 " + (it.idle_days == null ? "?" : esc(it.idle_days)) + " 天</span>";
      /* 倒计时：这一版把未到期的也列出来了，所以每一行都要答"还要等多久"。
         没到期的那一格换个颜色并在 title 里说清"勾了就是提前删"——别让人以为勾上和到期是一回事。 */
      const due = `<span class="pkd${it.aged === false ? " pkd-w" : ""}"`
        + ` title="${it.aged === false ? "还没到安静期：勾这一行删它属于提前删，会多问一句并在凭据里单独记一笔" : "定时器今晚就会动它"}">`
        + `${esc(L.purgeDue(it))}</span>`;
      const vd = it.voided
        ? `<span class="pkv" title="作废时刻：${esc(it.void_at || "—")}">作废 `
          + `${esc(it.void_by || "?")} ${esc(String(it.void_at || "").slice(5, 10))}</span>`
        : '<span class="pkv pkv0">没人点过作废</span>';
      /* 每一行都要说得出"这是哪张票"：只暂存过、没提交过的票在台账里没有条目，
         两格号可能都是空的，那时票名（归档目录名，运维拿它去盘上找东西）就是唯一的身份。
         勾选框的 aria-label 也一样：五行共用一句，读屏器分不出勾的是哪一张。 */
      const name = it.stem || it.mawb || "—";
      const who = (it.kind === "mawb" ? "主单 " : "分单 ") + (it.hawb || it.mawb || name);
      return `<tr><td><input type="checkbox" data-pk="${esc(it.key)}"${on ? " checked" : ""}`
        + ` aria-label="选择 ${esc(who)}"></td>`
        + `<td><span class="pk-${esc(it.kind)}">${it.kind === "mawb" ? "主单" : "分单"}</span>`
        + `<span class="pks" title="票名（服务器上的归档名）：${esc(name)}">${esc(name)}</span></td>`
        + `<td class="tno"><span class="pkn">${esc(it.mawb || "—")}</span></td>`
        + `<td class="tno"><span class="pkn">${esc(it.hawb || "—")}</span></td>`
        + `<td>${quiet}${due}${vd}</td>`
        + `<td>${ev}</td><td class="pkb">${esc(L.purgeBytes(it.bytes))}</td></tr>`;
    }).join("") + "</tbody></table>";
  purgeDrawGo();
}
function purgeEarly(){ return purgePicked().filter(it => it.aged === false).length; }
function purgeDrawGo(){
  const n = purgePicked().length, e = purgeEarly(), b = $("#purgeGo");
  b.disabled = !n;
  /* 未到期的张数写在按钮上：这一页现在能勾到没满安静期的票，
     不写清楚就是让人在"普通清理"和"绕过安全期"之间盲按。 */
  b.textContent = (PURGE.armed ? "确认删除这 " : "删除选中的 ") + n + " 张"
    + (e ? "（其中 " + e + " 张还没到安静期）" : "") + (PURGE.armed ? "（不可恢复）" : "");
}
function purgeArm(){                                  // 第一步：不发请求，只把按钮换成确认
  PURGE.armed = true;
  const e = purgeEarly();
  purgeDrawGo();
  toast(e ? "再点一次才真的删。这一批里有 " + e + " 张还没到安静满 " + PURGE.days
          + " 天——是你主动要提前删的，删了不可恢复，凭据里会单独记一笔。"
        : "再点一次那颗按钮才真的删——这一下没有反悔的键。");
}
async function purgeFire(){
  const picked = purgePicked();
  PURGE.armed = false;
  if (!picked.length) return purgeDrawGo();
  $("#purgeGo").disabled = true;
  try{
    const r = await fetch(BASE + "/purge", {method: "POST",
      headers: {"Content-Type": "application/json"},
      /* early 只是"这一张没到期我也要点删"的意图；能不能真删、算不算提前删，
         全在服务端（它按当下的安静期重算，不认这里说的任何数）。 */
      body: JSON.stringify({items: picked.map(x => ({kind: x.kind, key: x.key,
                                                     early: x.aged === false}))})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    purgeDone(j.results || []);
  }catch(e){
    toast("删除失败：" + (e.message || e), "bad");
    purgeDrawGo();
  }
}
function purgeDone(results){
  const bad = results.filter(r => !r.ok), n = results.length - bad.length;
  /* 逐张点名：一批里有一张没删掉，只报"成功 n 张"就是把那一张糊过去——
     而这正是提交那一圈踩过的坑（发了一半却报全成）。 */
  if (!bad.length){ toast(n ? `已彻底删除 ${n} 张` : "没有票被删除"); }
  else{
    toast(`删掉 ${n} 张，${bad.length} 张没删掉：` + bad.map(b =>
      String(b.key || "").split("|").pop() + "（" + (b.error || "未知原因") + "）").join("；"), "bad");
  }
  loadPurge();
}
async function purgeSaveDays(){
  const v = $("#purgeDays").value;
  try{
    const r = await fetch(BASE + "/purge/config", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({days: v})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    toast("安静期已设为 " + j.days + " 天");
    loadPurge();                                     // 安静期一变，名单就跟着变
  }catch(e){
    toast("安静期没改掉：" + (e.message || e), "bad");
    $("#purgeDays").value = PURGE.days;     // 被拒的那个数字不该留在框里：那是"现在满几天"的当前值
  }
}
$("#btnPurge").addEventListener("click", () => { openGate("purgeWin"); loadPurge(); });
$("#purgeClose").addEventListener("click", () => {
  $("#purgeWin").hidden = true; PURGE.armed = false;
});
$("#purgeDaysSave").addEventListener("click", purgeSaveDays);
$("#purgeQuery").addEventListener("input", e => {
  PURGE.q = e.target.value; PURGE.armed = false; renderPurge();
});
$("#purgeAll").addEventListener("click", () => {
  /* 全选只翻**已到期**的那些：这一档要是一并勾到未到期的，「全选 + 点两下」就等于
     一键把这台机器上所有票端掉——提前删只能逐张勾，这是面板里唯一必须留着的硬约束。 */
  const shown = purgeShown().filter(it => it.aged === true);
  const allOn = shown.length > 0 && shown.every(it => PURGE.sel[it.key] === true);
  shown.forEach(it => { PURGE.sel[it.key] = !allOn; });
  PURGE.armed = false; renderPurge();
});
$("#purgeFilter").addEventListener("click", e => {
  const b = e.target.closest("[data-pf]"); if (!b) return;
  PURGE.f = b.dataset.pf; PURGE.armed = false;   // 换档就是换一批人，第一步的确认作废
  [...$("#purgeFilter").querySelectorAll("[data-pf]")].forEach(x => {
    const on = x.dataset.pf === PURGE.f;
    x.classList.toggle("on", on); x.setAttribute("aria-pressed", String(on));
  });
  renderPurge();
});
/* 两步确认的分支放在监听器里而不是 purgeFire 里：purgeFire 自己只管真删，
   读起来才看得出"第二下"这个函数里没有任何回头路。 */
$("#purgeGo").addEventListener("click", () => { PURGE.armed ? purgeFire() : purgeArm(); });
$("#purgeList").addEventListener("change", e => {
  const c = e.target.closest("[data-pk]"); if (!c) return;
  PURGE.sel[c.dataset.pk] = c.checked;
  PURGE.armed = false;                               // 改过勾选，第一步重走：确认的张数已经变了
  renderPurge();
});
$("#viewDesk").addEventListener("click", () => setView("desk"));
$("#rail").addEventListener("click", e => {
  if (e.target.closest("#railExpand")){ RAIL.open = !RAIL.open; renderRail(); return; }
  const b = e.target.closest("[data-i]"); if (!b) return;
  const t = S.tickets[+b.dataset.i]; if (!t) return;
  mvOff();                          // 从窄轨换票就是回到分单核对，主单视图必须先关掉
  S.sel = t.stem || t.filename;
  RAIL.open = false;
  render();
});
$("#viewDay").addEventListener("click", () => setView("day"));
// 统计是一张盖在整页之上的窗口：打开时现算，改日期/分组也仍是现算（窗口里不留第二套算术）
$("#btnStats").addEventListener("click", () => { openGate("statsWin"); loadStats(); });
$("#statsClose").addEventListener("click", () => { $("#statsWin").hidden = true; });
$("#stGo").addEventListener("click", loadStats);
$("#stCsv").addEventListener("click", () => { window.location = BASE + "/stats/export" + statsQS(); });
["stFrom", "stTo", "stGroup"].forEach(id => $("#" + id).addEventListener("change", loadStats));
/* 分段筛选只重画 tbody。委托挂在 #stBox 上：innerHTML 换掉的是它的孩子，委托点自己不动。
   要是这里调 renderStats()，六张卡与条形图会跟着重建，入场动画每点一次筛选就重播一遍。 */
$("#stBox").addEventListener("click", e => {
  const b = e.target.closest("[data-sf]"); if (!b) return;
  ST.seg = b.dataset.sf;
  b.parentElement.querySelectorAll("button").forEach(x => {
    const on = x === b;
    x.classList.toggle("on", on);
    x.setAttribute("aria-pressed", on ? "true" : "false");
  });
  renderStatsRows();
});
$("#dayGo").addEventListener("click", () => loadDay(true));
$("#dayDate").addEventListener("change", () => { VIEW.date = ""; loadDay(true); });
$("#dayOnlyVoid").addEventListener("change", e => { dayOnlyVoid = e.target.checked; dayConfirm = ""; renderDay(false); });
/* 三组档位控件都只改 DV 里的一个字段再重画：筛选/排序是"同一批票换个看法"，
   所以传 fade=false —— 每换一次筛就闪一遍淡入，正是原型第 9 条坑点。 */
$("#dayFilter").addEventListener("click", e => {
  const b = e.target.closest("[data-f]"); if (!b) return;
  DV.filter = b.dataset.f;
  b.parentElement.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  renderDay(false);
});
/* 「我的 / 全部」换的是**读哪一份数据**，不是怎么排版：所以要重新拉一次 /day，
   不能只在这批行里筛（那批行本来就是服务端按人给出来的，筛不出别人的）。 */
$("#dayScope").addEventListener("click", e => {
  const b = e.target.closest("[data-ds]"); if (!b) return;
  DAY.scope = b.dataset.ds;
  loadDay();
});
/* 号检索与那颗「刷新」是同一个动作：重新问一次服务端，回车即查、清空即回默认那一档。
   不在浏览器里筛——那一屏行本来就是服务端筛过一轮的，筛不出没给的东西。 */
$("#dayQ").addEventListener("keydown", e => { if (e.key === "Enter") loadDay(); });
$("#dayQ").addEventListener("search", () => loadDay());
$("#daySort").addEventListener("click", e => {
  const b = e.target.closest("[data-s]"); if (!b) return;
  DV.sort = b.dataset.s;
  b.parentElement.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  renderDay(false);
});
$("#dayCollapse").addEventListener("click", () => {
  const collapsing = dvAnyOpen();
  dvGroups().forEach(pk => {
    if (collapsing) DV.collapsed.add(pk); else DV.collapsed.delete(pk);
    dvAnimate(pk, collapsing);           // 每组各自动画、各自的收尾定时器，不重建 DOM
  });
  dvSyncCollapse();
});
$("#dayDensity").addEventListener("click", e => {
  const b = e.target.closest("[data-d]"); if (!b) return;
  const d = b.dataset.d === "cozy" ? "cozy" : "compact";
  document.body.dataset.density = d;     // 行高由 CSS 令牌接管，这里不碰表格内容
  localStorage.setItem(LS.dayDensity, d);
  document.querySelectorAll("#dayDensity button")
    .forEach(x => x.classList.toggle("on", x === b));
});
async function postVoid(k, on){
  const i = k.indexOf("|");
  const body = {kind: k.slice(0, i), key: k.slice(i + 1), void: on};
  dayConfirm = "";
  try{
    const r = await fetch(BASE + "/void", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({detail:"返回不是 JSON（HTTP " + r.status + "）"}));
    if (!r.ok || !j.ok) throw new Error(j.detail || ("HTTP " + r.status));
    toast(on ? "已作废：这一行从台账与统计里收起来了（原件、暂存与提交记录都没动）"
             : "已恢复：这一行回到台账", "ok");
  }catch(err){
    toast((on ? "作废失败：" : "恢复失败：") + err.message, "bad");
  }
  loadDay(false);      // 以服务端为准重画：作废本落在服务器上，别人那一屏也要跟着变
}
$("#dayBox").addEventListener("click", e => {
  const c = e.target.closest(".dvchev");
  if (c){                                  // 箭头排在最前，先接住：占位箭头（没分单）不给点
    if (!c.classList.contains("dvnone")) dvToggle(c.dataset.dvk);
    return;
  }
  const o = e.target.closest("[data-open]");
  if (o){ setView("desk"); openHouse(o.dataset.open); return; }
  const dy = e.target.closest("[data-day]");
  if (dy){                                 // 空那天上那个「最近有票的是 X」：翻过去，不是让人自己去改日期框
    $("#dayDate").value = dy.dataset.day; VIEW.date = ""; loadDay(true); return;
  }
  const ds = e.target.closest("[data-ds]");
  if (ds){                                 // 空态里那颗「切到「全部」」：与上面那颗档是同一个动作
    DAY.scope = ds.dataset.ds; loadDay(true); return;
  }
  const m = e.target.closest("[data-mopen]");
  if (m){                                  // 主单行：回工作台按这个号检索，核对区自己会出来
    setView("desk");
    const box = $("#mstNo");
    box.value = m.dataset.mopen;
    box.scrollIntoView({behavior: "smooth", block: "center"});
    mstSearch();
    return;
  }
  const v = e.target.closest("[data-void]");
  if (v){                                  // 第一下只把这一行变成「确认作废？」，不发请求（防误点）
    if (dayConfirm !== v.dataset.void){ dayConfirm = v.dataset.void; renderDay(false); return; }
    postVoid(v.dataset.void, true); return;
  }
  const n = e.target.closest("[data-vno]");
  if (n){ dayConfirm = ""; renderDay(false); return; }
  const u = e.target.closest("[data-undo]");
  if (u){ postVoid(u.dataset.undo, false); }
});
/* ── 三道把手：分栏宽窄（票面栏↔字段栏）、字段名列宽、值格自己的宽 ──────────
   存本机不存账号：各人屏幕不一样，不该互相将就。拖的时候只改样式，松手才写盘
   （每帧写 localStorage 会把拖动本身拖慢）。
   分栏那道与值格那道写的是同一份份额偏好（LS.split / --vw）：值格宽出来的每一像素都是从
   票面栏扣的，各存一份就会互相盖——拖完一边，另一边回到旧值，看起来像"没生效"。 */
function deskGeom(){
  const wrap = $("#wrap"), cs = getComputedStyle(wrap);
  /* 百分数是相对"整行的内容宽"（不含 .wrap 的左右内边距）——CSS 里那条轨道就是这么算的，
     量两栏之和会算出偏大的百分数，拖 100px 实际走 130px。 */
  const avail = wrap.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
  return [$("#view").getBoundingClientRect().width, avail];
}
function setDeskShare(pct){ $("#wrap").style.setProperty("--vw", pct + "%"); }
function saveDeskShare(pct){ setDeskShare(pct); localStorage.setItem(LS.split, pct); }
function applyDeskPrefs(){
  const wrap = $("#wrap"), vw = Number(localStorage.getItem(LS.split)), kw = Number(localStorage.getItem(LS.colK));
  if (vw > 0) wrap.style.setProperty("--vw", vw + "%");
  if (kw > 0) wrap.style.setProperty("--colK", L.colWidthAfter(kw, 150, 420) + "px");
}
function bindSplitGrip(){
  const g = $("#gripView");
  if (!g) return;
  let st = null;
  g.addEventListener("pointerdown", e => {
    e.preventDefault(); const [w, avail] = deskGeom();
    st = {x: e.clientX, w, avail, v: 0}; g.classList.add("drag"); g.setPointerCapture(e.pointerId);
  });
  g.addEventListener("pointermove", e => {
    if (!st) return;
    st.v = L.splitPctAfter(st.w + (e.clientX - st.x), st.avail, 260); setDeskShare(st.v);
  });
  g.addEventListener("pointerup", () => {
    if (!st) return;
    g.classList.remove("drag");
    if (st.v) localStorage.setItem(LS.split, st.v);
    st = null;
  });
  g.addEventListener("keydown", e => {
    const d = e.key === "ArrowLeft" ? -40 : e.key === "ArrowRight" ? 40 : 0;
    if (!d) return;
    e.preventDefault();
    const [w, avail] = deskGeom();
    saveDeskShare(L.splitPctAfter(w + d, avail, 260));
  });
}
/* 表头里那道是每次渲染新建的，所以监听挂在 document 上按 id 找——直接绑会在换票后失效。 */
function bindColGrip(){
  let st = null;
  const put = w => $("#wrap").style.setProperty("--colK", w + "px");
  document.addEventListener("pointerdown", e => {
    const g = e.target.closest && e.target.closest(".gripCol");
    if (!g) return;
    e.preventDefault();
    st = {x: e.clientX, w: g.closest("th").getBoundingClientRect().width, v: 0};
    g.classList.add("drag"); g.setPointerCapture(e.pointerId);
  });
  document.addEventListener("pointermove", e => {
    if (!st) return;
    st.v = L.colWidthAfter(st.w + (e.clientX - st.x), 150, 420); put(st.v);
  });
  document.addEventListener("pointerup", () => {
    if (!st) return;
    document.querySelectorAll(".gripCol.drag").forEach(g => g.classList.remove("drag"));
    if (st.v) localStorage.setItem(LS.colK, st.v);
    st = null;
  });
  document.addEventListener("keydown", e => {
    const g = e.target.closest && e.target.closest(".gripCol");
    if (!g) return;
    const d = e.key === "ArrowLeft" ? -20 : e.key === "ArrowRight" ? 20 : 0;
    if (!d) return;
    e.preventDefault();
    const w = L.colWidthAfter(g.closest("th").getBoundingClientRect().width + d, 150, 420);
    put(w); localStorage.setItem(LS.colK, w);
  });
}
document.addEventListener("click", e => {
  if (e.target.closest && e.target.closest("[data-fhreset]")) resetFieldHeights();
});
/* 值格右边缘那道：拖的是「值这一列」的宽，所以整列一起变——一格一格变会参差得像页面坏了。
   格子是每次渲染新建的，监听挂 document 上按 class 找；直接绑会在换票后失效。 */
function bindEdgeGrip(){
  let st = null;
  document.addEventListener("pointerdown", e => {
    const g = e.target.closest && e.target.closest(".edg");
    if (!g) return;
    e.preventDefault();               // 别把这一拖顺手变成"取消选择正在编辑的那格"
    const [w, avail] = deskGeom();
    st = {x: e.clientX, w, avail, v: 0};
    g.classList.add("drag"); g.setPointerCapture(e.pointerId);
  });
  document.addEventListener("pointermove", e => {
    if (!st) return;
    st.v = L.boxWidthPct(st.w, e.clientX - st.x, st.avail, 260); setDeskShare(st.v);
  });
  document.addEventListener("pointerup", () => {
    if (!st) return;
    document.querySelectorAll(".edg.drag").forEach(g => g.classList.remove("drag"));
    if (st.v) localStorage.setItem(LS.split, st.v);
    st = null;
  });
  document.addEventListener("keydown", e => {
    const g = e.target.closest && e.target.closest(".edg");
    if (!g) return;
    const d = e.key === "ArrowRight" ? 40 : e.key === "ArrowLeft" ? -40 : 0;
    if (!d) return;
    e.preventDefault();
    const [w, avail] = deskGeom();
    saveDeskShare(L.boxWidthPct(w, d, avail, 260));
  });
}
applyDeskPrefs(); dvInitDensity(); bindSplitGrip(); bindColGrip(); bindEdgeGrip();
checkAuth();
