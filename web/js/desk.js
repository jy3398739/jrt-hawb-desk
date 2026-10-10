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

const LS = {draft:"hawb.review.drafts", masterDrafts:"hawb.review.masterDrafts", hintLocate:"hawb.review.hint.locate",
            locateZoom:"hawb.review.locate.zoom", fieldH:"hawb.review.fieldH",
            split:"hawb.review.split", colK:"hawb.review.colK"};
const $ = s => document.querySelector(s);
const nowTxt = () => new Date().toISOString().slice(0,19).replace("T"," ");
const S = {tickets:[], sel:null, running:false};
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
   原文侧的毛病由后端 validator / 保真回查出红旗，走 chips 那条路。 */
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

/* ── 质检红旗 → 落到哪个字段 ─────────────────────────────────────────── */
function flagsByField(qc){
  const map = {}, rest = [];
  for (const f of (qc && qc.flags) || []){
    const k = KEYS.find(kk => f.startsWith(kk + " ") || f.includes(kk + ":") ||
                              f.includes(kk + " 未") || f.includes(kk + " 缺失") || f.includes("'" + kk + "'"));
    if (k) (map[k] = map[k] || []).push(f); else rest.push(f);
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
/* 与后端 codes.clean_tax 同口径：去空白转大写，摘开头标签词（VAT#/TAX ID/USCI…） */
function cleanTaxJs(v){
  let out = String(v || "").replace(/\s+/g, "").toUpperCase();
  for (let i = 0; i < 2; i++){
    const s = out.replace(/^(?:USCI|CNPJ|CPF|RFC|GSTIN|GST|TAXID|TAXNO|TAX|VAT(?:NO|NR|NUMBER|ID)?|统一社会信用代码)[#.:：]*/, "");
    if (s === out) break;
    out = s;
  }
  return out;
}
function isTaxLabelFlag(f){
  return f.includes("混进了标签") && f.includes("已自动摘掉") &&
         (f.includes("SHIPPER_INFO_EORI") || f.includes("CONSIGNEE_INFO_EORI"));
}
function taxFlagCleared(t, f){
  const k = f.includes("SHIPPER_INFO_EORI") ? "SHIPPER_INFO_EORI" : "CONSIGNEE_INFO_EORI";
  const m = f.match(/: '(.*)'$/);
  if (!m) return false;
  const rev = reviewed(t);
  const cur = cleanTaxJs(rev[k]);
  return cur !== "" && cur === cleanTaxJs(m[1]);
}
function effectiveFlags(t){
  const filled = awbFilled(t), ack = t.ackF || [];
  return ((t.qc && t.qc.flags) || []).filter(f => {
    if (ack.includes(f)) return false;              // 复核员已确认无误的旗（留痕在 acked_flags）
    if (isMissingAwbFlag(f)){
      const okM = !f.includes("MAWB_NO") || filled.MAWB_NO;
      const okH = !f.includes("HAWB_NO") || filled.HAWB_NO;
      if (okM && okH) return false;         // 缺的号都补上了 → 这条红旗视为已清
    }
    if (isTaxLabelFlag(f) && taxFlagCleared(t, f)) return false;   // 摘好的号没被动过 → 提示已确认
    return true;
  });
}

/* ── 本机未保存的编辑（防刷新丢编辑；接口未就绪时也是提交兜底） ──────────────
   改名是因为「暂存」现在单指服务器那一档：同名会让制单员以为点了就交上去了，
   而没上服务器的票同事查不到。这块存的只是这台浏览器里的编辑。 */
function drafts(){ try { return JSON.parse(localStorage.getItem(LS.draft) || "{}"); } catch (e) { return {}; } }
function saveDraft(t){
  if (!t.stem) return;
  const d = drafts();
  d[t.stem] = {stem:t.stem, filename:t.filename, channel:t.channel, elapsed:t.elapsed, savedAt:nowTxt(),
               qc:t.qc, raw:t.raw, air:t.air, airE:t.airE, ackF:t.ackF || [], submitted:t.submitted};
  localStorage.setItem(LS.draft, JSON.stringify(d));
}
function loadDraft(stem){
  const d = drafts()[stem]; if (!d) return null;
  return {file:null, filename:d.filename || d.stem, stem:d.stem, state:"done", restored:true,
          channel:d.channel, elapsed:d.elapsed, qc:d.qc, raw:d.raw || {}, air:d.air || {},
          airE:d.airE || {}, ackF:d.ackF || [], submitted:d.submitted || null};
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
function pvBody(html){ $("#pvBody").innerHTML = html; }
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
  if (!lines.length) return '<div class="empty">公司侧这条主单没有资料文本：接口没返回，或从未录入。</div>';
  const rows = lines.map(x => '<div class="j blk"><b>' + (x.i ? x.i + ':' : '') + '</b> ' + esc(x.text) + '</div>').join("");
  return '<div class="mvtxt"><div class="t2">L1 原文 · 公司主单资料（逐字，未加工）</div>' + rows +
    '<p class="hint hintp flush">主单没有票面版式原件，所以这里是资料文本而非票面图：点右边字段名不能定位高亮。</p></div>';
}
function renderPreview(){
  if (MV){
    const key = "M|" + MV.mawb;
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
  const boxes = LOC.hits.filter(l => (l.page || 1) === LOC.page).map(l => {
    const [x1, y1, x2, y2] = l.bbox;
    return `<div class="bbox" title="${esc(l.text)}" style="left:${x1 / 10}%;top:${y1 / 10}%;` +
      `width:${Math.max(x2 - x1, 10) / 10}%;height:${Math.max(y2 - y1, 10) / 10}%"></div>`;
  }).join("");
  pvBody(`<div class="page" id="pvPageBox"><img id="pvImg" src="${src}" alt="票面第 ${LOC.page} 页"><div class="lyr">${boxes}</div></div>`);
  $("#pvLocPage").textContent = "第 " + LOC.page + "/" + LOC.pages + " 页";
  $("#pvLocPrev").disabled = LOC.page <= 1;
  $("#pvLocNext").disabled = LOC.page >= LOC.pages;
  const im = $("#pvImg");
  if (im.complete && im.naturalWidth) locFit(); else im.onload = locFit;
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
  const first = page.querySelector(".bbox");
  const bh100 = first ? im.naturalHeight * parseFloat(first.style.height) / 100 : 0;   // 100% 宽时该行的高（px）
  const p = zoomPref();
  const cap = L.maxZoomPct(im.naturalWidth, pane.clientWidth);
  const z = L.locateZoom(p.mode, p.pct, L.autoZoomPct(pane.clientHeight, bh100), cap);
  $("#pvLocPct").max = cap;      // 上限先摆在框子上，别等人填了 9999 才告诉他会被收
  page.style.width = (z || 100) + "%";
  if (first){
    const pr = page.getBoundingClientRect(), fr = first.getBoundingClientRect(), br = pane.getBoundingClientRect();
    /* 巡航用 scrollTo：一次调用同时给两个方向，浏览器自己合成一段滚动。
       不给 #pvBody 写 scroll-behavior:smooth——installPan 每次 pointermove 都在改 scrollLeft/Top，
       那会让"按住拖动"变成果冻。设了系统"减少动态效果"就直给，不滚。 */
    pane.scrollTo({left: pane.scrollLeft + (fr.left + fr.width / 2) - (pr.left + br.width / 2),
                   top: pane.scrollTop + (fr.top + fr.height / 2) - (pr.top + br.height / 2),
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
  return await waitJob(j.job, t);
}
/* 解析改成就绪取：入队后轮询 /job。位次变了就刷一行状态，人才知道不是卡住了。
   作业表只在内存里——服务重启后旧作业 404，这里自动重传一次，不再让制单员手动重来。 */
async function waitJob(job, t){
  return await new Promise((res, rej) => {
    poller(async () => {
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
                        qc:j.qc, raw:j.raw || {}, air:j.air || {}, airE:{}, ackF:[], ll:j.transcript || null});
      if (S.sel === t.filename) S.sel = t.stem;
      saveDraft(t);
      hintLocate();          // 第一次解析成功时提一次「点字段名能定位」，之后不再啰嗦
    }catch(e){
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
   报失败会让人以为白点了，又去点一次，而白点的其实只是公司那半。 */
function stageMsg(n, c){
  const base = "已暂存（改了 " + (n || 0) + " 列）：同事检索这条单号就能看到";
  if (!c) return base;
  if (c.ok && c.mode === "live") return "已暂存并寄到公司（状态 0，还能继续改）：改了 " + (n || 0) + " 列";
  if (c.ok) return base + "；公司那边没发（本机 mock 模式）";
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
    t.staged = {at: nowTxt(), by: who, edited: n};
    render();
    toast(stageMsg(n, j.staged && j.staged[0] && j.staged[0].company), "ok");
  }catch(e){ toast("暂存失败：" + (e.message || e), "bad"); }
}
async function submitTicket(t, ackNoReview){
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
  if (ackNoReview) p.acked_no_review = true;   // 只在人确认过之后才带，不许默认替人确认
  const body = {tickets:[p], submitted_at:nowTxt(), client:"hawb-review-desk/1.0"};
  t.state = "submitting"; render();
  let pending = false;
  try{
    const r = await fetch(SUBMIT_URL, {method:"POST", headers:{"Content-Type":"application/json"},
                                       body:JSON.stringify(body)});
    if (r.status === 404 || r.status === 405) pending = true;
    else {
      const j = await r.json().catch(() => ({ok:false, error:"返回不是 JSON（HTTP " + r.status + "）"}));
      if (!r.ok || j.ok === false){
        // 服务端 400 的 detail 可能是字符串（缺号），也可能是 {message, flags}（红旗未清）
        // 或 {message, ack}（没人复核过）：不把 message 挖出来，制单员只会看到光秃秃一句
        // "HTTP 400"，不知道该改哪。
        const d = j.detail;
        if (d && d.ack === "no_inputter_review"){
          // 第三道门：服务器说这张票没有录入员复核过的暂存记录。问一次，点了确认才重发；
          // 不点就当没发生过——绝不能自己替他确认。
          t.state = "done"; render();
          if (confirm((d.message || "这张票没有录入员复核过的暂存记录。") +
                      "\n\n确认「未经录入员复核，仍要回传公司」？")) submitTicket(t, true);
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
  saveDraft(t); render(); toast("已提交：" + t.stem, "ok");
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
  if (t.submitted && t.submitted.mode === "server") return ["ok", "已提交"];
  if (t.submitted) return ["warn", "待提交"];
  if (effectiveFlags(t).length) return ["warn", "需复核"];
  return ["ok", "干净"];
}
function current(){ return S.tickets.find(t => t.stem === S.sel) || S.tickets.find(t => t.filename === S.sel); }
function render(){
  const done = S.tickets.filter(t => t.state === "done").length;
  const rev = S.tickets.filter(t => effectiveFlags(t).length).length;
  $("#cnt").textContent = S.tickets.length + " 票 · 已解析 " + done + (rev ? " · 需复核 " + rev : "");
  $("#list").innerHTML = S.tickets.length ? S.tickets.map((t, i) => {
    const [c, label] = statusOf(t), n = editedList(t).length;
    const isCur = (t.stem && t.stem === S.sel) || (!t.stem && t.filename === S.sel);
    return `<button type="button" class="row${isCur ? " sel" : ""}" data-i="${i}"${isCur ? ' aria-current="true"' : ""}>
      <span class="dot ${c}" aria-hidden="true"></span>
      <span class="nm">${esc(t.filename)}<br><span class="m">${esc(t.stem || "等待解析")}</span></span>
      <span class="st">${label}${n ? `<br><span class="st-edited">改 ${n} 项</span>` : ""}</span></button>`;
  }).join("") : `<div class="empty">尚无票据。</div>`;
  renderRail();
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
function renderDrafts(){
  const d = drafts();
  const keys = Object.keys(d).filter(k => !S.tickets.some(t => t.stem === k));
  $("#drafts").innerHTML = keys.length ? keys.map(k => {
    const t = d[k], n = Object.keys(t.airE || {}).length;
    return `<button type="button" class="row" data-load="${esc(k)}">
      <span class="dot warn" aria-hidden="true"></span>
      <span class="nm">${esc(k)}<br><span class="m">${esc(t.savedAt || "")} · 改 ${n} 项 · 未提交</span></span>
      <span class="st">载入</span></button>`;
  }).join("") : `<div class="empty">编辑会自动存在本机浏览器，防刷新丢失；提交成功后仍可在此回看。</div>`;
}
function control(t, k){
  const v = airVal(t, k);
  /* 一律 textarea：原生斜角只长在 textarea 上，短字段从前用 input 标签就没有可拖的角
     （2026-10-09 用户："可以自由拖拽改变大小的"）。单行那些标 .one：挡回车、按内容长高。 */
  const one = LONG.has(k) ? "" : ' class="one"';
  return `<textarea data-k="${k}" rows="1"${one}` + (NUM[k] ? ` inputmode="decimal"` : "") + `>${esc(v)}</textarea>`;
}
/* 单行那些换成了 textarea，就得把回车挡住：这个值原样写进公司系统，换行混进 MAWB 号或件重
   就是脏数据，而且在单行框里换行看不见（改完的人以为只有一行）。 */
function blockEnter(e){ if (e.key === "Enter" && e.target.classList.contains("one")) e.preventDefault(); }
/* 长文本框高度跟着内容走：以前按 "\n" 数行数，收发货人整串是一整段没有换行，
   再长也只有 1 行高，看着别扭；改成量实际渲染高度（封顶后出滚动条）。 */
function autoGrow(el){
  /* 宽不是单格的属性（斜角拖那一下改的是整列），所以每次量高度前先把 inline 宽交还给列：
     留着它，这一格就从此钉死在自己那个宽度上，别人再调列宽也调不到它。 */
  el.style.width = "";
  /* 人拖过的那一列以拖的为准：从前这里每次渲染与每一下输入都按内容重设高度，
     拖完一打字就弹回去——"能拖"在用户那儿等于"不能自定义"（2026-10-09）。 */
  const k = el.dataset.k || el.dataset.mk || "";
  const rec = k ? fieldHeights()[k] : 0;
  if (rec){ el.style.height = L.fieldHeight(rec, 0) + "px"; return; }
  el.style.height = "auto";
  el.style.height = L.fieldHeight(0, el.scrollHeight + 2) + "px";
}
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
  return '<div class="grip" id="gripCol" role="separator" aria-orientation="vertical" tabindex="0" '
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
  document.querySelectorAll("textarea[data-k], textarea[data-mk]").forEach(autoGrow);
  render();
  toast(n ? `已恢复 ${n} 列的框高为自动` : "没有自定义过的框高", "ok");
}
function statusCell(t, k, fl){
  const l2 = txt(t.raw, k), l3 = airVal(t, k);
  const ck = checks(k, l2, l3);
  let head = "";
  if (fl.length) head = `<span class="badge warn">红旗 ${fl.length}</span>`;
  else if (!l2.trim() && !l3.trim()) head = `<span class="badge">字段为空</span>`;
  else if (!l3.trim()) head = `<span class="badge miss">航空口径空</span>`;
  else head = `<span class="badge same">已归一</span>`;
  const edits = (k in t.airE) ? `<div class="note"><span class="e">已改</span>
      <button type="button" class="rs" data-restore="${esc(k)}">还原</button></div>` : "";
  const notes = ck.slice(0, 3).map(x => `<div class="note"><span class="${x[1] === "bad" ? "b" : "q"}">${esc(x[0])}</span></div>`).join("");
  return head + edits + notes;
}
function renderMain(){
  const el = $("#main"), t = current();
  el.classList.remove("has-todo");     // 只有分单核对区有置顶待办条
  /* 主单视图：36 列可编辑 + 红旗逐条确认 → 提交发送回公司（mawb3）。列名/中文名/分组由后端
     fields 表给（单一真源），红旗在提交时由服务端按当前值重算——前端只负责让人看清并确认。 */
  if (MV){
    const rec = MV.rec || {}, qc = rec.qc || {}, flags = mvFlags();
    const vals = mvValues(), left = flags.filter(f => !MV.acked.includes(f));
    const missed = (qc.missed_cols) || [];
    const chips = flags.map(f => {
      const done = MV.acked.includes(f);
      const hard = f.includes("缺失") || f.startsWith("保真") || f.includes("失败");
      return `<span class="chip ${done ? "" : (hard ? "bad" : "warn")}">${esc(f)}` +
        (done ? `<button class="ackb" data-munack="${esc(f)}">撤回</button>`
              : `<button class="ackb" data-mack="${esc(f)}">确认无误</button>`) + `</span>`;
    }).join("");
    const MVG = rec.groups || {};
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
      const hit = flags.filter(f => f.includes(k)).length;
      const miss = missed.includes(k);
      rows += `<tr id="mf-${k}" class="${hit || miss ? "flag" : ""}">
        <td class="k"><div class="kk">${esc(lab)}</div><div class="ky">${esc(k)}</div></td>
        <td class="c v${k in MV.edit ? " edited" : ""}"><textarea data-mk="${k}" rows="1"${MV_LONG.has(k) ? "" : ' class="one"'}>${esc(v)}</textarea>${edgeGripHtml()}</td>
        <td class="s">${miss ? `<span class="badge miss">漏取</span>`
          : hit ? `<span class="badge warn">红旗 ${hit}</span>`
          : (v ? `<span class="badge same">有值</span>` : `<span class="badge">资料没提</span>`)}</td></tr>`;
    }
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
      ${chips ? `<div class="chips">${chips}</div>` : ""}
      ${MV.conflicts.length ? `<div class="chips"><span class="chip bad">本机未保存的编辑与最新解析对不上：${esc(MV.conflicts.map(mvLabel).join("、"))} —— 这几列改的还是重解析前的值，请对着左栏原文重改</span></div>` : ""}
      ${MV.staged ? `<div class="chips"><span class="chip">服务器上有 ${esc(L.roleCn(MV.staged.role))} ${esc(MV.staged.by)} 暂存的核对结果（${esc(MV.staged.at)}），已按那份显示</span></div>` : ""}
      ${(MV.localDiff || []).length ? `<div class="chips"><span class="chip bad">${esc(MV.localDiff.map(mvLabel).join("、"))} 你这台机器上的改动和服务器暂存的不一致，已按服务器版本显示；你那份没删，还在本机未保存的编辑里</span></div>` : ""}
      ${rec.state === "failed" ? `<div class="empty empty-err">解析失败：${esc(rec.error || "未知错误")}<br>
        <span class="sub">等一会儿点上面「重新解析」重试；连续失败就换个模型再看。</span></div>` : ""}
      <div class="ft top">
        <button class="btn" id="mvStage" type="button" title="把这 36 列存到服务器给同事核对，同时在 j9 那边占个位（状态 0，还能反复改）；只有「提交主单回公司」才会真的发送">暂存到公司</button>
        <button class="btn pri" id="mvSubmit">提交主单回公司</button>
        <span class="sp"></span>
        <span class="fhslot">${fhResetHtml()}</span>
        <span class="hint" id="mvMsg">${esc(MV.msg || (left.length ? "红旗 " + left.length + " 条待确认" : ""))}</span>
      </div>
      <div class="tools"><span class="hint">改完直接点提交：红旗由服务端按当前值重算，未清的会退回来让逐条确认。
        主单表没有件重/航路/税号列，<b>税号按公司口径统一填同主体的 EORI 列</b>；
        同一主体有多个号（EORI 与 VAT 并存那种）就用 <b>/</b> 拼在同一格，两个都留。</span></div>
      <table><thead><tr>
        <th class="kcol">字段${gripColHtml()}</th><th class="g">公司 AMS 列 · 可改</th><th class="num scol">状态</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
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
    $("#mvStage").addEventListener("click", mvStage);
    $("#mvSubmit").addEventListener("click", () => mvSubmit());
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
  const ef = effectiveFlags(t);
  const fm = flagsByField({flags: ef}), [c, label] = statusOf(t);
  const chips = ef.map(f => {
    const k = KEYS.find(kk => f.startsWith(kk + " ") || f.includes(kk + ":") || f.includes(kk + " 未") ||
                              f.includes(kk + " 缺失") || f.includes("'" + kk + "'"));
    const hard = f.startsWith("保真") || f.includes("缺失");
    /* chip 里两个动作各一个按钮：跳转（对字段）与确认（对这条提示）。
       从前是 span 套 button，点哪儿都算跳转，确认要 stopPropagation 才不被带着走。 */
    const chip = `<span class="chip ${hard ? "bad" : "warn"}">` +
      (k ? `<button type="button" class="chipb" data-jump="${k}" title="点击：跳到并高亮这个字段">${esc(f)}</button>`
         : `<span class="chipx">${esc(f)}</span>`) +
      `<button class="ackb" data-ack="${esc(f)}" title="票面已核对无误 → 确认这条提示，提交放行（留痕进台账）">确认无误</button></span>`;
    return chip;
  }).join("");
  let rows = "", last = "";
  for (const [k, lab, g] of FIELDS){
    if (g !== last){ rows += `<tr class="grp"><td colspan="3">${GROUPS[g]}</td></tr>`; last = g; }
    const fl = fm.map[k] || [];
    rows += `<tr id="f-${k}" class="${fl.length ? "flag" : ""}">
      <td class="k loc"><button type="button" class="kbtn" data-jump-field="${k}" title="点击：在左边票面上定位并放大这个值"><span class="kk">${esc(lab)}</span><span class="ky">${esc(k)}</span></button></td>
      <td class="c v" data-cell="air">${control(t, k)}${edgeGripHtml()}</td>
      <td class="s" data-st="${k}">${statusCell(t, k, fl)}</td></tr>`;
  }
  const fid = t.qc && t.qc.fidelity;
  el.classList.add("has-todo");
  el.innerHTML = `
    <div class="hd">
      <span class="dot ${c}" aria-hidden="true"></span>
      <div><div class="nm">${esc(t.filename)}</div>
        <div class="meta">${esc(t.stem || "")} · ${esc(t.channel || "?")} 通道 · ${t.elapsed || 0}s
          ${t.staged ? " · " + esc(L.roleCn(t.staged.role)) + " " + esc(t.staged.by) + " 已暂存（改 " + t.staged.edited + " 列）" : ""}${t.restored === "server" ? " · 服务器载入" : (t.restored ? " · 本机未保存的编辑载入" : "")}${label ? " · " + label : ""}
          · 保真回查 ${fid ? `${fid.passed}/${fid.checked} 字段有票面转录出处` : "本次无记录"}</div></div>
    </div>
    <div class="todo">
      <span class="lbl">待办</span>
      <span class="hint" id="editedHint">${esc(todoText(t, ef))}</span>
      <button class="btn sm" id="jumpFirst" type="button">跳到首个红旗字段</button>
      <label><input type="checkbox" id="onlyFlag"> 只看有红旗 / 空值的字段</label>
      <span class="fhslot">${fhResetHtml()}</span>
      <span class="sp"></span>
      <span class="hint">点字段名 → 定位票面</span>
    </div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}
    <table><thead><tr>
      <th class="kcol">字段${gripColHtml()}</th><th class="g">航空口径 L3 · 已归一（可改）</th>
      <th class="num scol">状态</th>
    </tr></thead><tbody>${rows}</tbody></table>
    <div class="ft bottom">
      <button class="btn" id="stage" type="button" title="把现在的核对结果存到服务器给同事看，同时在 j9 那边占个位（状态 0，还能反复改）；缺号只存本机。只有「提交本票」才会真的发送">暂存到公司</button>
      <button class="btn pri" id="submit">提交本票</button>
      <button class="btn" id="expOne">导出本票 JSON</button>
      <button class="btn" id="expAll">导出全部已解析</button>
      <span class="sp"></span>
      <button class="btn sm" id="prevOne" type="button" title="上一张票">‹ 上一票</button>
      <button class="btn sm" id="nextOne" type="button" title="下一张票">下一票 ›</button>
      <button class="btn gh" id="dropOne">从列表移除</button>
    </div>`;
  wireMain(t);
  measureChrome();
  $("#main").querySelectorAll("textarea").forEach(autoGrow);
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
function todoText(t, ef){
  const empt = FIELDS.filter(x => !String(airVal(t, x[0]) || "").trim()).length;
  return `红旗 ${ef.length} · 空值 ${empt} · 已改 ${editedList(t).length}`;
}
function refreshRow(t, k){
  const tr = $("#f-" + CSS.escape(k)); if (!tr) return;
  const ef = effectiveFlags(t);
  const fl = flagsByField({flags: ef}).map[k] || [];
  tr.querySelector("[data-st]").innerHTML = statusCell(t, k, fl);
  tr.querySelector('[data-cell="air"]').classList.toggle("edited", k in t.airE);
  const hint = $("#editedHint");
  if (hint) hint.textContent = todoText(t, ef);
  const row = document.querySelector(`.row[data-i="${S.tickets.indexOf(t)}"] .st`);
  if (row) row.innerHTML = statusOf(t)[1] + (editedList(t).length ? `<br><span class="st-edited">改 ${editedList(t).length} 项</span>` : "");
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
  }
  $("#main").querySelectorAll("[data-k]").forEach(inp => {
    const apply = () => {
      setEdit(t, inp.dataset.k, inp.value);
      refreshRow(t, inp.dataset.k);
      if (inp.tagName === "TEXTAREA") autoGrow(inp);
    };
    inp.addEventListener("input", apply);
    inp.addEventListener("blur", apply);
    inp.addEventListener("keydown", blockEnter);
  });
  $("#main").querySelectorAll("[data-restore]").forEach(b => b.addEventListener("click", () => {
    const k = b.dataset.restore; delete t.airE[k]; saveDraft(t); render();
  }));
  $("#main").querySelectorAll("[data-jump]").forEach(ch => ch.addEventListener("click", () => {
    const k = ch.dataset.jump; if (!k) return;
    const row = $("#f-" + CSS.escape(k)); if (!row) return;
    row.scrollIntoView({behavior:"smooth", block:"center"});
    row.classList.remove("hit");
    void row.offsetWidth;             // 先重排一次：连着点同一个字段第二次也要真的闪
    row.classList.add("hit");
    const inp = row.querySelector("[data-k]"); if (inp) inp.focus();
  }));
  $("#main").querySelectorAll("[data-ack]").forEach(b => b.addEventListener("click", () => {
    const f = b.dataset.ack;
    t.ackF = t.ackF || [];
    if (!t.ackF.includes(f)) t.ackF.push(f);
    saveDraft(t); render();
    toast("已确认：" + f.slice(0, 40) + (f.length > 40 ? "…" : "") + "（提交时留痕进台账）", "ok");
  }));
  $("#jumpFirst").addEventListener("click", () => {
    const fm = flagsByField(t.qc);
    const k = KEYS.find(x => (fm.map[x] || []).length) || (fm.rest[0] ? "" : "");
    if (!k){ toast("本票红旗未指到具体字段，逐条对照票面看", "ok"); return; }
    $("#f-" + CSS.escape(k)).scrollIntoView({behavior:"smooth", block:"center"});
  });
  $("#onlyFlag").addEventListener("change", e => {
    const fm = flagsByField(t.qc);
    for (const [k] of FIELDS){
      const row = $("#f-" + CSS.escape(k)); if (!row) continue;
      const interesting = (fm.map[k] || []).length || !txt(t.raw, k).trim() || !airVal(t, k).trim() ||
                          k in t.airE;
      row.hidden = e.target.checked && !interesting;
    }
  });
  $("#stage").addEventListener("click", () => stageTicket(t));
  $("#submit").addEventListener("click", () => submitTicket(t));
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
  if (used < want) toast(`已按这张票面图的真实分辨率限制在 ${used}%（填的 ${want}% 只会糊）`, "warn");
});
$("#pvNew").addEventListener("click", () => {
  const u = (LOC.on && LOC.url) || PREVIEW.url;
  if (!u){ toast("还没有可打开的票面", "bad"); return; }
  window.open(u, "_blank");       // blob URL 直接开新标签：图片能看，PDF 走阅读器
});
$("#list").addEventListener("click", e => {
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
/* 「清空」在卡片标题栏里，不在 #drafts 列表内——挂在列表上的委托收不到它的点击（曾因此点了没反应） */
$("#clearDrafts").addEventListener("click", () => {
  const n = Object.keys(drafts()).length;
  if (!n){ toast("本机没有未保存的编辑", "bad"); return; }
  if (!confirm(`清空本机浏览器里未保存的 ${n} 条编辑？（它们从没上过服务器；已落盘的 JSON 结果不受影响）`)) return;
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
  $("#pwWarn").hidden = !(admin && PW_DEFAULT);   // 默认口令没改：把话放在管理员天天看得见的地方
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
  probe(); loadModels(); render();
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
$("#btnAcct").addEventListener("click", () => { $("#acct").hidden = false; loadAcct(); });
$("#pwWarn").addEventListener("click", () => $("#btnAcct").click());   // 话说到哪儿，入口就在哪儿
$("#btnMawb").addEventListener("click", () => {   // 检索已并入本页：按钮只做"滚过去 + 聚焦输入框"
  setView("desk");                                // 在今日台账里点它，要先回到工作台才看得见那张卡
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
/* 名下分单表里"公司还不认识"的那几行，来源是检索响应里的 pending（服务器从盘上现算）。
   从前这里是自己数浏览器状态（pendingUnder 读 S.tickets + localStorage），那份名单只对
   当前这个人、这台机器成立——制单员上午传的票，录入员下午查同一张主单仍是"没有分单"。 */
function mstTable(list, pend){
  pend = pend || [];
  if (!list.length && !pend.length)
    return '<p class="hint hintp">这条主单下公司还没有分单记录。' +
           '<b>解析不等于提交</b>：有人点「暂存」，核对结果就存在服务器上给同事看；' +
           '点「提交」才会回传公司，这里才会出现公司的行。</p>';
  const rows = list.map(o => {
    const ss = o.send_status === undefined || o.send_status === null ? "—"
             : (SEND_TXT[o.send_status] || o.send_status);
    const no = o.stem ? '<button type="button" class="lk" data-open="' + esc(o.stem) + '" title="点开这张分单的核对页">' + esc(o.hawb) + '</button>'
                      : esc(o.hawb);
    return "<tr><td>" + no + "</td><td>" + esc(o.mawb) +
      "</td><td>" + esc(o.reviewer || "—") + "</td><td>" +
      (o.submitted_here ? esc(o.submitted_at || "—")
                        : '<span class="hint">不在本台提交</span>') +
      "</td><td>" + esc(ss) + "</td><td>" +
      (o.stem ? '<a href="' + BASE + '/source/' + encodeURIComponent(o.stem) + '" target="_blank">查看原件</a>'
              : '<span class="hint">本机无归档</span>') + "</td></tr>";
  }).join("") + pend.map(p => {
    const no = p.stem
      ? '<button type="button" class="lk" data-open="' + esc(p.stem) + '" title="点开这张分单的核对页">' + esc(p.hawb) + '</button>'
      : esc(p.hawb);
    const who = p.stager
      ? esc(p.stager) + (p.edited ? ' <span class="st-edited">改 ' + p.edited + ' 列</span>' : '')
      : '<span class="hint">只解析，没人暂存</span>';
    return '<tr><td>' + no + "</td><td>" + esc(p.mawb) + "</td><td>" + who + "</td><td>" +
      (p.staged_at ? esc(p.staged_at) : '<span class="hint">—</span>') + '</td>' +
      '<td><span class="hint">公司侧还没有</span></td><td>' +
      (p.has_original ? '<a href="' + BASE + '/source/' + encodeURIComponent(p.stem) + '" target="_blank">查看原件</a>'
                      : '<span class="hint">本机无归档</span>') + "</td></tr>";
  }).join("");
  return '<table><colgroup><col style="width:19%"><col style="width:15%"><col style="width:12%">' +
    '<col style="width:18%"><col style="width:16%"><col></colgroup><thead><tr>' +
    '<th>分单号</th><th>主单号</th><th>提交人</th><th>提交时间</th><th>公司发送状态</th><th>票面原件</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>' +
    '<p class="hint hintp">「提交人/提交时间」是本审核台提交回公司的留痕；' +
    '「公司发送状态」是公司有没有把这条分单发给航司。' +
    '待公司发送 = 公司已收到、还没往航司发，不是说本台没提交。' +
    (pend.length ? '另有 ' + pend.length + ' 张公司还不认识：它们是这台机器上解析/暂存过的，' +
     '「改 N 列」是暂存时相对模型输出改了多少。' : '') + '</p>';
}
/* 一份结果两处落位：左栏只留入口那一行，名下分单表进页面底部整宽卡（264px 里放不下五列） */
function mstRender(j, mawb){
  $("#mstResult").innerHTML = mstCard(j, mawb);
  const list = j.hawb_orders || [], pend = j.pending || [];
  MST_CARD_ON = !!(list.length || pend.length);
  syncCards();
  $("#mstHawbCnt").textContent = (list.length || pend.length)
    ? ("主单 " + mawb + " · 公司 " + list.length + " 张" + (pend.length ? " · 本台待提交 " + pend.length + " 张" : ""))
    : "";
  $("#mstHawbBox").innerHTML = mstTable(list, pend);
}
/* 点分单号 = 从主单侧回到那张分单自己的核对页：服务器上落盘的 L2/L3/质检直接装回票据列表，
   不需要制单员重新上传原件。改完还是走 /submit，红旗由服务端按当前值重算。 */
async function openHouse(stem){
  const hit = S.tickets.find(t => t.stem === stem);
  if (hit){ mvOff(); S.sel = stem; render(); $("#view").scrollIntoView({behavior:"smooth", block:"start"}); return; }
  try{
    const r = await fetch(BASE + "/ticket/" + encodeURIComponent(stem));
    if (!r.ok){
      const j = await r.json().catch(() => ({}));
      throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    }
    const d = await r.json(), dr = drafts()[stem] || {};
    /* `/ticket` 只有模型口径；别人核对过的值在 `/staged` 那份里。没暂存过就是没有（404），
       不影响打开——所以这里不能 throw，安静地按"没有"处理。 */
    let st = null;
    try{
      const sr = await fetch(BASE + "/staged/" + encodeURIComponent(stem));
      if (sr.ok) st = await sr.json();
    }catch(e){ st = null; }
    const mg = L.stagedMerge(st && st.air_final, dr.airE || {}, st && st.edits);
    const t = {file:null, filename:d.filename || stem, stem:d.stem, state:"done", restored:"server",
               channel:d.channel, elapsed:d.elapsed,
               qc: Object.keys(d.qc || {}).length ? d.qc : null,
               raw:d.raw || {}, air:d.air || {}, airE:mg.airE, ackF:dr.ackF || [],
               staged: st && !st.submitted_at ? {by:st.stager, role:st.stager_role,
                                                 at:st.staged_at, edited:Object.keys(st.edits || {}).length} : null,
               submitted: d.submitted ? {mode:"server", at:d.submitted.submitted_at,
                                         reviewer:d.submitted.reviewer} : null};
    S.tickets.push(t); mvOff(); S.sel = stem; render();
    $("#view").scrollIntoView({behavior:"smooth", block:"start"});
    const from = t.submitted ? "已提交过，改动需再点提交"
      : (st ? "载入 " + L.roleCn(st.stager_role) + " " + st.stager + " 暂存的核对结果（改 "
                 + Object.keys(st.edits || {}).length + " 列）" : "本机模型结果，改完点提交");
    toast("已载入分单 " + (t.air.HAWB_NO || stem) + "（" + from + "）", "ok");
    if (mg.conflicts.length){
      toast("注意：" + mg.conflicts.join("、") + " 你这台机器上的改动和服务器上的暂存不一致，" +
            "已按服务器版本显示；你那份没删，还在下面的「本机未保存的编辑」里", "warn");
    }
  }catch(e){ toast("打开这张分单失败：" + (e.message || e), "bad"); }
}
async function mstSearch(force){
  const m = $("#mstNo").value.trim(), st = $("#mstStatus");
  if (!m){ st.textContent = "请先填主单号"; return; }
  st.textContent = "检索中…";
  try{
    const r = await fetch(BASE + "/company/mawb?mawb=" + encodeURIComponent(m) + (force ? "&force=1" : ""));
    if (r.status === 401){ $("#gate").hidden = false; throw new Error("登录已过期，请重新登录"); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    st.textContent = "（" + j.mode + "）";
    MST_LAST = j;
    mstRender(j, m);
    if (j.master && j.master.state === "parsing") mstPoll(m);   // 解析在后台跑，卡片跟着刷新状态
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
  const base = (MV.rec && MV.rec.qc && MV.rec.qc.flags) || [];
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
  MV.edit = mg.airE; MV.acked = m.acked; MV.extraFlags = m.extraFlags;
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
    toast(stageMsg(j.edited, j.company), "ok");
  }catch(e){ toast("主单暂存失败：" + (e.message || e), "bad"); }
}
async function mvSubmit(ackNoReview){
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
    if (ackNoReview === true) payload.acked_no_review = true;   // 只在人确认过之后才带
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
      } else if (r.status === 400 && d.ack === "no_inputter_review"){
        // 与分单同一条门：没人复核过要多点一次确认，点才算；不点什么都不发
        if (confirm((d.message || "这条主单没有录入员复核过的暂存记录。") +
                    "\n\n确认「未经录入员复核，仍要回传公司」？")) mvSubmit(true);
      } else throw new Error((typeof d === "string" ? d : d.message) || ("HTTP " + r.status));
    } else {
      MV.submitted = {at: nowTxt(), mode: j.mode};
      MV.edit = {}; MV.acked = []; MV.extraFlags = []; MV.conflicts = [];
      mstDraftClear(MV.mawb);                  // 已回传公司：草稿再留着，下次打开同一主单会把交过的改动重新盖上来
      mvSetRec(Object.assign({}, MV.rec, {ams: mvValues()}));
      MV.msg = ""; toast("主单 " + MV.mawb + " 已回传公司（" + (j.action || "ok") + "）", "ok");
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
$("#mstResult").addEventListener("click", async e => {
  const use = e.target.closest("[data-mv]");
  if (use){
    if (!MST_LAST) return;
    if (LOC.on) exitLocate();          // 定位模式的页图会被资料文本顶掉，先退干净免得留着半张高亮层
    MV = {mawb: use.dataset.mv, order: MST_LAST.mawb_order || {}, rec: {},
          edit: {}, editBase: {}, acked: [], extraFlags: [], conflicts: [], msg: "", submitted: null};
    mvSetRec(MST_LAST.master || {});
    render(); mvPoll();
    $("#view").scrollIntoView({behavior: "smooth", block: "start"});   // 停在票面栏：窄屏两栏堆叠时核对区在它下面，滚到核对区会让人以为票面栏没动静
    toast((Object.keys(MV.edit).length
            ? "已载入主单 " + MV.mawb + "：本机未提交的 " + Object.keys(MV.edit).length + " 列改动已恢复"
              + (MV.conflicts.length ? "（其中 " + MV.conflicts.length + " 列与最新解析对不上，见下方红条）" : "")
            : "已载入主单 " + MV.mawb + " 的解析结果")
          + "（36 列可改，提交前由服务端重算红旗）", "ok");
    return;
  }
});
/* ── 三个视图（§3.3）：核对工作台 / 今日台账 / 统计，同一页互切，票据与草稿都不重载 ─────
   今日台账读 /day：那一天这台机器经手过哪些票、谁提交的、还有几张没回公司。
   统计读 /stats：模型那一版与人最后定下的那一版逐列比（数只在 stats.py 算一处）。
   公司的发送状态不在这两张表里（要按主单去公司查，有限流），所以指路去「主单检索」。 */
const VIEW = {cur: "desk", rows: [], date: ""};
const ST = {rows: [], summary: {}};
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
  if (VIEW.cur === "day") loadDay();
}
const todayStr = () => {
  const n = new Date(), p = x => String(x).padStart(2, "0");
  return n.getFullYear() + "-" + p(n.getMonth() + 1) + "-" + p(n.getDate());
};
async function loadDay(){
  const box = $("#dayBox");
  box.innerHTML = '<div class="empty">正在读今天的台账…</div>';
  try{
    const picked = $("#dayDate").value;
    const r = await fetch(BASE + "/day" + (picked && picked !== VIEW.date ? "?date=" + encodeURIComponent(picked) : ""));
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : ("HTTP " + r.status));
    VIEW.date = j.date; VIEW.rows = j.rows || [];
    $("#dayDate").value = j.date;               // 以服务端那天为准：服务器与本机时区可能不同
    renderDay();
  }catch(e){
    box.innerHTML = `<div class="empty empty-err">读台账失败：${esc(e.message || e)}</div>`;
  }
}
/* 今日台账：分单与主单合一张表（行上带 kind），作废是"从台账与统计里收起来"的软闸门。
   dayConfirm 只活在内存里：刷新页面就该问第二遍，记进 localStorage 反而会把上一轮的确认
   留到下一次误点。 */
let dayOnlyVoid = false, dayConfirm = "";
const KIND_CN = {hawb: "分单", mawb: "主单"};
function voidCell(r){
  const k = r.kind + "|" + (r.kind === "mawb" ? r.mawb : r.stem);
  if (r.voided)
    return '<button class="btn sm" type="button" data-undo="' + esc(k) + '" title="当初作废：' +
           esc(r.voided.by || "?") + " " + esc(r.voided.at || "") + '">恢复</button>';
  if (dayConfirm === k)
    return '<span class="vq">作废这一行？原件、暂存与提交记录都不动</span>' +
           '<button class="btn sm danger" type="button" data-void="' + esc(k) + '">确认作废</button>' +
           '<button class="btn sm" type="button" data-vno="1">取消</button>';
  return '<button class="btn sm gh" type="button" data-void="' + esc(k) +
         '" title="从台账与统计里收起来，随时能恢复；票面原件、暂存与提交台账一律不动">作废</button>';
}
function renderDay(){
  const s = L.daySummary(VIEW.rows);
  $("#dayCnt").textContent = `${s.total} 张 · 已提交 ${s.submitted} · 待提交 ${s.pending}`
    + (s.failed ? ` · 失败 ${s.failed}` : "") + (s.flags ? ` · 红旗 ${s.flags} 条` : "")
    + (s.voided ? ` · 已作废 ${s.voided}` : "");
  const rows = L.dayVisible(VIEW.rows, dayOnlyVoid);
  if (!rows.length){
    $("#dayBox").innerHTML = VIEW.rows.length
      ? '<div class="empty">' + (dayOnlyVoid ? "这一天没有作废的票。" : "这一天的票都被作废了。勾上「只看作废」翻回来。")
        + '</div>'
      : '<div class="empty">这一天这台机器没经手过票。换个日期，或点「刷新」。</div>';
    return;
  }
  const html = rows.map(r => {
    const m = r.kind === "mawb";
    const no = m
      ? `<button type="button" class="lk" data-mopen="${esc(r.mawb)}" title="回工作台按这个主单号检索并打开核对区">${esc(r.mawb)}</button>`
      : r.has_original
        ? `<button type="button" class="lk" data-open="${esc(r.stem)}" title="回到工作台接着核这张票">${esc(r.hawb || r.stem)}</button>`
        : esc(r.hawb || (m ? r.mawb : "没取到分单号"));
    const st = r.failed ? '<span class="badge miss">解析失败</span>'
      : r.submitted ? '<span class="badge same">已提交</span>' : '<span class="badge warn">未提交</span>';
    return `<tr class="${r.failed ? "flag" : ""}${r.voided ? " voided" : ""}">
      <td class="kcol"><span class="kind k${esc(r.kind)}">${KIND_CN[r.kind] || esc(r.kind)}</span></td>
      <td>${no}</td>
      <td>${m ? '<span class="hint">本行就是主单</span>' : esc(r.mawb || "—")}</td>
      <td>${st}${m && !r.failed ? ` <span class="hint">${r.filled}/${r.cols_total} 列</span>` : ""}</td>
      <td>${r.flags ? `<span class="badge warn">红旗 ${r.flags}</span>` : '<span class="badge">无</span>'}</td>
      <td>${r.submitted ? esc(r.submitted.reviewer + " · " + r.submitted.at +
                             (r.submitted.action ? " · " + r.submitted.action : "")) : "—"}</td>
      <td>${esc((r.channel || "—") + " · " + (r.elapsed || 0) + "s")}</td>
      <td>${r.has_original
        ? `<a href="${BASE}${m ? "/mawb/source/" + encodeURIComponent(r.mawb) : "/source/" + encodeURIComponent(r.stem)}" target="_blank" rel="noopener">看原件</a>`
        : '<span class="hint">没归档原件</span>'}</td>
      <td class="kcol">${voidCell(r)}</td></tr>`;
  }).join("");
  $("#dayBox").innerHTML = `<table><thead><tr>
      <th class="kcol">类型</th><th>单号</th><th>主单</th><th>本台状态</th><th>红旗</th>
      <th>提交（谁 · 何时 · 公司回执）</th><th>解析</th><th>原件</th><th class="kcol">作废</th></tr></thead>
      <tbody>${html}</tbody></table>
      <p class="hint hintp">「公司发送状态」要按主单去公司系统查（限流 10 次/秒）：点顶栏「主单检索」查那张主单下的全部票。</p>`;
}
/* 统计页：数全部来自 /stats 的 summary（Python 一处算），这里只排版与换算百分号。
   为什么不让前端也算一遍：同一套算术写两遍迟早给出两个百分比，而这些数要拿去做决定
   ——今日台账那页就是为这件事专门留了一条用例。 */
function pct(v){ return v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%"; }
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
  const s = ST.summary;
  $("#stCnt").textContent = `可算 ${s.tickets || 0} 张 · 可比 ${s.fields_comparable || 0} 列`
    + (s.legacy ? ` · 历史无逐字段留痕 ${s.legacy} 张（不进分母）` : "");
  const head = [["逐字段准确率（模型本身读得准不准）", pct(s.acc_field)],
                ["复核后准确率（人还剩多少活；能不能对接看这个）", pct(s.acc_after_review)],
                ["一次通过率（一张没改的票占比）", pct(s.clean_rate)],
                ["制单员改的列", s.edits_doc || 0],
                ["录入员改的列", s.edits_inp || 0],
                ["未经录入员复核就发出", (s.unreviewed || 0) + " 张"]];
  let html = '<table class="list compact"><thead><tr><th>指标</th><th class="num">值</th></tr></thead><tbody>'
    + head.map(h => `<tr><td>${esc(h[0])}</td><td class="num">${esc(String(h[1]))}</td></tr>`).join("")
    + "</tbody></table>";
  if (!s.tickets){
    html += '<div class="empty">窗口内没有可算的票：点过「暂存」或「提交」之后才有逐字段留痕。'
      + '<span class="sub">本功能上线前的台账只存了字段名与哈希，比不出值，已计入上面「历史无逐字段留痕」。</span></div>';
  }
  if ((s.by_field || []).length){
    html += '<div class="t2">哪一列最常改（该修提示词还是码表，看这里）</div>'
      + '<table class="list compact"><thead><tr><th>列</th><th class="num">被改次数</th><th class="num">票数</th></tr></thead><tbody>'
      + s.by_field.slice(0, 12).map(e => `<tr><td>${esc(e.field)}</td>`
        + `<td class="num">${e.edits}</td><td class="num">${e.tickets}</td></tr>`).join("")
      + "</tbody></table>";
  }
  if ((s.groups || []).length){
    const ks = Object.keys(s.groups[0]);
    html += '<div class="t2">分组看</div><table class="list compact"><thead><tr>'
      + ks.map(c => `<th>${esc(c)}</th>`).join("") + "</tr></thead><tbody>"
      + s.groups.map(g => "<tr>" + ks.map(c => `<td>${esc(String(g[c]))}</td>`).join("") + "</tr>").join("")
      + "</tbody></table>";
  }
  if (ST.rows.length){
    /* 两个号各占一列（2026-10-09 用户："挤在一格里眼花缭乱"）：连成一格时列一窄就断在点号后面，
       每格变成"半行 + 孤零零一行"，竖着看全是锯齿。拆开后各自不许断行、宽度按字符数给足，
       等宽数字天然对齐。一个号都没解析出来时才退回票名——整列都写"缺号"反而认不出是哪张。
       票名与原始文件名留在悬停提示里：找回那张 PDF 全靠它。模型那一列删了（要横评模型看上面的分组轴）。 */
    html += '<div class="t2">逐张</div><table class="list compact"><thead><tr>'
      + '<th>主单号</th><th>分单号</th><th>上传人</th><th>暂存人</th><th class="num">可比</th>'
      + '<th class="num">改动</th><th>制单员改的</th><th>录入员改的</th></tr></thead><tbody>'
      + ST.rows.map(r => {
        const m = r.mawb ? esc(r.mawb) : (r.hawb ? "—" : esc(r.stem));
        return `<tr class="${r.legacy ? "hit" : ""}">
        <td class="tno" title="${esc((r.stem || "") + (r.source_name && r.source_name !== r.stem ? " · " + r.source_name : ""))}">${m}</td><td class="tno">${esc(r.hawb || "—")}</td>
        <td>${esc((r.uploader || "—") + (r.uploader_role ? "（" + L.roleCn(r.uploader_role) + "）" : ""))}</td>
        <td>${esc(r.stager || "—")}</td>
        <td class="num">${r.legacy ? "—" : r.comparable}</td>
        <td class="num">${r.legacy ? "无留痕" : r.total}</td>
        <td>${esc((r.doc || []).join("、") || "—")}</td>
        <td>${esc((r.inp || []).join("、") || "—")}</td></tr>`; }).join("") + "</tbody></table>";
  }
  html += '<p class="hint hintp">两个数不能混：<b>逐字段准确率</b>问模型读得准不准；'
    + '<b>复核后准确率</b>问录入员还剩多少活——能不能把提取结果直接喂给平台，看后者。'
    + '没暂存记录就直发的票，改动全记在制单员档（那些值确实是核对台上的人改的），'
    + '「未经录入员复核」那一列是它的对照组。</p>';
  $("#stBox").innerHTML = html;
}
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
$("#btnStats").addEventListener("click", () => { $("#statsWin").hidden = false; loadStats(); });
$("#statsClose").addEventListener("click", () => { $("#statsWin").hidden = true; });
$("#stGo").addEventListener("click", loadStats);
$("#stCsv").addEventListener("click", () => { window.location = BASE + "/stats/export" + statsQS(); });
["stFrom", "stTo", "stGroup"].forEach(id => $("#" + id).addEventListener("change", loadStats));
$("#dayGo").addEventListener("click", loadDay);
$("#dayDate").addEventListener("change", () => { VIEW.date = ""; loadDay(); });
$("#dayOnlyVoid").addEventListener("change", e => { dayOnlyVoid = e.target.checked; dayConfirm = ""; renderDay(); });
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
  loadDay();      // 以服务端为准重画：作废本落在服务器上，别人那一屏也要跟着变
}
$("#dayBox").addEventListener("click", e => {
  const o = e.target.closest("[data-open]");
  if (o){ setView("desk"); openHouse(o.dataset.open); return; }
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
    if (dayConfirm !== v.dataset.void){ dayConfirm = v.dataset.void; renderDay(); return; }
    postVoid(v.dataset.void, true); return;
  }
  const n = e.target.closest("[data-vno]");
  if (n){ dayConfirm = ""; renderDay(); return; }
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
    const g = e.target.closest && e.target.closest("#gripCol");
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
    document.querySelectorAll("#gripCol.drag").forEach(g => g.classList.remove("drag"));
    if (st.v) localStorage.setItem(LS.colK, st.v);
    st = null;
  });
  document.addEventListener("keydown", e => {
    const g = e.target.closest && e.target.closest("#gripCol");
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
applyDeskPrefs(); bindSplitGrip(); bindColGrip(); bindEdgeGrip();
checkAuth();
