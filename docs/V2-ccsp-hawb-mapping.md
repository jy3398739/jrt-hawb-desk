# V2 · CCSP 国际分单录入 字段映射表（M1 草稿）

> 依据：2026-09-26 只读侦察。样本 = 平台已发送真票 主单 `999-95764373` / 分单 `VCE4373`（hawbId 13946743，PEK→VCE）。
> 站点：http://www.infoccsp.com/sso/sso-login.do（航空物流信息服务平台，中国航信/天信达）。
> 本表是 `sites/ccsp.json` 站点档案的雏形；**V2 全程 dry-run 起步，提交动作永远人工点**。

## 1. 平台页面结构（侦察实据）

- 登录：POST `/sso/sso-login.do`，字段 `companyCode / username / password / webLoginCheckCode(图形验证码) / rememberme`。**会话走 URL 重写（`;jsessionid=…`）**，自动化必须跟随重写。
- 主框架 `/sso/ui-agent_index.do`，内容在 `iframe#frmright`，菜单切换 `openInIframe(url, menuId)`。
- 分单录入入口：**国际分单 → 国际分单录入** `/fas/hawb/HAWBAction.do`
  1. `mawbVirtualForm` → `saveMAWBVirtual.do`：先录主单号建"虚拟主单"（awbpre/awbno/origin/dest/pcs/wgt）——**写操作**。
  2. 详情页 `HAWBAction_initAddHAWB.do?hmr.mawb.awbPre=…&hmr.mawb.awbNo=…&hmr.hawb.hawbNo=…`，主表单 `editHAWBForm`，72 控件，命名空间 `hmr.`。
  3. 分单列表操作：添加分单 / 发送分单 / 删除分单 / 移动分单 / 作废 —— 全部写操作。
- 平台强制确认：`hmr.hawb.needCargoNameConfirm`、`needHsCodeConfirm`（提交前必须勾/确认货名与 HS 码）。

## 2. 映射表：我们 40 字段 → 平台控件

### 2a. 直接映射（20 项）

| 我方字段（L3 air 口径） | 平台控件 `hmr.` | 备注 / 转换 |
|---|---|---|
| MAWB_NO | mawb.awbPre + mawb.awbNo | 拆 3 位前缀 + 8 位序号 |
| ORIGIN_NAME | hawb.originAirport | 已是 IATA 三字码 |
| DEST_NAME | hawb.destAirport | 同上 |
| HAWB_NO | hawb.hawbNo | ⚠️ 见 §3 待决①：平台短号制 |
| SHIPPER_INFO_COMP_NAME | hawb.shipper.name | |
| SHIPPER_INFO_COMP_ADDRESS | hawb.shipper.address | 平台单行 input，长地址可能超 |
| SHIPPER_INFO_CITTY | hawb.shipper.city | |
| SHIPPER_INFO_COUNTRY | hawb.shipper.country | 两位码 |
| SHIPPER_INFO_STATE | hawb.shipper.state | |
| SHIPPER_INFO_POSTAL | hawb.shipper.zipCode | |
| SHIPPER_INFO_TEL | hawb.shipper.tel | 样本格式 `861069479536`：去 + 去分隔符 |
| SHIPPER_INFO_FAX | hawb.shipper.fax | 空则留空（勿把 TEL 塞进来） |
| SHIPPER_INFO_TAX_ID | hawb.shipper.customsCode | 样本收货人 `IT03268900267`=VAT |
| SHIPPER_INFO_AEO | hawb.shipper.AEOCode | |
| CONSIGNEE_INFO_* 同上 8 项 | hawb.consignee.* | 另有 customsContact / customsContactNum（我们没有，默认空） |
| GOODS_INFO | hawb.cargoName | |
| GOODS_HS_CODE | hawb.hsCode | TEXTAREA，样本 `890312` |
| PIECES | hawb.pcs | |
| WEIGHT | hawb.grossWt | KG |
| SLAC | hawb.slac | |

### 2b. 我方有、平台没有（不填）

`SHIPPER/CONSIGNEE_INFO_EMAIL`、`*_INFO_EORI`（欧盟票 EORI 实务上会填进 customsCode，见样本）、`CREATE_TIME`（平台自动记）。

### 2c. 平台要、我们契约里没有（V2 默认留空，等业务定）

| 平台控件 | 含义 | 样本值 |
|---|---|---|
| hawb.transportDeclarevalue / transportInsurancevalue / customsDeclarevalue | 运输/保险/海关申报价值 | 空 |
| hawb.currency | 币种 | CNY |
| hawb.valuePaymode / othfeePaymode | 运费/杂费 预付PP·到付CC | PP / PP |
| hawb.specHandingCode | 特殊处理代码 | 空 |
| shipper/consignee.poBox | 邮政信箱 | 空 |
| hmr.hawbs[0].pcs/slac/grossWt | 分单明细行（可多行） | 同主值 |
| mawb.arrivalAirport1-4 | 主单航路多跳（挂在主单段） | VCE |

## 5. 端到端对格

### 5a. 八票扩样（2026-09-26 深夜定稿：3 张 999 + AL26090357/BJS001521213/SNA26090719/DSV1641268/DSV1641272；脚本 `poc/ccsp_compare999.py` → `ccsp_999_compare.json`；票面文字层取证定性）

**平台普查**：`分单2` 全部 PDF 文字层扫出 15 个候选主单号 → 平台存在 9（含 016-82445790 只建主单、分单合计 0）；074/232/695/014-24767341 系不存在；145-05958300 等三个号是收货人电话被误当主单（平台入口有模 7 校验兜底）。**平台也收非 999 前缀**（014 系 DSV 两张在录）。

**200 对格：一致 86 / 我方更全(contain) 35 / 口径 DIFF 28 / 我方空 27 / 平台空 7 / 双空 17。**

定性（文字层为准）：
1. **票面没有 → 平台收发货人库/模板补**：shipper city/zip（8 张里 6 张平台填 `BEIJING/101312`——深圳/青岛/石家庄发货人一律照填，模板铁证）；部分 tel。⇒ 不算我方缺陷，V2 走平台库回填或人工。
2. **票面有、我方漏（红旗全没响）——两个系统性盲区扩样坐实**：
   - **欧票 VAT/海关号漏抄：票面有的 5 张（IT×3、DE、DK-EORI）我方 5/5 全空**。DK 那张 EORI 就印在地址行里（`EORI NO DK28490704`），我方留在地址没拆。⇒ **TAX 反向红旗（含 VAT/EORI 形态、地址内拆分）列为 V2 前最高优先质检补丁**。
   - **GOODS_HS_CODE 漏抄再现**：BJS001521213 票面有 `901839,901831` 我方空（S2 提取，与 MiMo EDC 那例同款）⇒ HS 反向红旗候选坐实。
   - 地址提取偏短两例：SNA 收货地址提残（`108 --VIGNATE`）、SNA 发货地址丢 `SHIJIAZHUANG`；BJS001521213 的 HS 同票。旗没响，靠对格才发现。
3. **口径差（非错误）**：**城市码 vs 机场码**——票面 `BJS/MIL/YMQ`，平台一律机场码 `PEK/MXP/YUL`，8/8 张 origin 全如此 ⇒ V2 需要**城市码→主机场码映射表**（或人工确认）；city 精确值 vs 大城市；HS 8-10 位 vs 6 位（截前 6 位）；wt 浮点尾 0；公司名标点。
4. **我方赢面扩样确认**：多跳航路全对（DSV 两张 TO1=YVR/TO2=YUL 与平台 arrivalAirport1/2 完全一致）；货名/地址/尺寸体积我方更全（平台只填主货名）；shipper 真实 zip（深圳 518000/廊坊 065001）我方有而平台填模板；state/省我方多填（MI/TV/QC）。

**对 V2 的定案影响**：① dry-run 差异报告的基准是 **L1 票面文字层**，平台旧值只作参考；② TAX/HS 两条反向红旗是上线前置补丁；③ 城市码→机场码映射表是 M1 必备件；④ 平台"收发货人库"是 tel/zip/city 的可选回填源。

### 5b. 三票扩样（2026-09-26 晚：95764373/95765180/95766016）

**75 对格：一致 26 / 我方更全(contain) 14 / 写法或口径 DIFF 12 / 我方空 14 / 平台空 4 / 双空 5。**
并用文字层直读把"我方空"逐格定性成三类——**平台人工值不是金标准，票面才是**：

1. **票面根本没有 → 平台从收发货人库补**（我方提取天然拿不到，不算漏抄）：shipper 的 city/zip（三张票平台全填 `BEIJING/101312`，是模板值——T2 发货人明明在惠州）；T2/T3 的 shipper/consignee tel。⇒ V2 的 tel/city/zip 缺值时要么接平台库回填、要么列入人工清单，**不要当成提取质量问题**。
2. **票面有、我方没提到 = 真漏抄（红旗全没响）**：
   - **consignee 海关号/VAT 三张全漏**（票面印着 `IT03268900267`/`DE4827430`/`IT03599210261`，我方 TAX_ID 全空）——`find_missing_tax` 只认 USCI 18 位/税号标签，欧票 8-11 位 VAT 抓不到 ⇒ **候选反向红旗：欧票 VAT 形态漏抄**（与 TO1 同级优先）。
   - T1 shipper country（票面 CHINA，我方空）；T2 shipper tel 黏连（票面 `0752…6516836` 两段，我方黏成 `075265168363015`）——黏连问题独立记录。
3. **双方口径不同（不是谁错）**：origin（票面 BJS/SZX vs 平台一律 PEK——主单用机场码且平台未必按票面）；city（我方取票面精确值 Casale sul Sile/ROTH/QUARTO D'ALTINO，平台填大城市 VENICE/MUNICH）；HS（我方 8-10 位中国申报码 vs 平台 6 位国际码，映射时截 6 位即可）；wt 浮点尾 0；公司名标点。

**对 V2 的定案影响**：① dry-run 差异报告的基准是 **L1 票面文字层**，平台旧值只作参考；② TAX 反向核查要扩欧票 VAT；③ 平台"收发货人库"是 tel/zip/city 的合法来源，列为 V2 的可选回填源。

### 5c. 单票首例（999-95764373，2026-09-26 早，结论已并入 5a/5b）

**结论：22 对可比格中，一致/等价 13、我方更准 4、平台更准 5。**

- **一致**：MAWB、HAWB（票面印的就是 `VCE4373`——平台短号其实是票面号，§3① 基本解决：hawbNo 直接用票面 HAWB_NO）、DEST、TO1、PIECES/SLAC/WEIGHT、CONSIGNEE country/postal/tel。
- **我方更准 4**：货名（票面 `Inflatable boat…`，平台人工敲成 `LNFLATABLE BOAT`）；收发货人地址两条（平台截断且有手敲错 `LNDUSTRIE`）；shipper state/tax_id（平台空，我们有）。
- **平台更准 5（全是我方静默漏抄，红旗一个没响）**：shipper `country=CN`、`postal=101312`、`tel=861069479536`、consignee `tax_id=IT03268900267`、consignee `city`。→ **印证"空值无红旗"盲区；V2 dry-run 必须把"我方空、平台可能要"的字段列成人工核对清单**。
- **口径修正（映射表 2a 修改）**：**city 不能用 L3 值**——我方 L3 已把城市三字码化（shipper city=`TAO`），平台 `city` 要城市名（样本填 `BEIJING`）。V2 映射 city 一律取 **L2 原文口径**（或 CITY_IATA 反查）。
- **航路/机场码**：票面始发站 `BJS`（城市组码），平台主单填 `PEK`（机场码）→ 主单 origin/dest 平台口径是机场码，映射需按平台规则归一（待平台校验规则确认）。
- **平台数据来自收发货人库**：consignee 的 VAT 等值可能是从平台「收发货人管理」库带出的，票面上未必有 → 我方提取天然拿不到，V2 流程要么查平台库回填，要么列入人工补。

## 3. 待用户拍板（修订）

1. **分单号来源**：平台是自有短号制（`VCE4373` = 目的站码+流水），票面印的 `BJS001512355` 类号在平台上不存在。V2 的 hawbNo 填什么：让平台生成后回抄？还是我方编号规则？
2. **V2 对接哪条业务线**：国际分单录入（本文档） vs 分单预申报（`/fas/declaration/HAWBDeclarationInputNoAction.do`，海关口径）——字段面不同，需确认主战场。
3. **2c 那 7 类字段**：哪些真实业务会用到？要补进我方契约（40→N 字段）还是 V2 恒留空？

## 4. V2 落地形态（不变）

- `sites/ccsp.json`（本表机械化）+ `web_bridge.py`（人工登录一次 → 复用会话 → 逐票填表 → dry-run 截图 → 人工点发送）。
- 幂等键仍为 `归一化(MAWB)|归一化(HAWB)`；台账 `submitted.json` 增 `web` 段记回执与证据（截图、回执文本）。
- 门禁：`.env WEB_SUBMIT=on` + 每票人工确认 + 零红旗 + 白名单票；出错停批不重试。
