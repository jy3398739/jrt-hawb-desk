# -*- coding: utf-8 -*-
"""保真校验：L2 结构化字段值必须能在 L1 逐字转录文本中找到原文出处。
命中 → 记录出处行号（可在原票按 bbox 高亮）；不命中 → 告警（值被加工/模型脑补）。
"""
import re

import hawb2json as h

# 需要回查原文的文本字段（INFO 合并串是系统拼接、数值/日期是类型转换，不参与）
LONG_FIELDS = ["SHIPPER_INFO_COMP_NAME", "SHIPPER_INFO_COMP_ADDRESS",
               "CONSIGNEE_INFO_COMP_NAME", "CONSIGNEE_INFO_COMP_ADDRESS", "GOODS_INFO"]
SHORT_FIELDS = ["HAWB_NO", "ORIGIN_NAME", "TO1", "TO2", "TO3", "DEST_NAME",
                "SHIPPER_INFO_CITY", "SHIPPER_INFO_COUNTRY", "SHIPPER_INFO_STATE",
                "SHIPPER_INFO_POSTAL", "SHIPPER_INFO_TEL", "SHIPPER_INFO_FAX",
                "SHIPPER_INFO_EORI", "SHIPPER_INFO_AEO", "SHIPPER_INFO_EMAIL",
                "SHIPPER_INFO_TAX_ID",
                "CONSIGNEE_INFO_CITTY", "CONSIGNEE_INFO_COUNTRY", "CONSIGNEE_INFO_STATE",
                "CONSIGNEE_INFO_POSTAL", "CONSIGNEE_INFO_TEL", "CONSIGNEE_INFO_FAX",
                "CONSIGNEE_INFO_EORI", "CONSIGNEE_INFO_AEO", "CONSIGNEE_INFO_EMAIL",
                "CONSIGNEE_INFO_TAX_ID"]

_FW = {ord("，"): ",", ord("："): ":", ord("；"): ";", ord("（"): "(", ord("）"): ")",
       ord("　"): " "}


def _norm(s: str) -> str:
    """大小写敏感的宽松空白归一（全角标点转半角、所有空白压单空格）。
    保留大小写——L2 若把 Italy 改成 ITALY 必须能被抓出来。"""
    return re.sub(r"\s+", " ", str(s or "").translate(_FW)).strip()


def _alnum(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(s or ""))


def _find_lines(nval: str, lines: list[str]) -> list[int]:
    """返回包含该值的转录行号集合（跨行拼接也算命中，覆盖行号全记录）。"""
    hits = []
    for i, lt in enumerate(lines, 1):
        if nval and nval in lt:
            hits.append(i)
    if hits:
        return hits
    # 跨行情形：值跨相邻2-4行（如公司名+地址被分行），滑窗拼接检测
    for span in range(2, 5):
        for i in range(len(lines) - span + 1):
            joined = _norm(" ".join(lines[i:i + span]))
            if nval and nval in joined:
                return list(range(i + 1, i + span + 1))
    return []


def _tokens(s: str) -> list[str]:
    """词元：字母数字串（含 & 等连词保留）；数字段单独成词。"""
    return [t for t in re.split(r"[^0-9A-Za-z&]+", str(s)) if t]


def _coverage_ok(nval: str, lines: list[str]) -> tuple[bool, list[int]]:
    """长字段（地址/公司名/货描）保真：票面本就多行多区块排版，字段值把它们压成单行。
    判据：① 所有数字词元 100% 在票面出现；② 字母词元(>=3字符)覆盖率>=95%；
          ③ 首词、尾词在全文中出现顺序单调。返回(是否通过, 涉及行号)。"""
    target = _tokens(nval)
    if not target:
        return False, []
    num_tokens = [t for t in target if re.search(r"\d", t)]
    word_tokens = [t for t in target if len(t) >= 3 and not re.search(r"\d", t)]
    nfull_low = _norm(" ".join(lines)).upper()
    # ① 数字词元（门牌号/邮编/电话段）必须全部命中
    for t in num_tokens:
        if t.upper() not in nfull_low:
            return False, []
    # ② 字母词元覆盖率
    missing = [t for t in word_tokens if t.upper() not in nfull_low]
    if word_tokens and len(missing) / len(word_tokens) > 0.05:
        return False, []
    if not word_tokens and not num_tokens:
        return False, []
    # ③ 首尾顺序
    first, last = target[0].upper(), target[-1].upper()
    p1, p2 = nfull_low.find(first), nfull_low.rfind(last)
    if p1 < 0 or p2 < 0 or p1 > p2:
        return False, []
    # 收集涉及行
    hit_lines = []
    for i, lt in enumerate(lines, 1):
        lu = lt.upper()
        if any(t.upper() in lu for t in target[:50]):
            hit_lines.append(i)
    return True, hit_lines


_AWB_SHAPE = re.compile(r"(?<!\d)(\d{3})[-\s]?(\d{8})(?!\d)")


def _mawb_check_digit_ok(serial: str) -> bool:
    """IATA 主单号校验位：8 位序号的前 7 位 mod 7 == 第 8 位。
    主单号形态（3位-8位连写）会被 11 位电话号码撞上，靠校验位过滤掉。"""
    return int(serial[:7]) % 7 == int(serial[7])


# 票面 HS 码标签形态（2026-09-26 对格实测）：`HSCODE: 89031200` / `HS:7007290000` /
# `HS Codes: 85389000` / `HS code : 8505.11`。要求后面紧跟冒号，避免把普通行里的 HS 字样当标签。
_HS_LABEL_RE = re.compile(r"\bHS\s*(?:CODES?\s*)?[:：]", re.I)


def find_missing_tax(raw: dict, transcript: dict) -> list[tuple]:
    """反向核查税号漏抄：票面印着带标签的税号（USCI/CNPJ/VAT NO/TAX ID…），
    L2 三十九个字段里却一处都找不到。返回 [(标签, 号码)]。税号早先没有落点，模型按
    "不是电话不是 EORI"的规则直接丢弃，正向保真（L2⊆L1）永远抓不到这种"少抄"。"""
    text = _norm(transcript.get("full_text") or
                 " ".join(x["text"] for x in transcript.get("lines", [])))
    captured = _alnum(" ".join(str(v) for v in raw.values() if v))
    out = []
    for m in h.TAX_LABEL_RE.finditer(text):
        label, val = re.sub(r"\s+", " ", m.group(1).upper()).strip(), h.clean_tax(m.group(2))
        if len(_alnum(val)) < 8 or _alnum(val) in captured:
            continue
        out.append((label, val))
    return list(dict.fromkeys(out))


def find_missing_hs(raw: dict, transcript: dict, field: str = "GOODS_HS_CODE") -> list[str]:
    """反向核查 HS 码漏抄：票面印着带 HS 标签的码（`HSCODE: 89031200` / `HS Codes: 85389000` / `HS:850152`），
    GOODS_HS_CODE 却没接住。按前 6 位归一比对——平台/国际口径只填 6 位，也算已捕获。
    无标签的裸数字形态（EDC `84742708 (4),84647039 (24)`）明确不覆盖：裸 6-10 位可能是电话/邮编/货值，
    拿它挂旗误报率不可控。CCSP 八票对格（2026-09-26）坐实 S2/MiMo 各漏过一例且旗全哑。"""
    mine = re.sub(r"\D", "", str(raw.get(field, "") or ""))
    out = []
    for x in transcript.get("lines", []):
        t = str(x.get("text", "") if isinstance(x, dict) else x)
        m = _HS_LABEL_RE.search(t)
        if not m:
            continue
        for run in re.findall(r"\d{6,10}", t[m.end():]):
            if run[:6] not in mine:
                out.append(run)
    return list(dict.fromkeys(out))


def find_missing_mawb(raw: dict, transcript: dict) -> list[str]:
    """反向核查：L1 转录里出现、L2 却没取走的主单号。
    正向保真只能证明"L2 抄的都有出处"，证明不了"票面有的都抄了"——漏抄全靠这条。"""
    text = _norm(transcript.get("full_text") or
                 " ".join(x["text"] for x in transcript.get("lines", [])))
    captured = re.sub(r"\D", "", " ".join(str(v) for v in raw.values() if isinstance(v, str)))
    captured += re.sub(r"\D", "", str(raw.get("PIECES", ""))) + re.sub(r"\D", "", str(raw.get("WEIGHT", "")))
    out = []
    for m in _AWB_SHAPE.finditer(text):
        digits = m.group(1) + m.group(2)
        if digits in captured or not _mawb_check_digit_ok(m.group(2)):
            continue
        out.append(f"{m.group(1)}-{m.group(2)}")
    return list(dict.fromkeys(out))


_MON = {a: i for i, a in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _iso_parts(v) -> tuple | None:
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", str(v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _year(y: str) -> int:
    y = int(y)
    return 2000 + y if y < 100 else y


def date_candidates(text: str) -> set:
    """从 L1 转录里解析出所有能读出来的日期，返回 {(年,月,日)}；读不出年份的用 (None,月,日)。
    票面形态五花八门：2026-09-20 / 20-Sep-26 / 24SEP / Sep 21, 2026 / 26/09/2026 / 19-9月-26。
    判据只要求 L2 的签发日期能对上任一个（无年份的再核对年份确实出现在票面），
    候选一律取宽（日月颠倒两种都收、缺年份也算）——误报会让复核的人直接无视这条红旗。"""
    cands, up = set(), text.upper()

    def add(y, mo, d):
        try:
            mo, d = int(mo), int(d)
        except ValueError:
            return
        y = None if y in (None, "") else _year(str(y))
        if (y is None or 1990 <= y <= 2100) and 1 <= mo <= 12 and 1 <= d <= 31:
            cands.add((y, mo, d))

    for m in re.finditer(r"\b(\d{4})[-/. ](\d{1,2})[-/. ](\d{1,2})\b", up):     # 2026-09-20
        add(*m.groups())
    for m in re.finditer(r"\b(\d{1,2})[-.\s]?([A-Z]{3})[A-Z]*\.?[-.\s]?(\d{2}|\d{4})?\b", up):
        if m.group(2) in _MON:                                                   # 20-Sep-26 / 24SEP
            add(m.group(3), _MON[m.group(2)], m.group(1))
    for m in re.finditer(r"\b([A-Z]{3})[A-Z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", up):
        if m.group(1) in _MON:                                                   # Sep 21, 2026
            add(m.group(3), _MON[m.group(1)], m.group(2))
    for m in re.finditer(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", up):        # 26/09/2026 与美式两种
        a, b, y = m.groups()
        add(y, a, b)
        add(y, b, a)
    for m in re.finditer(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text):
        add(*m.groups())
    for m in re.finditer(r"(\d{1,2})[-/.]?(\d{1,2})月[-/.]?(\d{2,4})日?", text):  # 19-9月-26
        add(m.group(3), m.group(2), m.group(1))
    for m in re.finditer(r"(\d{1,2})月(\d{1,2})日", text):                        # 9月16日（无年份）
        add(None, m.group(1), m.group(2))
    for m in re.finditer(r"\b(\d{4})(\d{2})(\d{2})\b", up):                      # 20260920
        add(*m.groups())
    return cands


def _date_ok(ct: tuple, cands: set, text: str) -> bool:
    if ct in cands:
        return True
    y, mo, d = ct
    return (None, mo, d) in cands and str(y) in text


def verify_fidelity(raw: dict, transcript: dict, short: list | None = None,
                    long: list | None = None, hs_field: str = "GOODS_HS_CODE",
                    tax: bool = True) -> dict:
    """L2 字段回查 L1 逐字转录原文（大小写敏感、逐字），并反向查票面主单号/税号是否漏抄。
    扫描件 L1 来自 VLM 转录，电子单 L1 来自 Excel 导出的 PDF 文字层——两者都是票面逐字真值。
    short/long/hs_field/tax 是给主单侧复用时用的：主单是另一套列面（36 列、HS 叫
    GOODS_INFO_HSCODE、压根没有税号列），默认值就是分单口径，分单调用方一个字都不用改。
    返回 {checked, passed, failed, sources}。"""
    lines = [x["text"] for x in transcript.get("lines", [])]
    nlines = [_norm(t) for t in lines]
    nfull = _norm(transcript.get("full_text") or " ".join(lines))
    sources, failed, checked, passed = {}, [], 0, 0

    def check(field: str, kind: str):
        nonlocal checked, passed
        val = str(raw.get(field, "") or "").strip()
        if not val:
            return  # 空值不参与保真校验（由 validator 管缺失）
        checked += 1
        if kind == "mawb":
            # 3位前缀+8位号码分别能找到即可（电子单两格分储；扫描件同格也成立）
            digits = re.sub(r"\D", "", val)
            full_digits = re.sub(r"\D", "", nfull)
            ok = (len(digits) == 11 and digits in full_digits) or (
                len(digits) == 11 and digits[:3] in full_digits and digits[3:] in full_digits)
        elif kind == "alnum":
            target = _alnum(val)
            ok = bool(target) and any(target and target in _alnum(t) for t in nlines)
            if not ok and len(target) >= 6:  # 号码跨行
                ok = target in _alnum(nfull)
        else:
            nval = _norm(val)
            hit = _find_lines(nval, nlines)
            ok = bool(hit) or (nval in nfull)
            if ok:
                sources[field] = hit or None
            elif kind == "long":
                # 长字段（地址/公司名/货描）票面多行排版：严格连续失败时用词元覆盖率兜底
                ok2, hit2 = _coverage_ok(nval, nlines)
                ok = ok2
                if ok:
                    sources[field] = hit2 or None
        if ok:
            passed += 1
        else:
            failed.append({"field": field, "value": val[:80],
                           "reason": "未在L1逐字转录中找到原文（可能被改写或转录遗漏）"})

    check("MAWB_NO", "mawb")
    for f in (SHORT_FIELDS if short is None else short):
        check(f, "alnum" if f.endswith(("_TEL", "_FAX", "_EORI", "_POSTAL", "_TAX_ID"))
              or f in ("HAWB_NO",) else "text")
    for f in (LONG_FIELDS if long is None else long):
        check(f, "long")

    # 签发日期是唯一允许格式转换的字段，格式转换最容易把 20 抄成 26、把航班日期当签发日期
    ct = _iso_parts(raw.get("CREATE_TIME"))
    if ct:
        cands = date_candidates(nfull)
        # 票面一个带年份的日期都读不出、且年份也没单独出现时，无从回查：不报也不算通过
        verifiable = bool(cands) and (any(y for y, _m, _d in cands) or str(ct[0]) in nfull)
        if verifiable:
            checked += 1
            if _date_ok(ct, cands, nfull):
                passed += 1
            else:
                failed.append({"field": "CREATE_TIME", "value": str(raw.get("CREATE_TIME")),
                               "reason": "签发日期与 L1 转录里任何可解析日期都不符（可能取成航班日期或抄错）"})

    for cand in find_missing_mawb(raw, transcript):
        checked += 1
        failed.append({"field": "MAWB_NO", "value": cand,
                       "reason": "L1 转录里有、L2 没取的主单号（漏抄，需核票确认落点）"})

    if tax:
        for label, val in find_missing_tax(raw, transcript):
            checked += 1
            failed.append({"field": "TAX_ID", "value": val,
                           "reason": f"{label} 税号漏抄：票面有 {val}，"
                                     f"需核票填 SHIPPER_INFO_TAX_ID 或 CONSIGNEE_INFO_TAX_ID"})

    for cand in find_missing_hs(raw, transcript, field=hs_field):
        checked += 1
        failed.append({"field": hs_field, "value": cand,
                       "reason": f"HS 码漏抄：票面印着带标签的 HS 码 {cand}，{hs_field} 空或不包含，需核票补"})

    return {"checked": checked, "passed": passed,
            "failed": failed, "sources": sources}
