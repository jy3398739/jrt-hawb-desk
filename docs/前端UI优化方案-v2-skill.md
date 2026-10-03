# HAWB 主分单审核台 · 前端 UI 优化方案（frontend-skill 版）


> **状态（2026-10-03）：已被本轮 v2 改版取代，留档只为说清"为什么最后没照抄"。**
> 三条实测差异：① 里面 6 条"待修缺陷"（主单草稿进 localStorage、票行/字段名 button 化、
> 角色名转义、toast 队列）9 月那轮 `技术栈与前端优化-design.md §3.4` 就上线了；
> ② 内联样式只数了 `index.html` 的 8 处，`desk.js` 模板字符串里还有 32 处（更大的源头）；
> ③ 色板按仓库自己那条 WCAG 用例算有四个值不过 4.5:1（#64748b 4.32 / #d97706 2.89 /
> #dc2626 4.38 / #059669 3.42），因为把"状态点"和"状态文字"当成同一个颜色用了。
> 另外它假设存在的"视觉快照测试"并不存在；`--todoH` 是 JS 实测写入的，写成静态令牌会退回旧 bug。

> 日期：2026-10-03
> 基线：v1.0.5（commit 3f3309d），回归 324 项全绿
> 技能：frontend-skill（克制构图、图像主导、内容结构、有品动效）
> 约束：零构建、原生 JS、不引入框架/打包器、不改业务口径、不改后端 API

---

## 〇、设计三句话（frontend-skill 工作模型）

- **视觉命题**：航空货运的「控制塔」——冷静、精确、信息密集但不嘈杂，像塔台屏幕一样一眼即读。
- **内容计划**：工作台即全部。无 hero、无营销文案；首屏 = 票面 + 字段 + 状态窄轨，制单员坐下就干活。
- **交互命题**：三个有品动效——①票面页图淡入+缩放呼吸（材料感）；②窄轨状态点滑出列表（层级感）；③字段定位时票面 bbox 脉冲+自动巡航（空间感）。

---

## 一、现状诊断（frontend-skill 视角）

### 1.1 违背 skill 原则的问题

| 原则 | 现状 | 差距 |
|---|---|---|
| **Cardless by default** | 卡片嵌卡片：`.card` 里套 `.chips`、`.tools`、`.ft`、`.hd`，视觉噪音大 | 卡片只在「上传分单」和「主单检索」两个真交互容器保留，字段表/票面/台账改为纯布局 |
| **One accent color** | 实际使用蓝(`--edit`)、绿(`--ok`)、橙(`--warn`)、红(`--bad`) 四色竞争 | 确立 **一个功能主色**（蓝）+ 三个**语义状态色**（绿/橙/红仅用于状态点、红旗、校验提示，不参与装饰） |
| **Utility copy** | 登录页提示语偏长；部分空状态文案像说明书 | 文案压缩为操作指引，删除解释性句子 |
| **Motion with purpose** | 仅 1 处 `transition:.12s`、1 处 pulse 动画、1 处 bbox flash | 系统建立动效层级：入场 → 状态反馈 → 空间定位 |
| **Strong typography** | 字号 11px~16px 共 7 种，无字重层级 | 压缩为 4 档字号 + 2 档字重，数字用 tabular-nums |

### 1.2 技术债（保留此前方案发现）

- tokens.css 仅 6 变量，desk.css 50+ 硬编码色值
- index.html 24 处内联 style
- 圆角 6 种混用（4/6/7/8/10/99px）
- 无阴影层级体系

---

## 二、优化目标（skill 校准后）

| 维度 | 目标 | skill 依据 |
|---|---|---|
| 构图 | 首屏无卡片堆砌，票面为视觉锚点 | App UI: primary workspace first |
| 色彩 | 1 个功能主色 + 3 个语义状态色，其余中性灰 | One accent color |
| 字体 | 2 种字重（400/600）+ 4 档字号 | Two typefaces max → 单字体双字重 |
| 动效 | 3 个有品动效，全部服务功能 | Motion: presence and hierarchy |
| 文案 | 操作指引化，删除解释性文字 | Utility copy |
| 技术 | 设计令牌归口，零硬编码 | 项目既有约束 |

---

## 三、具体方案

### 3.1 设计令牌层（tokens.css 重写）

```css
:root{
  /* ── 中性基底（控制塔灰） ── */
  --c-bg: #f2f4f7;              /* 页面底：比当前更冷一度 */
  --c-surface: #ffffff;
  --c-surface-2: #f8fafc;       /* 表格头、工具栏 */
  --c-line: #e4e9f0;
  --c-line-strong: #cbd5e1;

  /* ── 文字：3 级，全部过 WCAG AA ── */
  --c-text-1: #0f172a;          /* 主要 */
  --c-text-2: #475569;          /* 次要 */
  --c-text-3: #64748b;          /* 辅助 */

  /* ── 功能主色：控制塔蓝 ── */
  --c-accent: #2563eb;
  --c-accent-hover: #1d4ed8;
  --c-accent-soft: #eff6ff;     /* 选中底色 */
  --c-accent-border: #93c5fd;

  /* ── 语义状态色（仅用于状态，不用于装饰） ── */
  --c-ok: #059669;    --c-ok-bg: #ecfdf5;
  --c-warn: #d97706;  --c-warn-bg: #fffbeb;
  --c-bad: #dc2626;   --c-bad-bg: #fef2f2;

  /* ── 票面定位高亮（功能色，不参与 UI 装饰） ── */
  --c-locate: rgba(251,191,36,.35);

  /* ── 间距：4 的倍数 ── */
  --s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-5: 24px; --s-6: 32px;

  /* ── 字号：4 档 ── */
  --f-xs: 11px;  --f-sm: 12px;  --f-md: 13px;  --f-lg: 15px;

  /* ── 字重：2 档 ── */
  --w-normal: 400; --w-bold: 600;

  /* ── 圆角：3 档 ── */
  --r-sm: 4px; --r-md: 8px; --r-pill: 99px;

  /* ── 阴影：3 级，全部用于功能层级 ── */
  --shadow-1: 0 1px 2px rgba(15,23,42,.06);
  --shadow-2: 0 4px 16px rgba(15,23,42,.10);
  --shadow-3: 0 8px 32px rgba(15,23,42,.16);

  /* ── 层级 ── */
  --z-sticky: 20; --z-rail-pop: 40; --z-toast: 60; --z-gate: 80;

  /* ── 动效曲线 ── */
  --t-out: cubic-bezier(.22,1,.36,1);      /* 标准出场 */
  --t-spring: cubic-bezier(.34,1.56,.64,1); /* 弹性反馈 */
  --d-fast: .15s; --d-normal: .25s; --d-slow: .4s;

  /* ── 布局 ── */
  --hdh: 52px;
  --todoH: 36px;
  --railW: 48px;
}
```

### 3.2 布局重构：Cardless 工作台

**文件**：`web/css/desk.css`、`web/index.html`

| 区域 | 现状 | 改为 |
|---|---|---|
| 票面浏览 `#view` | `.card` 有边框+圆角 | **去卡片化**：无边框、无圆角、无背景色，直接融入 `--c-bg`，成为「画布」 |
| 字段核对 `#main` | `.card` 有边框+圆角 | **去卡片化**：左右留 `--s-4` 空隙，底部 sticky 操作条加 `--shadow-2` 浮起 |
| 侧栏 `#side` | 三张 `.card`（上传/检索/暂存） | 保留卡片（真交互容器），但卡片圆角统一 `--r-md`，阴影降为 `--shadow-1` |
| 窄轨 `.rail` | 无卡片，正确 | 保持，展开列表加滑入动效 |
| 今日台账 `#dayCard` | `.card` | 去卡片化，整页表格直接铺在 `--c-bg` 上 |

**去卡片化后的视觉分隔手段**（skill: dividers, not cards）：
- 票面与字段之间：1px `--c-line` 垂直分隔线
- 字段表内部：行底 1px `--c-line`，组标题行背景 `--c-surface-2`
- 待办条：底部 1px `--c-line` + 背景 `--c-surface`

### 3.3 三个有品动效

**文件**：`web/css/desk.css`、`web/js/desk.js`

#### 动效 1：票面页图入场（材料感）
```css
.pv img.pvpage{
  opacity: 0; transform: scale(.96) translateY(8px);
  transition: opacity var(--d-slow) var(--t-out), transform var(--d-slow) var(--t-out);
}
.pv img.pvpage.on{ opacity: 1; transform: scale(1) translateY(0); }
```
- JS 在 `img` 加载完成后加 `.on` 类
- 多页时依次延迟 60ms 入场（`transition-delay`）

#### 动效 2：窄轨状态点滑出列表（层级感）
```css
.railList{
  transform: translateX(-12px); opacity: 0;
  transition: transform var(--d-normal) var(--t-out), opacity var(--d-normal) var(--t-out);
  pointer-events: none;
}
.railList.open{ transform: translateX(0); opacity: 1; pointer-events: auto; }
```
- 状态点本身加 `transition: transform var(--d-fast) var(--t-spring)`，hover 时 `scale(1.15)`

#### 动效 3：字段定位票面巡航（空间感）
```css
.pv .bbox{
  border: 2px solid var(--c-accent);
  background: var(--c-locate);
  animation: locatePulse 1.6s var(--t-out) 2;
}
@keyframes locatePulse{
  0%{ box-shadow: 0 0 0 0 rgba(37,99,235,.4); }
  50%{ box-shadow: 0 0 0 8px rgba(37,99,235,.1); }
  100%{ box-shadow: 0 0 0 0 rgba(37,99,235,0); }
}
```
- 自动滚动到 bbox 时加 `scroll-behavior: smooth`
- `prefers-reduced-motion` 时全部动画降级为即时切换

### 3.4 字体与数字

**文件**：`web/css/tokens.css`、`web/css/desk.css`

```css
body{
  font: var(--w-normal) var(--f-md)/1.5 "Inter", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  font-feature-settings: "tnum";  /* 全局数字等宽 */
}
```
- 等宽数字对制单员至关重要：件数/重量/单号对齐比较
- 中文字体保持系统栈，不引入 webfont（内网环境）

### 3.5 文案压缩（Utility Copy）

**文件**：`web/index.html`

| 位置 | 现状 | 改为 |
|---|---|---|
| 登录提示 | 「管理员 admin 登录后，可在「账号管理」给制单员（马殿齐 / 宛平 / 陈新 / 叶庭伸 / 赵文宇 / 杨皓荃 / 隗一航 / 董文志）下发或重置密码。」 | 「管理员登录后可在账号管理中下发制单员口令。」 |
| 空票面 | 「上传分单后这里显示票面原件，和右边字段逐条核对。」 | 「上传分单后显示票面。」 |
| 空字段 | 「上传分单后在这里逐字段核对。左边是票面原件，这里是航空口径全字段（国家两位码 / 城市三字码 / 电话归一），可直接改。点字段名可在票面上定位并放大对应原文。」 | 「上传后核对字段。点字段名定位票面原文。」 |
| 上传区 | 「把分单拖进来 / 图片 / PDF / Excel 电子单，逐张解析后在此排队」 | 「拖入分单 / 图片、PDF、Excel」 |
| 主单检索提示 | 「按主单号取回主单原生资料并自动解析；名下分单列在页面底部长条里，原件点开即看。原录入员检索台已并入本页。」 | 「输入主单号检索，名下分单显示在下方。」 |

### 3.6 交互缺陷修复（保留此前方案）

| # | 缺陷 | 修法 |
|---|---|---|
| 1 | 主单编辑刷新即丢 | 复用分单草稿机制，键 `hawb.review.masterDrafts` |
| 2 | 可点元素非 button | 票行/字段名/chip/还原全改 `<button>` |
| 3 | 状态仅靠色点 | 色点旁加文字标签 + aria-label |
| 4 | 未转义插值 | 全部 innerHTML 拼接前过 `esc()` |
| 5 | toast 覆盖 | 失败 toast 不自动消失，点击关闭 |
| 6 | 录入员角色标签错误 | 检查 `roleCn()` 调用处 |

---

## 四、实施步骤（10 步，每步独立可回退）

| 步 | 内容 | 文件 | 改动量 |
|---|---|---|---|
| 1 | tokens.css 重写：完整令牌体系 | tokens.css | ~100 行重写 |
| 2 | desk.css 硬编码色值 → 变量替换 | desk.css | ~60 处 |
| 3 | 去卡片化：票面/字段/台账去 `.card` | desk.css + index.html | ~20 处 |
| 4 | 圆角/阴影/间距/字号归一 | desk.css | ~40 处 |
| 5 | index.html 内联 style → class + 文案压缩 | index.html | ~30 处 |
| 6 | 三个有品动效实现 | desk.css + desk.js | ~30 行 |
| 7 | 主单草稿 localStorage | desk.js | ~50 行 |
| 8 | button 化 + aria + 状态文字 | index.html + desk.css + desk.js | ~25 处 |
| 9 | `prefers-reduced-motion` 降级 | desk.css | ~10 行 |
| 10 | 回归 324 项 + 前端逻辑 node 测试 | tests/ | 全绿 |

---

## 五、验证清单

- [ ] tokens.css 无硬编码色值（grep `#` 仅变量定义行）
- [ ] desk.css 无硬编码色值
- [ ] index.html 无内联 `style=`（除动态计算值）
- [ ] 票面/字段/台账区域无 `.card` 边框
- [ ] 全部可点元素为 `<button>` 或原生交互元素
- [ ] 主单编辑刷新后保留
- [ ] 窄轨展开有滑入动画
- [ ] 票面页图加载有淡入动画
- [ ] 字段定位 bbox 有脉冲动画
- [ ] `prefers-reduced-motion` 时动画全部关闭
- [ ] 数字全局等宽（`font-feature-settings: "tnum"`）
- [ ] 文案压缩后无解释性长句
- [ ] 324 项回归测试全绿
- [ ] WCAG AA 对比度抽检通过

---

## 六、与上一版方案的差异

| 维度 | 上一版（技术驱动） | 本版（frontend-skill 驱动） |
|---|---|---|
| 核心思路 | 设计令牌归口 + 缺陷修复 | **构图重构**（去卡片化）+ 令牌归口 + 缺陷修复 |
| 色彩 | 语义化命名 | 控制塔灰基底 + 单功能主色 + 语义状态色 |
| 动效 | 过渡归一 | **3 个有品动效**：入场/层级/空间 |
| 字体 | 未涉及 | 4 档字号 + 2 档字重 + **全局数字等宽** |
| 文案 | 未涉及 | **Utility copy 压缩** |
| 布局 | 保持 B3+窄轨 | B3+窄轨 + **去卡片化** |

---

## 七、风险与回退

| 风险 | 缓解 |
|---|---|
| 去卡片化后区域边界不清晰 | 用 `--c-line` 分隔线 + 背景色差维持边界；回归测试含视觉断言 |
| 动效在低配机器卡顿 | `prefers-reduced-motion` 强制降级；动画全部 `transform/opacity` 不触发重排 |
| 文案压缩后信息不足 | 压缩的是解释性文字，操作指引保留；制单员已培训 |
| 主单草稿引入新 bug | 复用分单已验证逻辑；新增测试用例 |

**回退**：每步独立提交，`git revert` 单步即可。
