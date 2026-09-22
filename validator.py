# -*- coding: utf-8 -*-
"""确定性结果校验器（与模型无关，拦截 VLM 偶发错误）。
返回警告列表；警告不阻断输出，但批处理结束会汇总，人工/二次提问复核。
"""
import re

from hawb2json import TAX_PREFIX_RE

# 电话国际区号 -> ISO2（只收录样本中出现的，按需扩充）
TEL_COUNTRY_CODES = {
    "86": "CN", "46": "SE", "39": "IT", "49": "DE", "36": "HU",
    "358": "FI", "91": "IN", "52": "MX", "55": "BR", "81": "JP",
    "82": "KR", "353": "IE", "41": "CH", "1": "US", "852": "HK",
    "65": "SG", "971": "AE", "90": "TR",
}


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
