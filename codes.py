# -*- coding: utf-8 -*-
"""公共码表与票面归一：两条链（分单/主单）与批处理、导出、校验都从这里取同一套真值。

这里是"码表与写法"唯一的家：IATA/国家码、电话区号、税号标签、主单号格式、发货/收货人拆列。
从前它们住在那套已退役的版式规则层里，别的模块直接调它的私有函数（下划线那种）当公用件用——
搬出来后跨模块只许叫公共名，`_` 开头的一律视为模块私产（tests/test_imports.py 盯着这两条）。
"""
import re
import unicodedata

TARGET_KEYS = ("MAWB_NO", "HAWB_NO", "SHIPPER_INFO", "CONSIGNEE_INFO", "ORIGIN_NAME",
               "TO1", "TO2", "TO3", "DEST_NAME", "GOODS_INFO", "GOODS_HS_CODE",
               "PIECES", "WEIGHT", "SLAC", "CREATE_TIME", "SEND_STATUS")

# 列面 = 基础若干 + 发货人 11 项 + 收货人 11 项 (CITTY 为库表原拼写, 勿"修正")
# 2026-10-10 业主定案：`*_INFO_TAX_ID` 两列**删除**，票面税号（中国 USCI/统一社会信用代码、
# 巴西 CNPJ、RFC、VAT NO、TAX ID）一律落进同主体的 `*_INFO_EORI` 那一格，多个号用 " / " 拼。
# 公司 AMS 列面本来就没有税号格（v1.8.0 起提交前就是这么并的），所以这一改只是把"两个地方"
# 收成"一个地方"，不再出现"票面有税号、EORI 空着、号在另一列"的第三种状态。
# GOODS_HS_CODE 是货物描述里 HS CODE(S) 的唯一落点；GOODS_INFO 保持照抄不动（保真口径）。
TARGET_KEYS_OUT = TARGET_KEYS[:2] + ("SHIPPER_INFO", "CONSIGNEE_INFO") + TARGET_KEYS[4:16] + \
    tuple(p + s for p in ("SHIPPER_INFO_", "CONSIGNEE_INFO_")
          for s in ("COMP_NAME", "COMP_ADDRESS", "CITY" if p == "SHIPPER_INFO_" else "CITTY",
                    "COUNTRY", "STATE", "POSTAL", "TEL", "FAX", "EORI", "AEO", "EMAIL"))

# 票面税号标签 -> 号码。只认带标签的，绝不拿裸数字猜（18 位纯数字也可能是货值/账号）。
# EORI 是 2026-09-26 八票逐格对台实测补的：欧票把海关号印成 `EORI IT03268900267`，
# 标签表漏了它导致五张欧票的 TAX 反向核查全哑。
TAX_LABEL_RE = re.compile(
    r"\b(USCI|统一社会信用代码|CNPJ|CPF|R\.?\s?F\.?\s?C\.?|GST\s*IN|GST|TAX\s*ID|TAX\s*NO"
    r"|EORI\s*(?:NO|NR|NUMBER)?|VAT\s*(?:NO|NR|ID|NUMBER)?)\b[^0-9A-Za-z]{0,4}([A-Z]{0,3}[0-9][0-9A-Za-z./\-]{5,})",
    re.I)

TAX_PREFIX_RE = re.compile(r"^(?:USCI|CNPJ|CPF|RFC|GST\s*IN|GST|TAX\s*ID|TAX\s*NO|TAX|EORI\s*(?:NO|NR|NUMBER)?"
                         r"|VAT\s*(?:NO|NR|NUMBER|ID)|VAT|统一社会信用代码)[#.:：\s]*", re.I)

def clean_tax(v) -> str:
    """税号归一：去空白、转大写、摘掉模型连标签一起抄进来的前缀（票面 'VAT#769638661' → 号码本身）。
    CNPJ/RFC 带点斜横线是号码的一部分，保留；只摘开头的标签词。"""
    out = re.sub(r"\s+", "", str(v or "")).upper()
    for _ in range(2):
        stripped = TAX_PREFIX_RE.sub("", out, count=1)
        if stripped == out:
            break
        out = stripped
    return out

# 一格装多个识别号的分隔符：**两侧必须带空格**。巴西 CNPJ 自己就含斜杠
# （07.454.234/0001-10），用裸斜杠当隔符会把一个号码切成两段、再拼回去永久坏掉。
_ID_SPLIT = re.compile(r"\s+/\s+")


def merge_ids(*vals) -> str:
    """把同主体的多个识别号并成一格（EORI 与税号今天共用这一格）。
    先拆再拼：暂存会反复写同一格，下一次读回来的就已经是拼好的那串——不拆的话
    每写一次多挂一段，第三次变成 "A / B / B / B"。"""
    out = []
    for v in vals:
        for part in _ID_SPLIT.split(str(v or "").strip()):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return " / ".join(out)


def split_ids(v) -> list:
    """拆开一格里的多个号（形态检查要逐段判）。"""
    return [x.strip() for x in _ID_SPLIT.split(str(v or "").strip()) if x.strip()]


# ------- 第二遍(航空运输要求)规范化参考表: 国家两位码 / 城市 IATA 三字码 ------
COUNTRY_ISO2 = {
    "CHINA": "CN", "P.R. CHINA": "CN", "P R CHINA": "CN", "PEOPLES REPUBLIC OF CHINA": "CN",
    "PEOPLE'S REPUBLIC OF CHINA": "CN", "MAINLAND CHINA": "CN", "CHINE": "CN",
    "TAIWAN": "TW", "HONG KONG": "HK", "MACAU": "MO", "MACAO": "MO",
    "JAPAN": "JP", "KOREA": "KR", "SOUTH KOREA": "KR", "REPUBLIC OF KOREA": "KR",
    "NORTH KOREA": "KP", "SINGAPORE": "SG", "MALAYSIA": "MY", "THAILAND": "TH",
    "VIETNAM": "VN", "VIET NAM": "VN", "INDONESIA": "ID", "PHILIPPINES": "PH",
    "INDIA": "IN", "PAKISTAN": "PK", "BANGLADESH": "BD", "SRI LANKA": "LK", "NEPAL": "NP",
    "MYANMAR": "MM", "CAMBODIA": "KH", "MONGOLIA": "MN", "KAZAKHSTAN": "KZ", "UZBEKISTAN": "UZ",
    "UAE": "AE", "UNITED ARAB EMIRATES": "AE", "SAUDI ARABIA": "SA", "QATAR": "QA",
    "KUWAIT": "KW", "BAHRAIN": "BH", "OMAN": "OM", "ISRAEL": "IL", "JORDAN": "JO",
    "LEBANON": "LB", "TURKEY": "TR", "IRAN": "IR", "EGYPT": "EG", "SOUTH AFRICA": "ZA",
    "NIGERIA": "NG", "KENYA": "KE", "TANZANIA": "TZ", "GHANA": "GH", "MOROCCO": "MA",
    "ALGERIA": "DZ", "TUNISIA": "TN", "USA": "US", "U.S.A.": "US", "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US", "AMERICA": "US", "CANADA": "CA", "MEXICO": "MX",
    "BRAZIL": "BR", "ARGENTINA": "AR", "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE",
    "VENEZUELA": "VE", "URUGUAY": "UY", "ECUADOR": "EC", "UK": "GB", "U.K.": "GB",
    "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB", "ENGLAND": "GB", "SCOTLAND": "GB",
    "IRELAND": "IE", "GERMANY": "DE", "DEUTSCHLAND": "DE", "FRANCE": "FR", "ITALY": "IT",
    "SPAIN": "ES", "PORTUGAL": "PT", "NETHERLANDS": "NL", "HOLLAND": "NL", "BELGIUM": "BE",
    "LUXEMBOURG": "LU", "SWITZERLAND": "CH", "AUSTRIA": "AT", "DENMARK": "DK", "NORWAY": "NO",
    "SWEDEN": "SE", "FINLAND": "FI", "ICELAND": "IS", "POLAND": "PL", "CZECH REPUBLIC": "CZ",
    "CZECHIA": "CZ", "SLOVAKIA": "SK", "HUNGARY": "HU", "ROMANIA": "RO", "BULGARIA": "BG",
    "GREECE": "GR", "CROATIA": "HR", "SLOVENIA": "SI", "SERBIA": "RS", "UKRAINE": "UA",
    "RUSSIA": "RU", "RUSSIAN FEDERATION": "RU", "BELARUS": "BY", "LITHUANIA": "LT",
    "LATVIA": "LV", "ESTONIA": "EE", "MALTA": "MT", "CYPRUS": "CY", "AUSTRALIA": "AU",
    "NEW ZEALAND": "NZ",
}

CITY_IATA = {
    "BEIJING": "BJS", "PEKING": "BJS", "XI'AN": "XIY", "XIAN": "XIY", "QINGDAO": "TAO",
    "SHANGHAI": "SHA", "GUANGZHOU": "CAN", "SHENZHEN": "SZX", "CHENGDU": "CTU",
    "HANGZHOU": "HGH", "NANJING": "NKG", "TIANJIN": "TSN", "WUHAN": "WUH", "XIAMEN": "XMN",
    "KUNMING": "KMG", "CHONGQING": "CKG", "DALIAN": "DLC", "ZHENGZHOU": "CGO",
    "CHANGSHA": "CSX", "SHENYANG": "SHE", "HARBIN": "HRB", "URUMQI": "URC", "SANYA": "SYX",
    "HAIKOU": "HAK", "JINAN": "TNA", "HEFEI": "HFE", "NANCHANG": "KHN", "FUZHOU": "FOC",
    "NANNING": "NNG", "GUIYANG": "KWE", "LANZHOU": "LHW", "YINCHUAN": "INC", "XINING": "XNN",
    "HOHHOT": "HET", "TAIYUAN": "TYN", "SHIJIAZHUANG": "SJW", "CHANGCHUN": "CGQ",
    "WUXI": "WUX", "NINGBO": "NGB", "WENZHOU": "WNZ", "CHANGZHOU": "CZX", "NANTONG": "NTG",
    "YANTAI": "YNT", "WEIHAI": "WEH", "LINYI": "LYI", "XUZHOU": "XUZ", "GUILIN": "KWL",
    "ZHANJIANG": "ZHA", "SHANTOU": "SWA", "LUOYANG": "LYA", "MIANYANG": "MIG", "LIJIANG": "LJG",
    "TOKYO": "TYO", "OSAKA": "OSA", "NAGOYA": "NGO", "SEOUL": "SEL", "INCHEON": "ICN",
    "BUSAN": "PUS", "TAIPEI": "TPE", "HONGKONG": "HKG", "MACAU": "MFM", "BANGKOK": "BKK",
    "SINGAPORE": "SIN", "KUALA LUMPUR": "KUL", "JAKARTA": "JKT", "MANILA": "MNL",
    "HANOI": "HAN", "HO CHI MINH": "SGN", "HOCHIMINH": "SGN", "DELHI": "DEL", "MUMBAI": "BOM",
    "CHENNAI": "MAA", "KOLKATA": "CCU", "BANGALORE": "BLR", "DUBAI": "DXB", "DOHA": "DOH",
    "ABU DHABI": "AUH", "RIYADH": "RUH", "JEDDAH": "JED", "ISTANBUL": "IST", "TEL AVIV": "TLV",
    "CAIRO": "CAI", "LONDON": "LON", "MANCHESTER": "MAN", "BIRMINGHAM": "BHX", "GLASGOW": "GLA",
    "DUBLIN": "DUB", "SHANNON": "SNN", "PARIS": "PAR", "LYON": "LYS", "MARSEILLE": "MRS",
    "FRANKFURT": "FRA", "MUNICH": "MUC", "MUENCHEN": "MUC", "MUNCHEN": "MUC", "BERLIN": "BER",
    "HAMBURG": "HAM", "DUSSELDORF": "DUS", "STUTTGART": "STR", "COLOGNE": "CGN", "KOELN": "CGN",
    "LEIPZIG": "LEJ", "BREMEN": "BRE", "HANNOVER": "HAJ", "NUREMBERG": "NUE", "DRESDEN": "DRS",
    "AMSTERDAM": "AMS", "ROTTERDAM": "RTM", "EINDHOVEN": "EIN", "ENSCHEDE": "ENS",
    "GRONINGEN": "GRQ", "MAASTRICHT": "MST", "BRUSSELS": "BRU", "LIEGE": "LGG",
    "CHARLEROI": "CRL", "LUXEMBOURG CITY": "LUX", "ZURICH": "ZRH", "GENEVA": "GVA",
    "BASEL": "BSL", "VIENNA": "VIE", "COPENHAGEN": "CPH", "OSLO": "OSL", "STOCKHOLM": "STO",
    "GOTHENBURG": "GOT", "LANDVETTER": "GOT", "KARLSTAD": "KSD", "MALMO": "MMA",
    "HELSINKI": "HEL", "WARSAW": "WAW", "WARSZAWA": "WAW", "PRAGUE": "PRG", "BUDAPEST": "BUD", "BUCHAREST": "BUH",
    "SOFIA": "SOF", "BELGRADE": "BEG", "ZAGREB": "ZAG", "MOSCOW": "MOW", "PETERSBURG": "LED",
    "LYON": "LYS", "EXUPERY": "LYS",
    "KYIV": "IEV", "KIEV": "IEV", "NEW YORK": "NYC", "LOS ANGELES": "LAX", "CHICAGO": "CHI",
    "HOUSTON": "HOU", "DALLAS": "DFW", "ATLANTA": "ATL", "MIAMI": "MIA",
    "SAN FRANCISCO": "SFO", "SEATTLE": "SEA", "BOSTON": "BOS", "DETROIT": "DTT",
    "TORONTO": "YTO", "VANCOUVER": "YVR", "MONTREAL": "YMQ", "MEXICO CITY": "MEX",
    "SAO PAULO": "SAO", "SAO": "SAO", "SANTIAGO": "SCL", "BUENOS AIRES": "BUE", "LIMA": "LIM", "BOGOTA": "BOG",
    "MANAUS": "MAO", "RIO DE JANEIRO": "RIO", "BRASILIA": "BSB", "CARACAS": "CCS", "MONTEVIDEO": "MVD",
    "GUARULHOS": "GRU", "ERCAN": "ECN", "VIRACOPOS": "VCP",
    "SYDNEY": "SYD", "MELBOURNE": "MEL", "BRISBANE": "BNE", "PERTH": "PER", "AUCKLAND": "AKL",
    "JOHANNESBURG": "JNB", "LAGOS": "LOS", "NAIROBI": "NBO", "CASABLANCA": "CMN", "TUNIS": "TUN",
    "ALGIERS": "ALG", "MILAN": "MIL", "MILANO": "MIL", "MALPENSA": "MXP", "LINATE": "LIN",
    "ROME": "ROM", "ROMA": "ROM", "MADRID": "MAD", "BARCELONA": "BCN", "VALENCIA": "VLC",
    "LISBON": "LIS", "PORTO": "OPO", "ATHENS": "ATH", "GDANSK": "GDN", "KRAKOW": "KRK",
    "VILNIUS": "VNO", "RIGA": "RIX", "TALLINN": "TLL", "SALZBURG": "SZG", "LJUBLJANA": "LJU",
    "BRATISLAVA": "BTS",
}

def _fold_ascii(s: str) -> str:
    """É->E, ã->a, ß->ss：票面带重音的地名要能和 ASCII 写的码表键对上。"""
    return unicodedata.normalize("NFKD", str(s or "")) \
        .encode("ascii", "ignore").decode("ascii", "ignore")

_CITY_IATA_N = {re.sub(r"[^A-Z]", "", _fold_ascii(k)): v for k, v in CITY_IATA.items()}

_STATES = ("SHAANXI", "SHANXI", "GUANGDONG", "ZHEJIANG", "JIANGSU", "SHANDONG", "FUJIAN",
           "HUNAN", "HUBEI", "HENAN", "HEBEI", "SICHUAN", "YUNNAN", "GUIZHOU", "ANHUI",
           "JIANGXI", "LIAONING", "JILIN", "HEILONGJIANG", "QINGHAI", "GANSU", "HAINAN",
           "GUANGXI", "NINGXIA", "XINJIANG", "INNER MONGOLIA", "TIBET", "CALIFORNIA", "TEXAS",
           "FLORIDA", "ILLINOIS", "PENNSYLVANIA", "OHIO", "MICHIGAN", "NEW JERSEY",
           "MASSACHUSETTS", "WASHINGTON", "GEORGIA", "VIRGINIA", "NORTH CAROLINA", "ARIZONA",
           "TENNESSEE", "INDIANA", "MISSOURI", "MARYLAND", "WISCONSIN", "COLORADO",
           "MINNESOTA", "ONTARIO", "BRITISH COLUMBIA", "ALBERTA", "QUEBEC", "MANITOBA",
           "SASKATCHEWAN", "BAYERN", "BAVARIA", "HESSEN", "HESSE", "SAXONY", "SACHSEN", "NRW")

_CITY_STOP = ("LTD", "LIMITED", "CO", "INC", "GMBH", "BV", "NV", "LLC", "CORP", "CORPORATION",
              "COMPANY", "AB", "AS", "OY", "KG", "AG", "SA", "SPA", "SRL", "PLC", "KK", "TBK",
              "ROAD", "RD", "STREET", "ST", "AVENUE", "AVE", "PARK", "INDUSTRIAL", "ZONE",
              "BUILDING", "BLDG", "TOWER", "CENTER", "CENTRE", "SUITE", "FLOOR", "ROOM",
              "DISTRICT", "COUNTY", "TOWN", "VILLAGE", "AIRPORT", "WAREHOUSE", "PLANT",
              "FACTORY", "OFFICE", "UNIT", "PLOT", "BLOCK", "FREE", "TRADE", "LOGISTICS",
              "TRANSPORT", "FREIGHT", "FORWARDING", "SHIPPING", "TRADING", "EXPORT", "IMPORT",
              "DEPARTMENT", "DEPT", "BRANCH", "NO", "CONTACT")

_POSTAL_RXS = [
    re.compile(r"\b[A-Z]{2}-\d{3,4}\s?\d{2}\b"),           # SE-652 21 / FI-00100 / PL-00123
    re.compile(r"\b\d{5}-\d{3}\b"),                         # BR CEP
    re.compile(r"\b\d{5}-\d{4}\b"),                         # US ZIP+4
    re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"),   # UK
    re.compile(r"\b\d{4}\s?[A-Z]{2}\b"),                    # NL 1234 AB
    re.compile(r"\b\d{5,6}\b"),                             # CN/US/DE/IT...
    re.compile(r"\b\d{3}\s?\d{2}\b"),                       # FR/SE
]

def lookup_city(name):
    """城市名 -> IATA 三字码 (XI'AN->XIY)；查不到返回空串"""
    s = _fold_ascii(str(name or "").upper())
    if not s:
        return ""
    full = re.sub(r"[^A-Z]", "", s)
    if full in _CITY_IATA_N:
        return _CITY_IATA_N[full]
    for tok in re.split(r"[^A-Z]+", s):  # "PEK-PEKING" / "NARITAAPT/TOKYO" 按非字母分词
        if tok and tok in _CITY_IATA_N:
            return _CITY_IATA_N[tok]
    if len(full) >= 6:  # OCR 粘连: 前缀匹配已知城市名 (BEIJINGCAPITAL... -> BJS)
        for k, v in _CITY_IATA_N.items():
            if len(k) >= 5 and full.startswith(k):
                return v
    return ""

def iata_place(v):
    """起降港名 -> IATA 三字码 (BEIJING->BJS / GOTHENBURG LANDVE->GOT)；已是三字码或未知则原样"""
    v = str(v or "").strip().upper()
    if not v:
        return ""
    return lookup_city(v) or v

def fmt_mawb(v):
    """主单号规范化（后8位中间不加空格）:
    131-33062536 -> 131-33062536；131-3306 2536 -> 131-33062536；
    999-PEK06865456 (3位前缀+机场码+8位) -> 999-06865456 (机场码非号码组成)；
    074-PEK68614 (机场码+5位, 信息不足) -> 074-PEK 68614 原样"""
    if v is None or str(v).strip() == "":
        return ""
    raw = str(v).strip().upper()
    mp = re.match(r"^(\d{2,3})[\s-]+(.+)$", raw)
    if mp:
        pre, rest = mp.group(1), mp.group(2)
        alnum = re.sub(r"[^A-Z0-9]", "", rest)
        ml = re.fullmatch(r"([A-Z]{3})?(\d+)", alnum)
        if ml:
            letters, digits = ml.group(1), ml.group(2)
            if len(digits) == 8:
                return f"{pre}-{digits}"                            # 8位连写，不分 4-4
            if letters:
                return f"{pre}-{letters} {digits}"
            return f"{pre}-{digits}"
    s = raw.replace(" ", "-")
    m = re.fullmatch(r"(\d{2,3})-([A-Z]{3})?(\d{7,8})", s)
    if m:
        pre, letters, digits = m.group(1), m.group(2), m.group(3)
        if len(digits) == 8:
            return f"{pre}-{digits}"
        if letters:
            return f"{pre}-{letters} {digits}"
        return f"{pre}-{digits}"
    # 已是 XXX-XXXX XXXX 的直接去中间空格
    m2 = re.fullmatch(r"(\d{2,3})-(\d{4}) (\d{4})", raw)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}{m2.group(3)}"
    return raw

_HS_CODE_RE = re.compile(r"H\.?\s*S\.?\s*(?:CODES?|码)\s*[:：]?\s*([0-9][0-9.,，;；/\s]{3,})", re.I)

def hs_code_text(cargo):
    """货物描述里 HS CODE(S) 后面的号码（可能多个，逗号连接）；点/空格归一，非纯数字丢弃。
    VLM 通道由模型直接填 GOODS_HS_CODE，这里兜 Excel/OCR 等结构化通道。"""
    out = []
    for c in cargo:
        for m in _HS_CODE_RE.finditer(str(c.get("description") or "")):
            for tok in re.split(r"[,，;；/]", m.group(1)):
                t = re.sub(r"[.\s]", "", tok)
                if t.isdigit() and 4 <= len(t) <= 12 and t not in out:
                    out.append(t)
    return ",".join(out)

_FW_TRANS = str.maketrans({"，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")",
                           "【": "[", "】": "]", "、": ",", "－": "-", "　": " "})

_SEG_PUNCT = " .;()[]{}"

def _strip_seg(s: str) -> str:
    """地址段两端去标点。摘掉尾部国码 '(SK)' 里的码后会留下不配对的 '(SK'，一并清掉。"""
    out = s.strip(_SEG_PUNCT)
    out = re.sub(r"[,(]\s*[A-Z]{0,4}\s*$", "", out).strip(_SEG_PUNCT)
    return out

def parse_party(p):
    """收/发货人 -> 结构化: 公司名/完整地址/街道/城市/州省/邮编/国家/电话/传真/EORI/AEO/EMAIL"""
    p = p or {}
    name = str(p.get("name") or "").translate(_FW_TRANS).strip()
    tel = str(p.get("tel") or "").translate(_FW_TRANS).strip()
    fax = str(p.get("fax") or "").translate(_FW_TRANS).strip()
    lines = [l.translate(_FW_TRANS).strip() for l in str(p.get("address") or "").splitlines()
             if l.strip()]
    blob = "\n".join(lines)
    if not tel:
        m = re.search(r"(?:TELEPHONE|PHONE|TEL|PH|TE)\b\.?\s*[/：:]?\s*(?:FAX)?\.?\s*[:：]?\s*"
                      r"(\+?\(?[\d][\d\s\-()./]{6,}?\d)", blob, re.I)
        tel = m.group(1).strip() if m else ""
    if not fax:
        m = re.search(r"FAX\b\.?\s*[/：:]?\s*(?:TEL|PH)?\.?\s*[:：]?\s*(\+?\(?[\d][\d\s\-()./]{6,}?\d)",
                      blob, re.I)
        fax = m.group(1).strip() if m else ""
    email = ""
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]*[A-Za-z]", blob)
    if m:
        email = m.group(0).strip(".,;")
    eori = aeo = ""
    m = (re.search(r"\bEORI\b[\s:：#.-]*([A-Z]{2}[A-Z0-9]{6,15})", blob, re.I)
         or re.search(r"\bV\.?A\.?T\.?(?:\s*(?:NO|NR|NUMBER|ID))?\b[\s:：.#-]*([A-Z]{2}[A-Z0-9]{6,15})",
                      blob, re.I))
    if m:
        eori = m.group(1).upper()
    elif re.fullmatch(r"[A-Z]{2}[A-Z0-9]{5,15}", str(p.get("tax_id") or "").upper()):
        eori = str(p["tax_id"]).upper()  # EORI/VAT 形态税号兜底 (USCI 纯数字不匹配)
    m = re.search(r"\bAEO\b[\s:：#.-]*([A-Z]{2}[A-Z0-9]{5,15})", blob, re.I)
    if m:
        aeo = m.group(1).upper()
    # 解析用地址: 摘除电话/邮箱/EORI/VAT/AEO 片段与公司名行
    clean = []
    for l in lines:
        l = re.sub(r"(?:TEL|FAX|PHONE|MOBILE)\.?\s*[:：/][^,\n]*", " ", l, flags=re.I)
        l = re.sub(r"(?:E-?MAIL|EORI|V\.?A\.?T\.?|AEO)\.?\s*[:：]?\s*[A-Z]{2}[A-Z0-9]{6,15}", " ", l, flags=re.I)
        l = re.sub(r"\bE-?MAIL\b[\s:：][^,\n]*", " ", l, flags=re.I)
        l = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]*", " ", l)
        l = re.sub(r",\s*\(\s*", ", ", l)  # OCR 噪声: "SHAANXI,( CHINA" -> "SHAANXI, CHINA"
        l = re.sub(r"\s{2,}", " ", l).strip(" ,;")
        if l and l.upper() != name.upper():
            clean.append(l)
    full_addr = ", ".join(clean)
    up = full_addr.upper()

    def _rightmost(names):
        best = None
        for nm in names:
            rx = re.compile(r"(?<![A-Z])" + re.escape(nm) + r"(?![A-Z])")
            for m in rx.finditer(up):
                if best is None or m.end() > best.end():
                    best = m
        return best

    country = state = postal = ""
    m = _rightmost(COUNTRY_ISO2)
    if m:
        country = m.group(0)
    m = _rightmost(_STATES)
    if m:
        state = m.group(0)
    ppos, ptxt = -1, ""
    for rx in _POSTAL_RXS:
        last = None
        for m in rx.finditer(up):
            last = m
        if last:
            ppos, ptxt = last.start(), last.group(0)
            break
    # 国家兜底推断：无显式国家码时，凭税号/邮编格式判断
    if not country:
        if re.search(r"\b\d{14}\b", up):                       # 巴西 CNPJ 14位
            country = "BRAZIL"
        elif re.search(r"\b\d{5}-\d{3}\b", up):                 # 巴西 CEP
            country = "BRAZIL"
    text = up
    if ptxt:
        text = text[:ppos] + text[ppos + len(ptxt):]
    for tok in (country, state):
        if tok:
            text = re.sub(r"(?<![A-Z])" + re.escape(tok) + r"(?![A-Z])", " ", text)
    text = re.sub(r"\s*,\s*(,\s*)+", ", ", text).strip(" ,")
    segs = [s for s in (_strip_seg(x) for x in re.split(r"[,\n]", text)) if s]
    stop_rx = re.compile(r"\b(" + "|".join(_CITY_STOP) + r")\b")
    city, city_i = "", -1
    for i in range(len(segs) - 1, -1, -1):
        if not re.search(r"[A-Z]{2}", segs[i]):  # 城市段至少 2 个字母 (剔除 OCR 标点碎片)
            continue
        if re.search(r"\d", segs[i]) or stop_rx.search(segs[i]):
            continue
        city, city_i = segs[i], i
        break
    # locality: 原地址段中含邮编的段无条件收, 仅含城市名的段仍要求无数字/无停用词 (印刷顺序)
    loc = []
    for s in [x for x in (_strip_seg(v) for v in re.split(r"[,\n]", up)) if x]:
        has_p = bool(ptxt) and ptxt in s
        has_c = bool(city) and city in s
        if not (has_p or has_c) or "+" in s or "@" in s:
            continue
        if not has_p and (re.search(r"\d", s) or stop_rx.search(s)):
            continue
        if s not in loc:
            loc.append(s)
    loc_join = ", ".join(loc)
    street_parts = [s for i, s in enumerate(segs)
                    if i != city_i and not (loc_join and s in loc_join)]
    return {"name": name, "full_addr": full_addr, "street": ", ".join(street_parts),
            "locality": ", ".join(loc), "city": city, "state": state, "postal": ptxt,
            "country": country, "tel": tel, "fax": fax,
            "eori": eori, "aeo": aeo, "email": email}

# 电话国际区号 -> ISO2（只收录样本中出现的，按需扩充）。区号判国家与"电话串边"两条红旗都用它，
# 从前 validator 里有一份、to_air 又从 validator 里借——码表的家应该是 codes。
TEL_COUNTRY_CODES = {
    "86": "CN", "46": "SE", "39": "IT", "49": "DE", "36": "HU",
    "358": "FI", "91": "IN", "52": "MX", "55": "BR", "81": "JP",
    "82": "KR", "353": "IE", "41": "CH", "1": "US", "852": "HK",
    "65": "SG", "971": "AE", "90": "TR",
}
