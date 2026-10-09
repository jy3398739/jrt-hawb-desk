# -*- coding: utf-8 -*-
"""确定性结果校验器（与模型无关，拦截 VLM 偶发错误）。
返回警告列表；警告不阻断输出，但批处理结束会汇总，人工/二次提问复核。
"""
import re

from codes import TEL_COUNTRY_CODES, TAX_LABEL_RE, TAX_PREFIX_RE, clean_tax
from master_fields import MASTER_LIMIT50

def _alnum_upper(v) -> str:
    """税号/电话比对用：去掉所有分隔符再比，票面 00.280.273/0001-37 与 00280273000137 是同一个号。"""
    return re.sub(r"[^0-9A-Za-z]", "", str(v or "")).upper()


def validate_raw(d: dict) -> list:
    """L2 原文口径校验：票面印什么 L2 就该照抄什么，所以这里只拦"绝不可能是票面值"的错误。
    城市名（票面 To3 格印 MANAUS）在 L2 属正常，三字码强制检查在 validate_air(L3) 做。"""
    warns = []

    # 1) 航路栏填了 2 字母承运人码(JL/LH)或含数字的航班号(CA949) —— 一定不是 To 栏票面值
    for k in ("TO1", "TO2", "TO3"):
        v = str(d.get(k, "")).strip()
        if re.fullmatch(r"[A-Za-z]{2}", v):
            warns.append(f"{k} 疑似填成承运人码: {v!r}")
        elif re.fullmatch(r"[A-Za-z]{2}\d{3,4}", v):
            warns.append(f"{k} 疑似填成航班号: {v!r}")

    # 1b) TO1 留空：本票库 18 票无一路出栏为空，且它与目的站同源——模型只填 DEST_NAME
    #     把第一跳留空是静默漏抄，正向保真查不到（空值不参与），只能靠这条结构判据出声
    dest = str(d.get("DEST_NAME", "")).strip()
    if not str(d.get("TO1", "")).strip() and dest:
        warns.append(f"TO1 疑似漏抄：目的站 {dest!r} 已取到而航路第一跳栏为空，需核票补 TO1")

    # 2) 主/分单号两栏独立，缺哪个都单独告警（历史 31 单 MAWB 全有、2 单票面确无 HAWB，
    #    但空缺更常见于两格分印时漏抄其中一格——如 MAWB_NO 有 fidelity 的反向核查兜底）
    mawb, hawb = str(d.get("MAWB_NO", "")).strip(), str(d.get("HAWB_NO", "")).strip()
    if not mawb and not hawb:
        warns.append("MAWB_NO 与 HAWB_NO 均为空")
    else:
        if not mawb:
            warns.append("MAWB_NO 缺失（主单号常与分单号分两格印，需核票确认票面是否有）")
        if not hawb:
            warns.append("HAWB_NO 缺失（需核票确认票面是否有）")
    if re.match(r"^(999|020|131|074|016|057|490)\D?\d{7,8}", hawb.replace(" ", "")):
        warns.append(f"HAWB_NO 疑似误填主单号: {hawb!r}")

    # 3) 件重（缺失才告警；单件均重差异大（服装 vs 工业设备），不做阈值判断，
    #    重量防翻倍由 ask_vision.py 对 Gross Weight 栏专项复核兜底）
    pieces, weight = d.get("PIECES"), d.get("WEIGHT")
    if pieces in (None, "", 0):
        warns.append("PIECES 缺失或为0")
    if weight in (None, "", 0):
        warns.append("WEIGHT 缺失或为0")

    # 4) 日期
    ct = str(d.get("CREATE_TIME", ""))
    if not ct:
        warns.append("CREATE_TIME 签发日期缺失（注意勿取航班日期）")
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ct):
        warns.append(f"CREATE_TIME 格式非 YYYY-MM-DD: {ct!r}")

    # 5) 电话国家码与收发货人国家一致性
    for pfx, cfield, tfield in (
        ("SHIPPER", "SHIPPER_INFO_COUNTRY", "SHIPPER_INFO_TEL"),
        ("CONSIGNEE", "CONSIGNEE_INFO_COUNTRY", "CONSIGNEE_INFO_TEL"),
    ):
        tel = re.sub(r"\D", "", str(d.get(tfield, "")))
        country = str(d.get(cfield, "")).strip().upper()
        if tel.startswith("00"):
            tel = tel[2:]
        if tel and country and len(country) == 2:
            for cc, iso in sorted(TEL_COUNTRY_CODES.items(), key=lambda x: -len(x[0])):
                if tel.startswith(cc):
                    if iso != country:
                        warns.append(f"{pfx} 电话区号 +{cc}({iso}) 与国家 {country} 不一致，疑似电话串边")
                    break

    # 6) EORI 形态粗检（2字母+多位字母数字）
    for k in ("SHIPPER_INFO_EORI", "CONSIGNEE_INFO_EORI"):
        v = str(d.get(k, "")).strip()
        if v and not re.fullmatch(r"[A-Za-z]{2}[A-Za-z0-9]{6,15}", v):
            warns.append(f"{k} 形态异常: {v!r}")

    # 7) 税号：长度对不上多半是被截断，与电话同值必是串栏
    for pfx in ("SHIPPER", "CONSIGNEE"):
        k = pfx + "_INFO_TAX_ID"
        v = _alnum_upper(d.get(k, ""))
        if not v:
            continue
        country = str(d.get(pfx + "_INFO_COUNTRY", "")).strip().upper()
        tel = _alnum_upper(d.get(pfx + "_INFO_TEL", ""))
        if v == tel:
            warns.append(f"{k} 与电话同值，疑似电话栏吞了税号")
        elif TAX_PREFIX_RE.match(str(d.get(k, "")).strip()):
            # 票面标签被连着号码一起抄进来了，L3 会自动摘掉；这里报出来是让复核的人看得见
            warns.append(f"{k} 混进了标签，只填号码本身（航空口径已自动摘掉）: {str(d.get(k))!r}")
        elif country in ("CN", "CHINA", "中国") and len(v) != 18:
            warns.append(f"{k} 中国 USCI 应为 18 位，现 {len(v)} 位: {str(d.get(k))!r}")
        elif len(v) < 8:
            warns.append(f"{k} 形态异常(过短): {str(d.get(k))!r}")

    return warns


def validate_air(d: dict) -> list:
    """L3 航空口径校验：城市名必须已被映射成 IATA 三字码。"""
    warns = []
    for k in ("TO1", "TO2", "TO3"):
        v = str(d.get(k, "")).strip()
        if v and not re.fullmatch(r"[A-Z]{3}", v):
            warns.append(f"{k} 未转成 IATA 三字码: {v!r}（码表缺项，需补 CITY_IATA）")
    for k in ("ORIGIN_NAME", "DEST_NAME"):
        v = str(d.get(k, "")).strip()
        if v and not re.fullmatch(r"[A-Z]{3}", v):
            warns.append(f"{k} 未转成 IATA 三字码: {v!r}（码表缺项，需补 CITY_IATA）")
    return warns


# ── 主单（MAWB）侧：公司 AMS_RECORD 是另一套列面，所以是另一张判据表 ──────────────
# 判据与分单同源（区号↔国家、EORI 形态、单号格式、限长），只是作用在公司列名上。
_MASTER_GROUPS = (("SHIPPER_INFO_COUNTRY", "SHIPPER_INFO_TEL"),
                  ("CONSIGNEE_INFO_COUNTRY", "CONSIGNEE_INFO_TEL"),
                  ("NOTIFY_INFO_COUNTRY", "NOTIFY_INFO_TEL"))
_MASTER_EORI = ("SHIPPER_INFO_EORI", "CONSIGNEE_INFO_EORI", "NOTIFY_INFO_EORI")
_MAWB_HARD = re.compile(r"^\d{3}-\d{8}$")


# EORI 这一格现在什么都往里放（税号统一落点，IT 2026-10-09），形态判据退一步只守"像不像号码"：
# 一格可写多个号（" / " 分隔），每段去掉标点后要匹配 EORI/VAT、纯数字税号（11-18 位）或 18 位 USCI。
# 位数不再当判据——列面放宽后 15 位、17 位都可能是某个国家的正经税号，按位数拦会把对的挡掉。
_EORI_SEG = re.compile(r"^(?:[A-Z]{2}[A-Z0-9]{6,15}|\d{11,18}|[0-9A-Z]{18})$")


def _eori_cell_ok(value: str) -> bool:
    segs = [re.sub(r"[^A-Z0-9]", "", s) for s in str(value).upper().split()]
    segs = [s for s in segs if s]
    return bool(segs) and all(_EORI_SEG.match(s) for s in segs)


def validate_master(ams: dict, transcript: dict | None = None) -> list:
    """主单可提交列的红旗。

    主单表**没有税号列**：按公司口径（2026-10-09 IT 定案）所有税号统一放同主体的 EORI 列，
    一个主体有多个号（EORI 与 VAT 并存那种）就用 " / " 拼在同一格，两个都留。
    限长 50 那九列是公司文档写死的 400 硬线，提前报出来比让人撞接口强。"""
    d = ams or {}
    warns: list[str] = []

    mawb = str(d.get("MAWB_NO", "") or "").strip()
    if not mawb:
        warns.append("MAWB_NO 缺失（主单号是提交定位键）")
    elif not _MAWB_HARD.match(mawb):
        warns.append(f"MAWB_NO 格式必须为 3位-横杠-8位（如 176-62400004）: {mawb!r}，公司会直接 400")

    slac = d.get("SLAC")
    if isinstance(slac, str) and slac.strip():
        try:
            int(float(slac.strip()))
        except ValueError:
            warns.append(f"SLAC 必须为整数: {slac!r}")

    hs = str(d.get("GOODS_INFO_HSCODE", "") or "").strip()
    if hs and not re.fullmatch(r"\d{4,}(?:\s*,\s*\d{4,})*", hs):
        warns.append(f"GOODS_INFO_HSCODE 只填号码本身（多个用逗号），现 {hs!r}"
                     "（清洗已摘过一次标签，还在多半是模型给了别的东西，需核）")

    for col in MASTER_LIMIT50:
        v = str(d.get(col, "") or "")
        if len(v) > 50:
            warns.append(f"{col} 长度不能超过 50 字符（当前 {len(v)}），公司会直接 400")

    for cfield, tfield in _MASTER_GROUPS:
        tel = re.sub(r"\D", "", str(d.get(tfield, "") or ""))
        country = str(d.get(cfield, "") or "").strip().upper()
        if tel.startswith("00"):
            tel = tel[2:]
        if tel and len(country) == 2:
            for cc, iso in sorted(TEL_COUNTRY_CODES.items(), key=lambda x: -len(x[0])):
                if tel.startswith(cc):
                    if iso != country:
                        warns.append(f"{tfield} 电话区号 +{cc}({iso}) 与 {cfield} {country} 不一致，疑似电话串边")
                    break

    for k in _MASTER_EORI:
        v = str(d.get(k, "") or "").strip()
        if v and not _eori_cell_ok(v):
            warns.append(f"{k} 形态异常: {v!r}（这一格放税号：两位国家字母开头的号码、"
                         "纯数字税号或 18 位 USCI；多个号用 / 隔开，别写标签词或整句话）")

    if transcript:
        text = transcript.get("full_text") or " ".join(
            str(x.get("text", "")) for x in transcript.get("lines", []))
        have = " ".join(str(v) for v in d.values() if isinstance(v, str))
        seen = set()
        for m in TAX_LABEL_RE.finditer(text):
            label, num = str(m.group(1)), clean_tax(m.group(2))
            if label.upper().startswith("EORI") or not num or num in seen or num in have:
                continue
            seen.add(num)
            warns.append(f"资料里有税号 {label} {num}，主单表没有税号列：填进同主体的 EORI 列，"
                         "那一格已有号就用 / 拼在一起（两个都留）")
    return warns
