# -*- coding: utf-8 -*-
"""主单（MAWB）检索结果 → 我方 40 字段视图（只读核对用）。

口径（2026-09-26 用户定案）：录入员/制单员在主单检索里拿到公司资料后，要能在同一套
「票面预览 + 核对」界面里看；但**这一路只展示与核对，不接提交**（主单写接口 mawb2/ 下一批）。

两条来源，规则不同：
- 公司 `AMS_RECORD`（已录入过）：列名跟我方契约几乎一致，只做换名映射，不重新解析。
  两处历史拼写差异必须换：`CONSIGNEE_INFO_CITY` → 我方库表原拼写 `CONSIGNEE_INFO_CITTY`；
  `GOODS_INFO_HSCODE` → 我方 `GOODS_HS_CODE`。
- `AMS_RECORD` 为空（公司从未录入）：只认**带标签**的东西（TEL:/FAX:/EORI/VAT/HS CODE/国家名），
  拆不出的一律留空并写进 notes 交人工——绝不靠启发式猜地址里的城市、邮编（那是把静默错
  从票面搬进核对区的最快方式）。

主单表本身没有的列（件数/重量/始发/目的/航路/分单号）一律不猜，直接进 notes。
"""
import re

import hawb2json as h

# 公司 AMS_RECORD 列 → 我方字段（不做映射的列：NOTIFY_* 9 列、HMY_ID）
_RENAME = {"CONSIGNEE_INFO_CITY": "CONSIGNEE_INFO_CITTY",
           "GOODS_INFO_HSCODE": "GOODS_HS_CODE"}
_SKIP = {"HMY_ID"}
_NOTIFY = re.compile(r"^NOTIFY")

# 外层只读块里能确定性识别的标签（跟票面同款写法）。标签与号码之间可能连着多个
# 标点/空白（`TEL NO.: +86…`、`EORI:PL…`、`EORI NO.: IT…`），所以分隔符要能吃多次。
_SEP = r"(?:\s*[.:：、])*\s*"
_LABELED = {
    "TEL": re.compile(r"(?:TEL|TE|PHONE)\s*(?:NO|NR|NUMBER)?" + _SEP + r"([+\d][\d\s().\-]{6,})", re.I),
    "FAX": re.compile(r"FAX\s*(?:NO|NR)?" + _SEP + r"([+\d][\d\s().\-]{6,})", re.I),
    "EORI": re.compile(r"\bEORI\s*(?:NO|NR|NUMBER)?" + _SEP + r"([A-Z]{2}[0-9A-Z.\-]{6,})", re.I),
}
_HS_TAIL = re.compile(r"\bHS\s*CODES?\s*[:：]?\s*([0-9][0-9.\s,，;；/-]{4,})\s*$", re.I)
_COUNTRY_WORDS = {
    "CHINA": "CN", "U.S.A.": "US", "USA": "US", "UNITED STATES": "US", "GERMANY": "DE",
    "JAPAN": "JP", "KOREA": "KR", "INDIA": "IN", "ITALY": "IT", "GERMAN": "DE",
    "FRANCE": "FR", "ENGLAND": "GB", "UNITED KINGDOM": "GB", "CANADA": "CA",
    "NETHERLANDS": "NL", "HOLLAND": "NL", "POLAND": "PL", "SPAIN": "ES", "TURKEY": "TR",
    "BELGIUM": "BE", "SWEDEN": "SE", "DENMARK": "DK", "FINLAND": "FI", "NORWAY": "NO",
    "AUSTRALIA": "AU", "NEW ZEALAND": "NZ", "BRAZIL": "BR", "MEXICO": "MX", "SINGAPORE": "SG",
    "SWITZERLAND": "CH", "AUSTRIA": "AT", "IRELAND": "IE", "PORTUGAL": "PT", "CZECH": "CZ",
    "HUNGARY": "HU", "ROMANIA": "RO", "GREECE": "GR", "RUSSIA": "RU", "UKRAINE": "UA",
    "SAUDI ARABIA": "SA", "UNITED ARAB EMIRATES": "AE", "QATAR": "QA", "KUWAIT": "KW",
    "THAILAND": "TH", "VIETNAM": "VN", "MALAYSIA": "MY", "INDONESIA": "ID", "PHILIPPINES": "PH",
    "BANGLADESH": "BD", "PAKISTAN": "PK", "SRI LANKA": "LK", "EGYPT": "EG", "MOROCCO": "MA",
    "TUNISIA": "TN", "ALGERIA": "DZ", "NIGERIA": "NG", "KENYA": "KE", "GHANA": "GH",
    "SOUTH AFRICA": "ZA", "ZAMBIA": "ZM", "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE",
    "ARGENTINA": "AR", "ECUADOR": "EC", "KAZAKHSTAN": "KZ", "UZBEKISTAN": "UZ",
}
# 主单表压根没有的列：这些只能从票面来，公司给不了，所以永远进 notes 而不是猜
MASTER_HAS_NO = ("PIECES", "WEIGHT", "ORIGIN_NAME", "DEST_NAME", "TO1", "HAWB_NO")


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def _country_from(text: str) -> str:
    """只认整词出现的国家名（大小写不敏感），从长到短匹配，避免 CHINA 里套 CHI。"""
    up = " " + _clean(text).upper() + " "
    for word in sorted(_COUNTRY_WORDS, key=len, reverse=True):
        if re.search(r"(?<![A-Z])" + re.escape(word) + r"(?![A-Z])", up):
            return _COUNTRY_WORDS[word]
    return ""


def _party_fields(blob: str, prefix: str) -> dict:
    """从一坨拼接的公司资料里只取带标签的东西；不猜姓名/地址边界（那是票面侧的活）。"""
    out = {}
    for key, rx in _LABELED.items():
        m = rx.search(blob or "")
        if m:
            out[prefix + key] = _clean(m.group(1)).replace(" ", "") if key != "TEL" else _clean(m.group(1))
    m = h.TAX_LABEL_RE.search(blob or "")
    if m:
        val = h.clean_tax(m.group(2))
        if val:
            col = "EORI" if m.group(1).upper().startswith("EORI") else "TAX_ID"
            out.setdefault(prefix + col, val)
    cc = _country_from(blob or "")
    if cc:
        out[prefix + "COUNTRY"] = cc
    return out


def parse_master(mawb_order: dict) -> dict:
    """公司主单资料 → {fields, notes, source}。fields 只用我方字段名，notes 是给人看的清单。"""
    mo = mawb_order or {}
    ams = mo.get("AMS_RECORD") if isinstance(mo.get("AMS_RECORD"), dict) else None
    fields: dict[str, str] = {}
    notes: list[str] = []
    for k, v in (ams or {}).items():
        if _NOTIFY.match(k) or k in _SKIP:
            continue
        col = _RENAME.get(k, k)
        val = _clean(v) if not isinstance(v, (int, float)) else str(v)
        if val:
            fields[col] = val
    source = "ams_record" if ams else "blobs"

    # 货名：主单表没有 GOODS_INFO 列，公司把它放在外层 GOODS_NAME（尾巴常带 HS CODE）
    goods_name = _clean(mo.get("GOODS_NAME"))
    hs_from_name = ""
    if goods_name:
        m = _HS_TAIL.search(goods_name)
        if m:
            hs_from_name = re.sub(r"[^0-9,，]", "", m.group(1))
            goods_name = _HS_TAIL.sub("", goods_name).strip(" ,-/")
        fields.setdefault("GOODS_INFO", goods_name)
        if hs_from_name and not fields.get("GOODS_HS_CODE"):
            fields["GOODS_HS_CODE"] = hs_from_name

    # 发货人/收货人：已录入的用 AMS_RECORD，没录入的只从带标签的东西里拿
    for prefix, blob_key in (("SHIPPER_INFO_", "SHIPPER_INFO"), ("CONSIGNEE_INFO_", "CONSIGNEE_INFO")):
        blob = _clean(mo.get(blob_key))
        for col, val in _party_fields(blob, prefix).items():
            fields.setdefault(col, val)
        city_suffix = "CITTY" if prefix.startswith("CONSIGNEE") else "CITY"
        for suffix in ("COMP_NAME", "COMP_ADDRESS", city_suffix, "STATE", "POSTAL"):
            want = prefix + suffix
            if not fields.get(want):
                notes.append(f"{want} 公司侧未录入且无法从资料块可靠拆出：以票面为准，人工填")

    if not fields.get("GOODS_HS_CODE") and not ams:
        notes.append("GOODS_HS_CODE 公司侧没有值：以票面 HS 栏为准（EORI/VAT 同理，标签在货描里时按块归属判）")
    for col in MASTER_HAS_NO:
        if not fields.get(col):
            notes.append(f"{col}：公司主单表没有这一列，只能从分单票面来")
    return {"fields": fields, "notes": notes, "source": source}
