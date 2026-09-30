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
};
if (typeof module !== "undefined") module.exports = L;
