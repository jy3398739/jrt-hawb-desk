# -*- coding: utf-8 -*-
"""第一遍原文口径 JSON -> 第二遍航空口径 JSON。
复用 codes.py 的 parse_party / COUNTRY_ISO2 / iata_place / origin_place / id_cell。
发货人/收货人的 *_INFO 整串不在归一之列：那一列只许是票面那一格的照抄串（2026-10-14 业主定案）。
"""
import re
import codes

_ISO2 = set(codes.COUNTRY_ISO2.values())
_ISO2_FULL = {v: k for k, v in codes.COUNTRY_ISO2.items()}

_EXTRA_COUNTRY = {
    "REPUBLIC OF IRELAND": "IE",
    "THE NETHERLANDS": "NL",
    "CZECH REP.": "CZ",
}
_ALL_ISO2 = _ISO2 | set(_EXTRA_COUNTRY.values())

# 票面把国名和国码写在同一格里：'Slovak Republic (SK)'（999-95764815 收货人）。
# 不摘括号则整串当"查不到原样返回"，发给公司的 *_INFO_COUNTRY 那一格还是个短语，
# 而公司按两字母码校验。（整串自 v1.24 起不再由代码重拼，括号残片不会再出现在那一列。）
_PAREN_ISO2 = re.compile(r"^(.*?)[,\s]*\(([A-Z]{2})\)$")


def _split_paren(u: str) -> tuple:
    """'SLOVAK REPUBLIC (SK)' -> ('SLOVAK REPUBLIC', 'SK')；不是这种写法则括号码为空。"""
    m = _PAREN_ISO2.match(u)
    return (m.group(1).strip(" ,"), m.group(2)) if m else (u, "")


def country_to_iso2(c: str) -> str:
    """L2 国家写法混杂（'Italy'/'cn'/'CN'/'CHINA'）统一为 ISO2；查不到原样大写返回。"""
    u = str(c or "").strip().upper()
    if not u:
        return ""
    if len(u) == 2 and u in _ALL_ISO2:
        return u
    name, code = _split_paren(u)
    return codes.COUNTRY_ISO2.get(name) or _EXTRA_COUNTRY.get(name) or code or name or u


def country_full(c: str) -> str:
    """L2 两字母码反查全称（供 _parse_party/地址剥离使用）；本身是全称则原样返回。"""
    u = str(c or "").strip().upper()
    if len(u) == 2:
        return _ISO2_FULL.get(u, "")
    name, code = _split_paren(u)
    return _ISO2_FULL.get(name) or name or code


def _first_phone(t: str) -> str:
    """多个号码(/ 或 ; 分隔, 如直线/手机)只取第一个"""
    return re.split(r"\s*[/;]\s*", str(t or ""))[0].strip()


# 号码本体：数字与分隔符连写；PDF 文字层的排版连字符 U+2010-2015、减号 U+2212 不算号码断点
_NUMCLS = r"\d\s\-()/.‐-―−­"
_PHONE_TOKEN = re.compile(r"\+?[\d][" + _NUMCLS + r"]{4,}\d")
_PHONE_LEAD = re.compile(r"^(?:TE|TEL|LTE|FAX|PHONE|MOB|CELL)\b[:.\s]*\+?[\d][" + _NUMCLS + r"]{3,}\d", re.I)


def _clean_phone(val: str) -> str:
    """从可能混排人名/税号的票面串里取出号码本体。
    票面常把 'TE +559240091129 Carla Os CNPJ: 00280273000137' 挤在同一格，
    L2 按原文口径整行照抄（那是对的），L3 归一化前必须先截到号码本身，
    否则剥掉字母后会把人名和税号拼成 26 位的怪物号码。"""
    # 先去干线 0：欧标写法 '+46 (0)70 31 76 762' 的 (0) 不是号码的一部分
    s = re.sub(r"\(0\)", "", str(val or "")).strip()
    if not s:
        return ""
    m = _PHONE_TOKEN.search(s)
    if not m:
        return ""
    # 票面有 + 就留着，没有就不给加：从前这里按已知区号替票面补一个 +（Excel 前导 ' 把冠码吃掉那一档），
    # 2026-10-23 业主定案取消——那个 + 是我们造的，不是抄的。
    return re.sub(r"[^+\d]", "", m.group(0))




def _drop_phone_segs(street: str) -> str:
    """票面常把电话印在地址框内（'TE +551156446468 CNPJ: 00280273000218'）。L2 按原文口径整格
    照抄是对的，但 L3 电话已单独成字段，故只把号码部分从街道里剔掉，同一格里的税号等其余内容保留。"""
    kept = []
    for seg in street.split(","):
        s = seg.strip()
        if not s:
            continue
        m = _PHONE_LEAD.match(s)
        if m:
            rest = s[m.end():].strip(" ,:;")
            if sum(c.isalpha() for c in rest) < 3:   # 剥掉号码后就没内容了，整段丢弃
                continue
            s = rest
        kept.append(s)
    return ", ".join(kept)


def _fix_addr_country(addr: str, country0: str) -> str:
    """L2 地址尾部两字母 ISO 码(如 '066200 CN')替换为国家全称,
    避免被 _parse_party 误判为城市。大小写不敏感。"""
    m = re.search(r"[,\s]+([A-Za-z]{2})\s*$", addr or "")
    if m and m.group(1).upper() in _ALL_ISO2:
        repl = country_full(country0) or ""
        return addr[:m.start()] + ((" " + repl) if repl else "")
    return addr


def _no_comma_split(addr: str, city: str, state: str, country: str, postal: str) -> str:
    """无逗号长串地址: 从尾部反复剥离 邮编/国家/州/城市(含 CITY 标签), 返回街道部分。
    不走 _parse_party——无逗号时它会整行误判 locality 导致地址重复。"""
    toks = addr.split()
    vals = [v for v in (postal, country, state, city) if v]

    def strip_span(v: str) -> bool:
        vt = v.split()
        n = len(vt)
        if n and len(toks) >= n and [t.upper().strip(",-") for t in toks[-n:]] == [x.upper() for x in vt]:
            start = len(toks) - n
            if (start > 0 and city and v.strip().upper() == city.strip().upper()
                    and toks[start - 1].upper().strip(",-") in ("CITY", "CITT")):
                start -= 1
            del toks[start:]
            return True
        return False

    changed = True
    while changed and toks:
        changed = False
        for v in vals:
            if strip_span(v):
                changed = True
                break
    return re.sub(r"\s{2,}", " ", " ".join(toks)).strip(" ,")


def _alnum0(v) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(v or "")).upper()


def _pull_tax(addr: str, tax: str, taken: set) -> tuple:
    """地址串里嵌着带标签的税号（USCI/CNPJ/VAT NO…）时摘出来单独成列。
    只认标签，裸数字一律不动；号码已在电话/邮编等列里出现过则不再重复建列。"""
    m = codes.TAX_LABEL_RE.search(addr or "")
    if not m:
        return addr, tax
    cleaned = re.sub(r"\s{2,}", " ", addr[:m.start()] + " " + addr[m.end():]).strip(" ,;/")
    val = codes.id_cell(m.group(0))
    # 去重按号码本体比：发出值现在带标签词，拿整串比会漏判（'CNPJ559240091129' 对不上电话那格）
    return cleaned, tax or ("" if _alnum0(codes.clean_tax(val)) in taken else val)


def to_air(d: dict) -> dict:
    """第一遍 dict -> 第二遍 dict（全字段，航空口径）。"""
    out = dict(d)
    out.pop("_error", None)
    out.pop("_file", None)

    out["ORIGIN_NAME"] = codes.origin_place(d.get("ORIGIN_NAME", ""))
    out["DEST_NAME"] = codes.iata_place(d.get("DEST_NAME", ""))
    # 航路：L2 可能照抄票面城市名（To3 格印 MANAUS），L3 必须落到三字码；已是三字码则原样
    for k in ("TO1", "TO2", "TO3"):
        out[k] = codes.iata_place(d.get(k, ""))
    # L3 航空口径：主单号 3位-8位连写（L2 可能照抄票面带 4-4 空格）
    out["MAWB_NO"] = codes.fmt_mawb(d.get("MAWB_NO", ""))

    for pfx in ("SHIPPER", "CONSIGNEE"):
        name = d.get(pfx + "_INFO_COMP_NAME", "")
        addr = d.get(pfx + "_INFO_COMP_ADDRESS", "")
        tel0 = d.get(pfx + "_INFO_TEL", "")
        fax0 = d.get(pfx + "_INFO_FAX", "")
        eori0 = d.get(pfx + "_INFO_EORI", "")
        city_key = pfx + "_INFO_CITY" if pfx == "SHIPPER" else pfx + "_INFO_CITTY"
        city0 = d.get(city_key, "")
        country0 = d.get(pfx + "_INFO_COUNTRY", "")
        # 地址剥离/解析用国家全称（地址尾部两字母码已被 _fix_addr_country 换成全称）
        country_full0 = country_full(country0) or str(country0 or "")

        # 识别号只有一个格子：EORI。海关号（SE5563646560）、中国 USCI、巴西 CNPJ、VAT/GST/TAX ID
        # 全往这里并（2026-10-10 定案，`*_INFO_TAX_ID` 两列已删）。模型三种毛病都要兜住：
        # 连着标签一起抄（"VAT#769638661"）、把号留在地址或整串里、同一主体给两个号。
        # 同一个号不许在同一格里出现两遍——所以已知的 EORI 段也算 taken，地址里再出现只清列不重拼。
        ids = [codes.id_cell(x) for x in codes.split_ids(eori0)]
        legacy = codes.id_cell(d.get(pfx + "_INFO_TAX_ID", ""))   # 老 raw 重算时还带着这一列
        if legacy:
            ids.append(legacy)
        taken = {_alnum0(codes.clean_tax(x))
                 for x in (tel0, fax0, d.get(pfx + "_INFO_POSTAL", "")) + tuple(ids)}
        addr, pulled = _pull_tax(addr, "", taken)
        if not pulled:      # 只留在整串里的号（BJS00032108 发货人）：整串兜底，否则 L3 会把它弄丢
            _, pulled = _pull_tax(d.get(pfx + "_INFO", ""), "", taken)
        if pulled:
            ids.append(pulled)
        out[pfx + "_INFO_EORI"] = codes.merge_ids(*ids)

        if not (name or addr):
            continue

        addr_fixed = _fix_addr_country(addr, country0)
        state0 = d.get(pfx + "_INFO_STATE", "")
        postal0 = d.get(pfx + "_INFO_POSTAL", "")

        if "," not in addr_fixed:
            street = _no_comma_split(addr_fixed, city0, state0, country_full0, postal0)
            pp = {"name": name, "country": country_full0, "tel": tel0, "fax": fax0, "eori": eori0}
        else:
            p = {"name": name, "address": addr_fixed.replace(", ", "\n"),
                 "tel": tel0, "fax": fax0}
            if eori0:
                p["tax_id"] = eori0
            pp = codes.parse_party(p)
            street = pp["street"] or addr_fixed

        street = _drop_phone_segs(street)
        country2 = country_to_iso2(country0 or pp["country"])

        tel = _clean_phone(_first_phone(tel0) or pp["tel"])
        fax = _clean_phone(_first_phone(fax0) or pp["fax"])

        out[pfx + "_INFO_COUNTRY"] = country2
        # 收发货人城市都留票面写法（收货人 2026-10-15、发货人 2026-10-23 业主定案）：公司那一格要的
        # 是城市名，转成三字码等于让人在核对区把 BEIJING/CHENNAI 手动改回去。代价写明白：转码从前
        # 顺手掩盖了城市格里的脏值，以后模型抄成 "BEIJING  CN" 就发 "BEIJING  CN"，核对区靠人眼。
        # 三字码仍只管这三处：到达港 DEST_NAME 与航路 TO1/TO2/TO3；起运港按城市全名发
        # （codes.ORIGIN_CITY_NAME），别把这四列混成一个口径。
        out[city_key] = city0
        out[pfx + "_INFO_COMP_ADDRESS"] = street
        out[pfx + "_INFO_TEL"] = tel
        out[pfx + "_INFO_FAX"] = fax
        if not out.get(pfx + "_INFO_EORI") and pp.get("eori"):
            out[pfx + "_INFO_EORI"] = pp["eori"]
        # 到这里 *_INFO 整串一个字都不动（out 是 d 的副本，L2 抄来什么就是什么）。以前这里按
        # 子字段重拼过一遍，40 份夹具实测三种破坏：同一段街道挂两遍（2_sch_1/5e696c 收货人）、
        # 邮编省名整段丢掉（5_dhl 收货人的 KA 560058）、公司名第二行被挪到地址后面
        # （4_ceva 发货人）——而这一列正是制单员核对时读的那一眼，串一重排就等于抄错票。
        # 整串为空时也不拼：留空会进制单员的空栏确认，拼出来的串谁都不会再看第二眼。

    return {k: out.get(k, "") for k in codes.TARGET_KEYS_OUT}
