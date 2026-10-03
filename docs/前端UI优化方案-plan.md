# HAWB 主分单审核台 · 前端 UI 整体美化与体验优化方案


> **状态（2026-10-03）：已被本轮 v2 改版取代，留档只为说清"为什么最后没照抄"。**
> 三条实测差异：① 里面 6 条"待修缺陷"（主单草稿进 localStorage、票行/字段名 button 化、
> 角色名转义、toast 队列）9 月那轮 `技术栈与前端优化-design.md §3.4` 就上线了；
> ② 内联样式只数了 `index.html` 的 8 处，`desk.js` 模板字符串里还有 32 处（更大的源头）；
> ③ 色板按仓库自己那条 WCAG 用例算有四个值不过 4.5:1（#64748b 4.32 / #d97706 2.89 /
> #dc2626 4.38 / #059669 3.42），因为把"状态点"和"状态文字"当成同一个颜色用了。
> 另外它假设存在的"视觉快照测试"并不存在；`--todoH` 是 JS 实测写入的，写成静态令牌会退回旧 bug。

> 日期：2026-10-03
> 基线：v1.0.5（commit 3f3309d），回归 324 项全绿
> 方向：**整体 UI 美化 + 体验优化**（用户定案）
> 约束：零构建、原生 JS、不引入框架/打包器、不改业务口径、不改后端 API

---

## 一、现状诊断

### 1.1 设计令牌层（tokens.css）
- **现状**：仅 6 个变量（`--bg/--panel/--line/--ink/--ink2/--ink3/--ok/--warn/--bad/--edit/--chip/--hdh/--todoH`），约 10 个色值
- **问题**：
  - 无语义化命名（`--edit` 实为品牌主色，`--chip` 是背景色而非组件）
  - 无状态色变体（成功/警告/错误的背景色、悬停色）
  - 无间距/字号/圆角/阴影/层级令牌
  - 无暗色模式预留

### 1.2 组件样式层（desk.css）
- **现状**：245 行，覆盖 30+ 组件
- **问题**：
  - **50+ 处硬编码色值**：`#f4f6f9`、`#fff`、`#e2e7ee`、`#1c2430`、`#5b6879`、`#616e80`、`#1f8b4d`、`#b8602a`、`#c0392b`、`#1c5fbf`、`#eef2f7`、`#eaf1fb`、`#cfe0f7`、`#9cc3ee`、`#c6d2e2`、`#fafcfe`、`#f7faff`、`#eef5ff`、`#fdf1e6`、`#eccdb0`、`#8c4a17`、`#fdeceb`、`#eec7c3`、`#9c2b20`、`#f3f6fa`、`#fbfcfe`、`#dfe6ee`、`#e6f0fb`、`#f0f3f7`、`#fdfefe`、`#eff3f7`、`#fff8f1`、`#f3e3d3`、`#c8b39a`、`#22303f`、`#186b3d`、`#8f2b21`、`#eef7f0`、`#f4f9ff`、`#eef1f6`、`#e2e7ee` 等
  - **24 处内联 style**（index.html 中 `style="margin-top:14px"` 等）
  - 无统一圆角规范（4px/6px/7px/8px/10px/99px 混用）
  - 无统一阴影规范（仅 3 处定义，无层级体系）
  - 无过渡动画规范（`transition:.12s` 仅 1 处，大量状态切换无动画）

### 1.3 交互体验层（desk.js + index.html）
- **已落地的优秀交互**：
  - 票面页图自渲染 + 滚轮缩放钉光标 + 按住拖动
  - 点字段名定位票面高亮
  - toast 队列（上限 4 条，不互相覆盖）
  - 轮询退避调度器
  - 本地草稿 localStorage
- **待修缺陷**（文档 §3.4 已列）：
  1. 主单 36 列编辑与红旗确认不进 localStorage（刷新即丢）
  2. 可点非元素未全部 `<button>` 化（票行、字段名、chip、还原）
  3. 状态仅靠色点区分（无文字/图标）
  4. `u.role` 等未转义插值
  5. 错误提示互相覆盖
  6. 录入员角色标签显示错误

### 1.4 布局结构
- **B3 + 窄轨已落地**：48px 状态窄轨 + 264px 侧栏 + minmax(420px,1.18fr) 票面 + minmax(360px,1fr) 字段
- **问题**：
  - 窄轨展开列表无动画
  - 三栏在 1460px 以下断为两栏、1020px 以下断为单栏，但断点间无平滑过渡
  - 票面浏览 `#view` 固定 `calc(100vh - 78px)`，顶栏高度变化时不同步

---

## 二、优化目标

| 维度 | 目标 |
|---|---|
| 视觉 | 建立完整设计令牌体系，消除全部硬编码色值与内联样式 |
| 一致性 | 统一圆角/阴影/间距/字号/过渡规范 |
| 交互 | 修复全部已知缺陷，提升反馈即时性 |
| 可访问性 | WCAG AA 对比度、键盘可达、读屏器友好 |
| 性能 | 减少重绘、优化滚动、图片懒加载 |

---

## 三、具体方案

### 3.1 设计令牌层重构（tokens.css 全面重写）

**文件**：`web/css/tokens.css`

**变更**：从 6 个变量扩为完整令牌体系

```css
:root{
  /* ── 色彩：语义化命名 ── */
  /* 背景 */
  --c-bg: #f4f6f9;
  --c-surface: #ffffff;
  --c-surface-raised: #fbfcfe;
  /* 线条 */
  --c-line: #e2e7ee;
  --c-line-strong: #c6d2e2;
  /* 文字 */
  --c-text-1: #1c2430;
  --c-text-2: #5b6879;
  --c-text-3: #616e80;
  /* 品牌/交互 */
  --c-accent: #1c5fbf;
  --c-accent-hover: #18519f;
  --c-accent-bg: #eaf1fb;
  --c-accent-border: #9cc3ee;
  /* 状态 */
  --c-ok: #1f8b4d;
  --c-ok-bg: #eef7f0;
  --c-warn: #b8602a;
  --c-warn-bg: #fdf1e6;
  --c-warn-border: #eccdb0;
  --c-bad: #c0392b;
  --c-bad-bg: #fdeceb;
  --c-bad-border: #eec7c3;
  /* 芯片/标签 */
  --c-chip-bg: #eef2f7;
  /* 特殊 */
  --c-highlight: rgba(255,196,0,.30);
  --c-highlight-strong: rgba(255,196,0,.62);

  /* ── 间距 ── */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px;  --s-4: 16px;  --s-5: 24px;  --s-6: 32px;

  /* ── 字号 ── */
  --f-xs: 11px;  --f-sm: 12px;  --f-md: 13px;  --f-lg: 14px;  --f-xl: 16px;

  /* ── 圆角 ── */
  --r-sm: 4px;  --r-md: 6px;  --r-lg: 8px;  --r-xl: 10px;  --r-pill: 99px;

  /* ── 阴影 ── */
  --shadow-sm: 0 1px 3px rgba(20,32,48,.08);
  --shadow-md: 0 4px 12px rgba(20,32,48,.12);
  --shadow-lg: 0 6px 24px rgba(20,32,48,.16);

  /* ── 层级 ── */
  --z-sticky: 20;
  --z-dropdown: 70;
  --z-toast: 60;
  --z-gate: 80;

  /* ── 过渡 ── */
  --t-fast: .12s ease;
  --t-normal: .18s ease;
  --t-slow: .24s ease;

  /* ── 布局 ── */
  --hdh: 54px;
  --todoH: 37px;
  --railW: 48px;
}
```

**同步修改**：`desk.css` 中全部 50+ 硬编码色值替换为变量引用。

### 3.2 组件样式归一（desk.css 修订）

**文件**：`web/css/desk.css`

| 组件 | 修改内容 |
|---|---|
| 全局 | 字体栈统一；`box-sizing` 保留；`:focus-visible` 描边色改 `--c-accent` |
| 顶栏 `.hdr` | 高度改 `--hdh`；padding 用 `--s-3`/`--s-4`；增加底部阴影 `--shadow-sm` |
| 卡片 `.card` | 圆角统一 `--r-xl`；边框色 `--c-line`；hover 时 `--shadow-md` |
| 按钮 `.btn` | 圆角 `--r-md`；padding 统一；增加 `transition: var(--t-fast)`；hover 效果统一 |
| 输入框 | 边框色 `--c-line`；focus 描边 `--c-accent-border`；背景 `--c-surface-raised` |
| 表格 | 表头背景 `--c-surface-raised`；行 hover `--c-accent-bg` |
| 票据行 `.row` | 增加 `transition: background var(--t-fast)`；选中态用 `--c-accent-bg` + 左侧 3px `--c-accent` |
| 芯片 `.chip` | 圆角 `--r-pill`；状态色变体统一 |
| 待办条 `.todo` | 背景 `--c-surface`；底部边框 `--c-line` |
| 票面浏览 `#view` | 高度计算改为 `calc(100vh - var(--hdh) - 2 * var(--s-4))` |
| 窄轨 `.rail` | 展开列表增加 `transform` 滑入动画 |
| Toast | 背景色改 `--c-text-1`（成功）/ `--c-bad`（失败）；圆角 `--r-lg` |
| 登录遮罩 `.gate` | 背景增加 `backdrop-filter: blur(4px)` |

### 3.3 交互体验修复

**文件**：`web/js/desk.js`（修订）、`web/index.html`（修订）

| # | 缺陷 | 修复方案 | 涉及文件 |
|---|---|---|---|
| 1 | 主单编辑不进 localStorage | 复用分单草稿机制，键改为 `hawb.review.masterDrafts`，`MV` 对象序列化含 `edit/acked/editBase` | desk.js |
| 2 | 可点元素非 button | 票行 `.row` → `<button class="row">`；字段名 `.kbtn` → `<button>`；chip 内动作 → `<button>`；还原 `.rs` → `<button>` | index.html + desk.css |
| 3 | 状态仅靠色点 | 状态点旁增加文字标签（`title` + `aria-label`）；窄轨 tooltip 显示完整状态文本 | desk.js |
| 4 | 未转义插值 | 全部 `innerHTML` 拼接前过 `esc()`；`u.role` 等用户输入强制转义 | desk.js |
| 5 | toast 覆盖 | 已修为队列（上限 4 条），但需确保失败 toast 不自动消失（点击关闭） | desk.js |
| 6 | 录入员角色标签 | `roleCn()` 已支持，检查渲染处是否正确调用 | desk.js |

### 3.4 响应式与动效

**文件**：`web/css/desk.css`

- **断点过渡**：在 1460px 和 1020px 断点间增加 `transition: grid-template-columns var(--t-slow)`（需 JS 辅助 class 切换）
- **票面加载**：增加 `<img>` 懒加载 `loading="lazy"`；页图加载时显示骨架屏
- **窄轨展开**：`.railList` 增加 `transform: translateX(-8px); opacity: 0` → `translateX(0); opacity: 1` 动画
- **字段定位高亮**：`.bbox` 动画保留，但增加 `prefers-reduced-motion` 媒体查询降级

### 3.5 可访问性增强

- **对比度**：全部文本色通过 WCAG AA（4.5:1），`--c-text-3` 已提至 `#616e80`
- **键盘导航**：全部交互元素 `<button>`/`<input>`/`<select>` 化；`tabindex` 检查
- **焦点可见**：`:focus-visible` 统一 `outline: 2px solid var(--c-accent)`
- **读屏器**：`aria-live="polite"` 区域（toast、状态栏）；`role="status"` 补充

---

## 四、实施步骤

| 步骤 | 内容 | 文件 | 预估改动量 |
|---|---|---|---|
| 1 | 重写 tokens.css：完整令牌体系 | tokens.css | ~80 行重写 |
| 2 | desk.css 硬编码色值 → 变量替换 | desk.css | ~60 处替换 |
| 3 | desk.css 圆角/阴影/间距/过渡归一 | desk.css | ~40 处调整 |
| 4 | index.html 内联样式 → class | index.html | ~8 处 |
| 5 | 修复主单 localStorage 草稿 | desk.js | ~50 行新增 |
| 6 | 可点元素 button 化 + aria 补充 | index.html + desk.css | ~20 处 |
| 7 | 状态文字标签 + 角色显示修复 | desk.js | ~10 处 |
| 8 | 响应式过渡 + 动效增强 | desk.css | ~15 处 |
| 9 | 可访问性终检（对比度/键盘/读屏） | 全部 | 测试驱动 |
| 10 | 回归测试（324 项）+ 前端逻辑测试 | tests/ | 确保全绿 |

---

## 五、验证清单

- [ ] tokens.css 无硬编码色值（grep `#` 仅出现在变量定义行）
- [ ] desk.css 无硬编码色值（grep 十六进制色值应返回 0）
- [ ] index.html 无内联 `style=`（除动态计算值）
- [ ] 全部可点元素为 `<button>` 或原生交互元素
- [ ] 主单编辑刷新后保留
- [ ] 窄轨展开有滑入动画
- [ ] 票面图片懒加载生效
- [ ] 324 项回归测试全绿
- [ ] 前端逻辑 node 测试全绿
- [ ] WCAG AA 对比度抽检通过

---

## 六、风险与回退

| 风险 | 缓解 |
|---|---|
| 色值替换遗漏导致视觉不一致 | 逐文件 grep 验证；回归测试含视觉快照对比（如已有） |
| 主单草稿机制引入新 bug | 复用分单已验证的 `draftMerge/draftTouch` 逻辑；新增测试用例 |
| button 化破坏现有布局 | CSS reset 已存在（`.row,.rs,.kbtn,.chipb,.lk` 的 `appearance:none` 规则），仅需扩展选择器 |
| 动画在低配机器卡顿 | `prefers-reduced-motion` 媒体查询自动降级 |

**回退策略**：每步独立提交，出问题 `git revert` 单步即可。tokens.css 与 desk.css 的变更可整文件回退。
