# -*- coding: utf-8 -*-
"""逐格「优化」规则表：把这一格的现值换成目的国的规则写法。

调用方是审核台那一格（人先看票面原值、确认值本身没错，再点优化），所以这里的判据只有一条：
**新值必须由现值唯一推出**。推不出来一律返回 None，宁可不动。
据此，本模块**不做**下面这些事（那些只在 validator 那边挂旗，让人自己核票）：
  · HS 编码补零凑位数；
  · 韩国邮编补横杠（新制 5 位、旧制 6 位都还在票面上，横杠位置推不出来）；
  · 德国邦、意大利大区（没有官方两字母码）；
  · 墨西哥 32 个联邦实体的 clave（JL/JAL、MS/MOS、CL 与 DF 几种写法都在流传，
    没拿到可信全表之前不塞进来——表少几格只是不动，表错一格就是静默改数据）；
  · 剥掉邮编前的国别前缀（`SE-114 33` 那个 `SE-` 是内容不是格式）；
  · 整串 `*_INFO`、公司名、详细地址、城市：一个字都不碰（照抄口径与"收发货人城市都留票面写法"
    是定案，见 tests/test_polish.py 里那两条越界守卫）。

`to_air()` 与这里的分工（2026-10-23 对着代码重新核过）：国家转两字母码、电话取首个号码去符号、
起运港归一成城市全名、识别号把票面标签词连在号码前面，都是 `to_air` 在提交时干的；
`POSTAL`/`STATE` 的国别写法归这里，识别号那一格现在两边都不动（从前 to_air 摘标签、
这里补标点，两条都被 2026-10-23 那一档定案撤掉了）。
两边叠起来才是"将要发出去的那一版"，所以 `preview()` 先过 `to_air` 再过本模块，
点优化写回的值 = 真正发出去的值，不会出现"点了还是发另一版"。
"""
import re
import unicodedata

# 可优化格 = 收发货人/通知人这几列的子列。整串与货物列、航路列都不在这张表里。
ACTIONABLE_SUFFIXES = ("POSTAL", "STATE", "COUNTRY", "TEL", "FAX", "EORI", "AEO", "EMAIL")
_PARTY_PREFIXES = ("SHIPPER", "CONSIGNEE", "NOTIFY")

# ── 不可见字符与非 ASCII 连字符（票面排版带来的那族坑） ───────────────────────────
_DROP = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad")           # 零宽、软连字符
_SPACE = {"\u00a0": " ", "\u202f": " ", "\u2007": " ", "\u3000": " "}  # 各种硬空格
_DASH = ("\u2010", "\u2011", "\u2012", "\u2013", "\u2015", "\u2212")   # 排版横杠族，U+2010 最常见


def _clean(value: str) -> str:
    s = str(value or "")
    for ch in _DROP:
        s = s.replace(ch, "")
    for ch in _DASH:
        s = s.replace(ch, "-")
    for ch, rep in _SPACE.items():
        s = s.replace(ch, rep)
    return re.sub(r"\s+", " ", s).strip()


def _bare(value: str) -> str:
    """查表用：摘重音、去标点、大写、压空格。SÃO PAULO 与 Sao Paulo. 都归到 SAO PAULO。"""
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z ]", " ", s)).strip().upper()


# ── 邮编：按国家认出"多少个数字/字母 + 分隔符放在哪儿"，位数不合就不动 ────────────
_POSTAL = {
    "BR": (r"(\d{5})(\d{3})", r"\1-\2", "巴西 CEP 印成 5 位+横杠+3 位"),
    "JP": (r"(\d{3})(\d{4})", r"\1-\2", "日本邮编印成 3 位+横杠+4 位"),
    "PT": (r"(\d{4})(\d{3})", r"\1-\2", "葡萄牙邮编印成 4 位+横杠+3 位"),
    "PL": (r"(\d{2})(\d{3})", r"\1-\2", "波兰邮编印成 2 位+横杠+3 位"),
    "US": (r"(\d{5})(\d{4})", r"\1-\2", "美国 ZIP+4 印成 5 位+横杠+4 位"),
    "SE": (r"(\d{3})(\d{2})", r"\1 \2", "瑞典邮编 3 位+空格+2 位"),
    "GR": (r"(\d{3})(\d{2})", r"\1 \2", "希腊邮编 3 位+空格+2 位"),
    "CZ": (r"(\d{3})(\d{2})", r"\1 \2", "捷克邮编 3 位+空格+2 位"),
    "SK": (r"(\d{3})(\d{2})", r"\1 \2", "斯洛伐克邮编 3 位+空格+2 位"),
    "NL": (r"(\d{4})([A-Z]{2})", r"\1 \2", "荷兰邮编 4 位数字+空格+2 位大写字母"),
    "CA": (r"([A-Z]\d[A-Z])(\d[A-Z]\d)", r"\1 \2", "加拿大邮编 3 位+空格+3 位"),
    "GB": (r"([A-Z]{1,2}\d[A-Z\d]?)(\d[A-Z]{2})", r"\1 \2", "英国邮编分外向码+空格+内向码"),
    "IE": (r"([A-Z0-9]{3})([A-Z0-9]{4})", r"\1 \2", "爱尔兰 Eircode 分路由码+空格+4 位"),
}


def _postal(value: str, cc: str):
    entry = _POSTAL.get(cc)
    if not entry:
        return None
    compact = re.sub(r"[\s\-.,]", "", value).upper()
    m = re.fullmatch(entry[0], compact)
    if not m:
        return None
    to = m.expand(entry[1])
    return (to, "POSTAL", entry[2]) if to != value else None


# ── 州 / 省：只放有官方两字母码、且这张表我有把握的四个国家 ───────────────────────
_STATE = {
    "US": {"ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
           "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
           "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
           "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
           "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
           "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
           "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
           "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
           "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
           "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
           "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
           "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
           "WISCONSIN": "WI", "WYOMING": "WY", "PUERTO RICO": "PR", "GUAM": "GU",
           "VIRGIN ISLANDS": "VI", "AMERICAN SAMOA": "AS", "NORTHERN MARIANA ISLANDS": "MP"},
    "CA": {"ALBERTA": "AB", "BRITISH COLUMBIA": "BC", "MANITOBA": "MB", "NEW BRUNSWICK": "NB",
           "NEWFOUNDLAND": "NL", "NOVA SCOTIA": "NS", "ONTARIO": "ON", "PRINCE EDWARD ISLAND": "PE",
           "QUEBEC": "QC", "SASKATCHEWAN": "SK", "NUNAVUT": "NU", "YUKON": "YT",
           "NORTHWEST TERRITORIES": "NT"},
    "BR": {"ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM", "BAHIA": "BA",
           "CEARA": "CE", "DISTRITO FEDERAL": "DF", "ESPIRITO SANTO": "ES", "GOIAS": "GO",
           "MARANHAO": "MA", "MATO GROSSO": "MT", "MATO GROSSO DO SUL": "MS", "MINAS GERAIS": "MG",
           "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR", "PERNAMBUCO": "PE", "PIAUI": "PI",
           "RIO DE JANEIRO": "RJ", "RIO GRANDE DO NORTE": "RN", "RIO GRANDE DO SUL": "RS",
           "RONDONIA": "RO", "RORAIMA": "RR", "SANTA CATARINA": "SC", "SAO PAULO": "SP",
           "SERGIPE": "SE", "TOCANTINS": "TO"},
    "AU": {"NEW SOUTH WALES": "NSW", "VICTORIA": "VIC", "QUEENSLAND": "QLD",
           "WESTERN AUSTRALIA": "WA", "SOUTH AUSTRALIA": "SA", "TASMANIA": "TAS",
           "AUSTRALIAN CAPITAL TERRITORY": "ACT", "NORTHERN TERRITORY": "NT"},
}
# 澳/加那两家的官方缩写本来就不是两字母（NSW/VIC/QLD…），照官方写法给，别硬凑成两字母。


def _state(value: str, cc: str):
    table = _STATE.get(cc)
    if not table:
        return None
    code = table.get(_bare(value))
    if not code or code == value.strip().upper():
        return None
    return (code, "STATE_ABBR", f"{cc} 的州/省官方写法是 {code}")


# ── 识别号那一格：只清字符，不再往号上补官方标点 ────────────────────────────────
# 从前有一条「整格恰好一个裸号就按国别补点与斜杠」（巴西 CNPJ/CPF）。2026-10-23 业主定案
# 这一格发「票面标签词 + 连写号码」（CNPJ00280273000137），补标点正好反着走——人一按就把
# 要发的样子拆回带标点，所以整条撤掉。号码本体长什么样是票面说了算，不是形状表说了算。
_RULES = (("POSTAL", _postal), ("STATE", _state))


def one(field: str, value: str, country: str = ""):
    """这一格能不能由现值唯一推出新值。可以 → (新值, 规则号, 依据)；不可以 → None。

    多条规则可以同批点火（先清字符，再按国别补格式），返回的规则号用 + 串起来，
    人点一下就是把这一格一次做到位，不留"点了优化还剩半格"的中间态。"""
    raw = str(value or "")
    cur = _clean(raw)
    if not cur:
        return None
    rules, whys = [], []
    if cur != raw:
        rules.append("CHARS")
        whys.append("去掉票面排版带进来的不可见字符与非 ASCII 连字符")
    cc = country_code(country)
    for suffix, fn in _RULES:
        if field.endswith("_INFO_" + suffix):
            got = fn(cur, cc)
            if got:
                cur, rid, why = got
                rules.append(rid)
                whys.append(why)
    if not rules or cur == raw:
        return None
    return (cur, "+".join(rules), "；".join(whys))


def country_code(value: str) -> str:
    """把国家那一格认成两字母码；认不出返回空串（规则就不点火）。延迟 import 避开循环。"""
    from to_air import country_to_iso2
    v = str(value or "").strip().upper()
    if not v:
        return ""
    iso = country_to_iso2(v)
    return iso if iso and len(iso) == 2 and re.fullmatch(r"[A-Z]{2}", str(iso)) else ""


def _party(field: str):
    for pfx in _PARTY_PREFIXES:
        for suf in ACTIONABLE_SUFFIXES:
            if field == f"{pfx}_INFO_{suf}":
                return pfx, suf
    return None, None


# ── 同一张表还要回答"这一格看着与目的国口径不合"（只挂旗，绝不改值） ─────────────
# 位数表与形状表都刻意留白：**表外国家一律闭嘴**。阿联酋/卡塔尔本来就没有街道邮编体系、
# 中国省没有两字母码、埃及税号位数几种口径都在用——给它们报旗只会把真旗淹掉。
POSTAL_LEN = {          # 国家 → 这格该有的位数（去分隔符后数）
    "BR": ((8,), "8 位"), "JP": ((7,), "7 位"), "US": ((5, 9), "5 位或 ZIP+4 的 9 位"),
    "CA": ((6,), "6 位"), "GB": ((5, 6, 7), "5-7 位"), "IE": ((7,), "7 位"),
    "NL": ((6,), "4 数字+2 字母"), "SE": ((5,), "5 位"), "NO": ((4,), "4 位"),
    "DK": ((4,), "4 位"), "FI": ((5,), "5 位"), "DE": ((5,), "5 位"), "AT": ((4,), "4 位"),
    "CH": ((4,), "4 位"), "BE": ((4,), "4 位"), "PL": ((5,), "5 位"), "CZ": ((5,), "5 位"),
    "SK": ((5,), "5 位"), "HU": ((4,), "4 位"), "RO": ((6,), "6 位"), "BG": ((4,), "4 位"),
    "GR": ((5,), "5 位"), "PT": ((7,), "7 位"), "HR": ((5,), "5 位"), "SI": ((4,), "4 位"),
    "FR": ((5,), "5 位"), "ES": ((5,), "5 位"), "IT": ((5,), "5 位"),
    "CN": ((6,), "6 位"), "IN": ((6,), "6 位"), "TH": ((5,), "5 位"), "MY": ((5,), "5 位"),
    "SG": ((6,), "6 位"), "ID": ((5,), "5 位"), "PH": ((4,), "4 位"), "VN": ((6,), "6 位"),
    "KR": ((5, 6), "新制 5 位（旧制 6 位也还在票面上）"),
    "IL": ((5, 7), "新制 7 位（旧制 5 位也还在票面上）"),
    "AU": ((4,), "4 位"), "NZ": ((4,), "4 位"), "MX": ((5,), "5 位"),
    "AR": ((4, 5, 8), "4 位旧码或 CPA 的字母+4+3"), "CL": ((6, 7), "6-7 位"),
    "PE": ((5,), "5 位"), "CO": ((6,), "6 位"), "ZA": ((4,), "4 位"),
    "NG": ((6,), "6 位"), "KE": ((5,), "5 位"), "RU": ((6,), "6 位"),
    "UA": ((5,), "5 位"), "KZ": ((6,), "6 位"), "BY": ((6,), "6 位"),
}

def _alnum_only(v: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(v or "")).upper()


# 邮编位数表也**只在整格是纯数字时**才量：票面常把 "1000 LJUBLJANA"、"SI-1000" 这种
# 城市名/前缀一起塞进那一格，量出来的"位数"是假的。带字母的国家（加/英/爱/荷）例外——
# 那四家的邮编本来就含字母，形状由上面 _POSTAL 那张表管。
_LETTER_POSTAL = ("CA", "GB", "IE", "NL")


def shape_warns(field: str, value: str, country: str = "") -> list:
    """这一格与目的国口径不合的红旗（只报不改，且表外国家一律闭嘴）。

    **识别号那一格不在这里判形状**：`*_INFO_EORI` 从 v1.12.0 起就同时装海关号与各国本土
    税号，意大利收货人给中国 USCI 是正解（`_id_cell_warns` 的注释里写过同一个结论，
    2026-10-20 拿真夹具再试一次又被咬回来）。那一格由"过短/混标签/票面查无此号"三条管。"""
    v = str(value or "").strip()
    if not v:
        return []
    pfx, suf = _party(field)
    if suf == "COUNTRY":
        return [] if country_code(v) else [
            f"{field} 的国家这一格既不是两字母码、也不在码表里: {v!r}"
            "（提交时按原样发出去，需核票；确认是正规国名就补 codes.COUNTRY_ISO2）"]
    cc = country_code(country)
    if suf != "POSTAL" or not cc:
        return []
    lens, hint = POSTAL_LEN.get(cc, ((), ""))
    if not lens:
        return []
    compact = _alnum_only(v)
    if compact.startswith(cc) and len(compact) > len(cc):
        compact = compact[len(cc):]            # SE-114 33 那种国别前缀不算位数
    if not compact.isdigit() and cc not in _LETTER_POSTAL:
        return []                              # 混进了城市名之类：量出来的位数不可信，闭嘴
    return [] if len(compact) in lens else [
        f"{field} 的 {cc} 邮编应是 {hint}，现 {len(compact)} 位: {v!r}（需核票）"]



def hs_warns(field: str, value: str) -> list:
    """HS 编码整段不足 6 位才报：一格里混着尺寸/体积时不报，免得把真旗淹了。"""
    s = str(value or "")
    if not s.strip():
        return []
    bad = [part.strip() for part in re.split(r"[,;]", s)
           if part.strip() and len(re.sub(r"\D", "", part)) < 6]
    return [f"{field} 的 HS 编码不足 6 位（ICS2 起 6 位、报关口径 8-10 位）: "
            f"{', '.join(repr(x) for x in bad)}（需核票）"] if bad else []



# 提交时本来就会做的归一：这几条不是本模块的规则，但"发出值"必须把它们算进来，
# 否则人点的那一下和真正发出去的那一版就不是一份东西。两条链的口径**不一样**：
#   分单走 to_air —— 国家转两字母码、电话取首个号码；
#   主单走 clean_ams —— 国家列只转大写（公司 AMS_RECORD 那一格到底要不要 alpha-2，
#     IT 还没定，见 [[j9-ams-six-endpoint-contract]]），识别号/ AEO 去空格并大写。
_AIR_RULES = {
    "COUNTRY": ("AIR_COUNTRY", "按 L3 航空口径转成两字母国家码"),
    "TEL": ("AIR_TEL", "电话只取第一个号码、去括号与多余符号"),
    "FAX": ("AIR_FAX", "传真只取第一个号码、去括号与多余符号"),
    "EORI": ("AIR_EORI", "识别号摘掉标签词、多个号用「 / 」拼进这一格"),
}
_MASTER_AIR_RULES = {
    "COUNTRY": ("AIR_CASE", "按公司口径转大写（这一格是否收两字母码待 IT 定，不替它转）"),
    "EORI": ("AIR_ID", "按公司口径：这一格去空格并转大写"),
    "AEO": ("AIR_ID", "按公司口径：这一格去空格并转大写"),
}
_PULL_WHY = {
    "EORI": "这一格是空的：票面别处（地址或页底 customs 区）印着的识别号，提交时 L3 会并进这一格",
    "TEL": "这一格是空的：整串里印着的号码，提交时 L3 取第一个填进这一格",
    "FAX": "这一格是空的：整串里印着的号码，提交时 L3 取第一个填进这一格",
}


def actionable_columns(cols) -> list:
    """把调用方给来的列面（`fieldspec.desk_keys()` 或 `master_fields.MASTER_COLS`）收成可优化那几列。

    列名不在这里抄一份：两条链的列面各有唯一的家，这里只做筛选，
    免得日后 IT 改列名时这儿长成第四处手写副本。"""
    return [c for c in cols if _party(c)[0]]


def _pull_rule(suf: str, cur: str, rid: str, why: str):
    """这一格本来是空的、值是从整串里提出来的：那要说成「提进来」，不能说成"摘掉标签词"。

    真票上这一档出现得最多（本地 44 份解析结果里 24 格都是它），话说错了人就会去翻标签词。"""
    if cur.strip() or suf not in _PULL_WHY:
        return rid, why
    return "AIR_PULL", _PULL_WHY[suf]


def preview(fields: dict, chain: str = "hawb") -> list:
    """整票扫描：返回 [{"field","from","to","rule","why"}]，就是核对区那些"可优化格"。

    先过本链的提交归一（分单 `to_air` / 主单 `clean_ams`，也就是不发也本来会做的那一步），
    再过本模块的国别规则。国家取自**同一主体**那一格，绝不拿另一家的：
    串主体等于把发货人的邮编按收货人国家改。"""
    fields = fields if isinstance(fields, dict) else {}
    if not fields:
        return []
    if chain == "master":
        from master_fields import clean_ams
        sent, air_rules = clean_ams(fields), _MASTER_AIR_RULES
    else:
        from to_air import to_air
        sent, air_rules = to_air(fields), _AIR_RULES
    rows = []
    for field in fields:
        pfx, suf = _party(field)
        if not suf:
            continue
        cur = str(fields.get(field) or "")
        raw = sent.get(field)
        base = "" if raw is None else str(raw)
        rules, whys = [], []
        if base != cur:
            if suf in air_rules:
                rid, why = _pull_rule(suf, cur, *air_rules[suf])
                rules, whys = [rid], [why]
            else:
                base = cur           # 提交归一动了这一格却不认识它：照旧发原值，不瞎报依据
        got = one(field, base, str(fields.get(f"{pfx}_INFO_COUNTRY") or ""))
        if got:
            base, rid, why = got
            rules.append(rid)
            whys.append(why)
        if base != cur:
            rows.append({"field": field, "from": cur, "to": base,
                         "rule": "+".join(rules), "why": "；".join(whys)})
    return rows
