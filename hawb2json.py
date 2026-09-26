#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hawb2json.py — 航空分单 (HAWB / Air Waybill) 批量识别并导出统一 JSON

支持格式:
  .xlsx / .xlsm / .xls   模板直读单元格 (Neutral/DSV 模板, JRT 模板, 100% 准确)
  .pdf                   有文字层 -> 按坐标规则解析; 纯扫描件/解析差 -> 自动转 OCR
  .png/.jpg/.bmp/.tif    OCR 识别 (RapidOCR, 本地离线)

用法:
  python hawb2json.py 分单格式                     # 转整个目录, 输出到 <目录>/json
  python hawb2json.py 某文件.pdf -o out            # 转单个文件
  python hawb2json.py 分单格式 -o out --dpi 260    # 调整扫描件渲染精度
  python hawb2json.py 分单格式 --no-ocr            # 只处理表格/PDF文字层, 跳过OCR

输出:
  <输出目录>/<同名>.json   每个文件一份全字段 JSON
  <输出目录>/all_hawbs.json  汇总
  <输出目录>/_log.txt      转换日志与告警
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
import unicodedata
from pathlib import Path

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
XLS_EXTS = {".xlsx", ".xlsm", ".xls"}

# ---------------------------------------------------------------- schema ----

def new_doc(file: str, fmt: str) -> dict:
    return {
        "source": {"file": file, "format": fmt, "pages": 1, "extracted_by": "", "template": ""},
        "carrier_brand": None,
        "document_type": None,
        "awb_stock_number": None,
        "hawb_number": None,
        "barcode_number": None,
        "shipper": {"name": None, "address": None, "account_number": None,
                    "tax_id": None, "tel": None, "fax": None},
        "consignee": {"name": None, "address": None, "account_number": None,
                      "tax_id": None, "tel": None, "fax": None},
        "notify": {"name": None, "address": None},
        "issuing_agent": {"name": None, "address": None},
        "agents_iata_code": None,
        "account_no": None,
        "accounting_information": None,
        "reference_number": None,
        "optional_shipping_information": None,
        "airport_of_departure": None,
        "routing": [],
        "airport_of_destination": None,
        "flight_date": None,
        "currency": None,
        "chgs_code": None,
        "wt_val": {"ppd": None, "coll": None},
        "other_charges": {"ppd": None, "coll": None},
        "declared_value_carriage": None,
        "declared_value_customs": None,
        "amount_of_insurance": None,
        "handling_information": None,
        "sci": None,
        "cargo": [],
        "other_charges_list": [],
        "charges_summary": {
            "weight_charge": {"prepaid": None, "collect": None},
            "valuation_charge": {"prepaid": None, "collect": None},
            "tax": {"prepaid": None, "collect": None},
            "total_other_charges_due_agent": {"prepaid": None, "collect": None},
            "total_other_charges_due_carrier": {"prepaid": None, "collect": None},
            "total_prepaid": None,
            "total_collect": None,
            "currency_conversion_rates": None,
            "cc_charges_in_dest_currency": None,
            "charges_at_destination": None,
            "total_collect_charges": None,
        },
        "signature_of_shipper_or_agent": None,
        "executed_on": None,
        "executed_at": None,
        "signature_of_issuing_carrier_or_agent": None,
        "copy_type": None,
        "extras": {},
    }


def _norm_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else float(v)
    t = str(v).strip().replace(",", "")
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        f = float(t)
        return int(f) if f.is_integer() else f
    return None


def _clean(v):
    if v is None:
        return None
    t = re.sub(r"[ \t\u00a0]+", " ", str(v)).strip()
    return t or None


def _sq(t: str) -> str:
    """压平文本: 去掉所有非字母数字并转小写, 用于 OCR 无空格容错"""
    return re.sub(r"[^a-z0-9]", "", str(t).lower())


def _set_party(party: dict, block: str):
    """把整块文本拆成 name/address/tel/fax/tax_id"""
    for k in ("account_number", "tax_id", "tel", "fax"):
        party.setdefault(k, None)
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    lines = [re.sub(r"\bCO\.\d+,\s*", "CO., ", l) for l in lines]  # OCR: "CO.1," -> "CO., "
    if not lines:
        return
    nm, rest = lines[0], lines[1:]
    m_co = re.search(r"\bCO\.(\d+)$", nm)  # 行尾 "CO.1": 逗号被误读成 1
    mL = re.match(r"\s*(?:LTD|LIMITED)\b[,.]?\s*", rest[0], re.I) if rest else None
    if m_co and mL:  # "CO., LTD" 被断行: 修复并拼回公司名
        nm = nm[:m_co.start()] + "CO., " + mL.group(0).strip(" ,.")
        rest[0] = rest[0][mL.end():].strip()
    party["name"] = nm
    for l in rest:
        # TEL/FAX 共用标签 ("TEL/FAX.: +86..." / "FAX/TEL: ...") 一号两用: 各自独立匹配
        m_t = re.search(
            r"\b(?:TELEPHONE|PHONE|TEL|PH|TE)\b\.?\s*[/：:]?\s*(?:FAX)?\.?\s*[:：]?\s*(\+?[\d\-\s()]{6,})",
            l, re.I)
        m_f = re.search(
            r"\bFAX\b\.?\s*[/：:]?\s*(?:TEL|PH)?\.?\s*[:：]?\s*(\+?[\d\-\s()]{6,})", l, re.I)
        if m_t and not party["tel"]:
            party["tel"] = m_t.group(1).strip()
        if m_f and not party["fax"]:
            party["fax"] = m_f.group(1).strip()
    m = re.search(r"\b(USCI|CNPJ|EORI|TAX\s*ID|VAT)\b\s*[:：]?\s*([A-Z0-9]{6,})", block, re.I)
    if m:
        party["tax_id"] = m.group(2)
    rest = [l for l in rest if l]
    party["address"] = "\n".join(rest) if rest else None


CARRIER_KEYWORDS = [
    ("SCHENKER", "Schenker"), ("CEVA", "CEVA"), ("DSV", "DSV"),
    ("DHL", "DHL"), ("ROBINSON", "Robinson"), ("SAVINO DEL BENE", "Savino Del Bene"),
    ("PANALPINA", "Panalpina"), ("KUEHNE", "Kuehne+Nagel"),
    ("EXPEDITORS", "Expeditors"), ("DIMERCO", "Dimerco"), ("NIPPON EXPRESS", "Nippon Express"),
]
STOCK_PREFIX = {"999": "Air China", "074": "DSV(SACO)", "020": "Air China"}


def detect_brand(text: str):
    up = str(text).upper()
    for kw, brand in CARRIER_KEYWORDS:
        if kw in up:
            return brand
    for pfx, brand in STOCK_PREFIX.items():
        if re.search(rf"\b{pfx}\s*-?\s*(PEK|BJS|TSN|[A-Z]{{3}})?\s*\d", up):
            return brand
    return None


# ------------------------------------------------------------- Excel 通道 ----

def _xl_col_letter(c):
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def load_xlsx_cells(path: Path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = None
    for cand in wb.worksheets:
        if "air waybill" in cand.title.lower():
            ws = cand
            break
    if ws is None:
        ws = wb.worksheets[0]
    cells = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is not None and str(c.value).strip():
                cells[f"{c.coordinate}"] = str(c.value).replace("\r\n", "\n").strip()
    return cells, [s.title for s in wb.worksheets]


def load_xls_cells(path: Path):
    import xlrd
    wb = xlrd.open_workbook(str(path))
    ws = None
    for cand in wb.sheets():
        if "air waybill" in cand.name.lower():
            ws = cand
            break
    if ws is None:
        ws = wb.sheet_by_index(0)
    cells = {}
    for r in range(ws.nrows):
        for c in range(ws.ncols):
            v = ws.cell_value(r, c)
            if v not in (None, "") and str(v).strip():
                cells[f"{_xl_col_letter(c)}{r + 1}"] = str(v).replace("\r\n", "\n").strip()
    return cells, [s.name for s in wb.sheets()]


def _g(cells, addr):
    return cells.get(addr)


def _vh(cells, addrs):
    for a in addrs:
        v = _clean(_g(cells, a))
        if v:
            return v
    return None


def map_neutral(cells: dict, doc: dict):
    """Neutral HAWB (DSV 版式) xlsx"""
    doc["source"]["template"] = "Neutral HAWB (DSV)"
    doc["document_type"] = "HAWB"
    h = _g(cells, "AC1")
    if h:
        m = re.search(r"HAWB\s*No\.?:?\s*(\S+)", h, re.I)
        if m:
            doc["hawb_number"] = m.group(1)
    doc["awb_stock_number"] = _vh(cells, ["D1"])
    doc["extras"]["awb_stock_number_bottom"] = _vh(cells, ["AA61"])
    doc["extras"]["agent_account_top"] = _vh(cells, ["M2"])
    ship = "\n".join(x for x in (_g(cells, f"D{r}") for r in range(3, 7)) if x)
    _set_party(doc["shipper"], ship)
    con = "\n".join(x for x in (_g(cells, f"D{r}") for r in range(9, 14)) if x)
    _set_party(doc["consignee"], con)
    doc["consignee"]["account_number"] = _vh(cells, ["M8"])
    # 勾选框 Y14=WT/VAL PPD, Z14=WT/VAL COLL, AA14=Other PPD, AB14=Other COLL
    if (_g(cells, "Y14") or "").upper() == "Y":
        doc["wt_val"]["ppd"] = True
    if (_g(cells, "Z14") or "").upper() == "Y":
        doc["wt_val"]["coll"] = True
    if (_g(cells, "AA14") or "").upper() == "Y":
        doc["other_charges"]["ppd"] = True
    if (_g(cells, "AB14") or "").upper() == "Y":
        doc["other_charges"]["coll"] = True
    agent = "\n".join(x for x in (_g(cells, f"D{r}") for r in range(16, 19)) if x)
    _set_party(doc["issuing_agent"], agent)
    doc["accounting_information"] = " / ".join(
        x for x in (_clean(_g(cells, "U16")), _clean(_g(cells, "U17"))) if x) or None
    doc["reference_number"] = _vh(cells, ["U22"])
    doc["optional_shipping_information"] = _vh(cells, ["AD22"])
    doc["airport_of_departure"] = _vh(cells, ["D22"])
    routing = []
    if _clean(_g(cells, "D24")):
        routing.append({"to": _g(cells, "D24"), "by": _clean(_g(cells, "F24"))})
    if _clean(_g(cells, "N24")):
        routing.append({"to": _g(cells, "N24"), "by": _clean(_g(cells, "O24"))})
    doc["routing"] = routing
    doc["currency"] = _vh(cells, ["U24"])
    doc["chgs_code"] = _vh(cells, ["X24"])
    doc["declared_value_carriage"] = _vh(cells, ["AD24"])
    doc["declared_value_customs"] = _vh(cells, ["AH24"])
    doc["airport_of_destination"] = _vh(cells, ["D26"])
    doc["amount_of_insurance"] = _vh(cells, ["U26"])
    fl = [_clean(_g(cells, a)) for a in ("L26", "P26")]
    doc["flight_date"] = ", ".join(x for x in fl if x) or None
    doc["handling_information"] = _vh(cells, ["D27"])
    desc = []
    for r in range(32, 44):
        v = _clean(_g(cells, f"AF{r}"))
        if v:
            desc.append(v)
    cargo = {
        "pieces": _norm_num(_g(cells, "D32")),
        "gross_weight": _norm_num(_g(cells, "G32")),
        "weight_unit": _vh(cells, ["H32"]),
        "rate_class": _vh(cells, ["J32"]),
        "commodity_item_no": _vh(cells, ["M32", "N32"]),
        "chargeable_weight": _norm_num(_g(cells, "Q32")),
        "rate": _norm_num(_g(cells, "U32")),
        "total": _norm_num(_g(cells, "AD32")),
        "description": "\n".join(desc) or None,
    }
    dims = re.findall(r"(\d+\s*[xX×]\s*\d+\s*[xX×]\s*\d+(?:\s*CM)?)", "\n".join(desc))
    mv = re.search(r"VOL\.?\s*W(?:EIGHT|GHT)\.?[:\s]*([\d.]+)", "\n".join(desc), re.I)
    vol = re.search(r"VOL(?:UME)?\.?[:\s]*([\d.]+)\s*(CBM|M3)?", "\n".join(desc), re.I)
    hs = re.findall(r"HS\s*Codes?\s*[:：]?\s*([\d, ]+)", "\n".join(desc), re.I)
    cargo["dims"] = dims or None
    cargo["volume_weight"] = _norm_num(mv.group(1)) if mv else None
    cargo["volume"] = vol.group(1) + (vol.group(2) or "") if vol else None
    cargo["hs_codes"] = hs or None
    doc["cargo"] = [cargo]
    cs = doc["charges_summary"]
    cs["weight_charge"]["prepaid"] = _norm_num(_g(cells, "K46"))
    cs["total_other_charges_due_carrier"]["prepaid"] = _norm_num(_g(cells, "K52"))
    cs["total_prepaid"] = _norm_num(_g(cells, "K58"))
    q46 = _clean(_g(cells, "Q46"))
    if q46:
        m = re.match(r"([A-Z]{2,4})\s+(.*?)\s+([\d.]+)$", q46)
        if m:
            doc["other_charges_list"].append(
                {"code": m.group(1), "note": m.group(2), "amount": _norm_num(m.group(3))})
        else:
            doc["extras"]["other_charges_note"] = q46
    doc["signature_of_shipper_or_agent"] = _vh(cells, ["Q55"])
    doc["extras"]["signature_of_shipper_block"] = " / ".join(
        x for x in (_clean(_g(cells, "Q53")), _clean(_g(cells, "Q54"))) if x) or None
    doc["executed_on"] = _vh(cells, ["Q59"])
    doc["executed_at"] = _vh(cells, ["U59"])
    doc["signature_of_issuing_carrier_or_agent"] = _vh(cells, ["AD59"])
    doc["carrier_brand"] = detect_brand(json.dumps(doc, ensure_ascii=False)) or "DSV"


def map_jrt(cells: dict, doc: dict):
    """JRT 副本带格分单 xls (列号见模板)"""
    doc["source"]["template"] = "JRT 分单"
    doc["document_type"] = "HAWB"
    prefix3 = _norm_num(_g(cells, "E2"))
    doc["extras"]["top_agent_no"] = prefix3
    doc["extras"]["top_route_code"] = _vh(cells, ["H2"])
    doc["airport_of_departure"] = _vh(cells, ["E27"])
    stock8 = _norm_num(_g(cells, "J2"))
    # JRT 模板主单号拆两格: E2=3位承运人前缀(如125英航) + J2=8位号码 -> 125-55888770
    if prefix3 and re.fullmatch(r"\d{3}", str(prefix3)) and stock8 and re.fullmatch(r"\d{8}", str(stock8)):
        doc["awb_stock_number"] = f"{prefix3}-{stock8}"
    else:
        doc["awb_stock_number"] = stock8
    doc["hawb_number"] = _vh(cells, ["AQ2"])
    ship = "\n".join(x for x in (_g(cells, f"E{r}") for r in range(6, 8)) if x)
    _set_party(doc["shipper"], ship)
    con = _g(cells, "E13")
    _set_party(doc["consignee"], con or "")
    if _g(cells, "AF20"):
        doc["accounting_information"] = _clean(_g(cells, "AF21"))
    routing = []
    if _clean(_g(cells, "E30")):
        routing.append({"to": _clean(_g(cells, "E30")), "by": _clean(_g(cells, "H30"))})
    doc["routing"] = routing
    doc["currency"] = _vh(cells, ["AF30"])
    if (_clean(_g(cells, "AK30")) or "").upper() in ("P", "X"):
        doc["wt_val"]["ppd"] = True
    if (_clean(_g(cells, "AM30")) or "").upper() in ("P", "X"):
        doc["other_charges"]["ppd"] = True
    doc["declared_value_carriage"] = _vh(cells, ["AP30"])
    doc["declared_value_customs"] = _vh(cells, ["AT30"])
    doc["airport_of_destination"] = _vh(cells, ["E32"])
    doc["flight_date"] = _vh(cells, ["T32"])
    doc["extras"]["carrier_use_no"] = _norm_num(_g(cells, "X32"))
    doc["amount_of_insurance"] = _vh(cells, ["AE32"])
    doc["handling_information"] = re.sub(r"^Handling\s+Information\s*", "", _vh(cells, ["E33"]) or "") or None
    desc = []
    for r in range(40, 52):
        v = _clean(_g(cells, f"AR{r}"))
        if v:
            desc.append(v)
    cargo = {
        "pieces": _norm_num(_g(cells, "E40")),
        "gross_weight": _norm_num(_g(cells, "L40")),
        "weight_unit": _vh(cells, ["M40"]),
        "rate_class": _vh(cells, ["O40"]),
        "commodity_item_no": _vh(cells, ["P40"]),
        "chargeable_weight": _norm_num(_g(cells, "Y40")),
        "rate": _norm_num(_g(cells, "AA40")),
        "total": _clean(_g(cells, "AP40")),
        "description": "\n".join(desc) or None,
    }
    dims = re.findall(r"(\d+\s*[xX×*]\s*\d+\s*[xX×*]\s*\d+\s*/\s*\d+)", "\n".join(desc))
    vol = re.search(r"VOL\.?[:\s]*([\d.]+)\s*(M3|CBM)?", "\n".join(desc), re.I)
    hs = re.findall(r"HS\s*CODE\s*[:：]?\s*([\d,]+)", "\n".join(desc), re.I)
    cargo["dims"] = dims or None
    cargo["volume"] = vol.group(1) + (vol.group(2) or "") if vol else None
    cargo["hs_codes"] = hs or None
    doc["cargo"] = [cargo]
    cs = doc["charges_summary"]
    cs["cc_charges_in_dest_currency"] = _norm_num(_g(cells, "Y68"))
    doc["copy_type"] = _vh(cells, ["P74", "O74"])
    doc["carrier_brand"] = "JRT" if (doc["hawb_number"] or "JRT").startswith("JRT") else detect_brand(json.dumps(doc))


def map_toyo(cells: dict, doc: dict):
    """TOYO INK 版式 xlsx：MAWB 在 D1，HAWB 在 W1，发货人 A3，收货人 A8。"""
    doc["source"]["template"] = "TOYO INK"
    doc["document_type"] = "HAWB"
    doc["awb_stock_number"] = _clean(_g(cells, "D1"))
    doc["hawb_number"] = _clean(_g(cells, "W1"))

    ship = _g(cells, "A3") or ""
    _set_party(doc["shipper"], ship)
    con = _g(cells, "A8") or ""
    _set_party(doc["consignee"], con)

    doc["handling_information"] = _clean(_g(cells, "S12"))
    if doc["handling_information"]:
        notify = re.search(r"NOTIFY\s*[:：]\s*(.+)", doc["handling_information"], re.I)
        if notify:
            doc["notify"]["name"] = notify.group(1).strip()

    doc["airport_of_departure"] = _clean(_g(cells, "A14"))
    routing = []
    to1, by1 = _clean(_g(cells, "A16")), _clean(_g(cells, "C16"))
    if to1:
        routing.append({"to": to1, "by": by1})
    l16 = _clean(_g(cells, "L16"))
    if l16:
        m = re.match(r"([A-Z]{3})\s+([A-Z0-9]{2})", l16)
        if m:
            routing.append({"to": m.group(1), "by": m.group(2)})
        else:
            routing.append({"to": l16.split()[0], "by": None})
    doc["routing"] = routing
    doc["airport_of_destination"] = _clean(_g(cells, "A18"))
    doc["flight_date"] = _clean(_g(cells, "K18"))

    ref = _clean(_g(cells, "A20"))
    if ref:
        m = re.search(r"INVOICE\s*NO\.?\s*[:：]?\s*(\S+)", ref, re.I)
        doc["reference_number"] = m.group(1) if m else ref

    desc = _clean(_g(cells, "AD22"))
    cargo = {
        "pieces": _norm_num(_g(cells, "A22")),
        "gross_weight": _norm_num(_g(cells, "D22")),
        "weight_unit": "K",
        "chargeable_weight": _norm_num(_g(cells, "L22")),
        "rate": _norm_num(_g(cells, "O22")),
        "description": desc,
    }
    hs = re.findall(r"NCM\s*[:：]?\s*([\d.,]+)", desc or "", re.I)
    if hs:
        cargo["hs_codes"] = hs
    doc["cargo"] = [cargo]

    m43 = _clean(_g(cells, "M43"))
    if m43:
        dm = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", m43)
        if dm:
            doc["executed_on"] = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        rest = re.sub(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}\s*", "", m43).strip()
        parts = re.split(r"\s{2,}", rest)
        if parts:
            doc["executed_at"] = parts[0] if len(parts) > 1 else None
            doc["signature_of_shipper_or_agent"] = parts[-1] if parts[-1] != doc["executed_at"] else None

    doc["currency"] = _clean(_g(cells, "R16"))
    doc["carrier_brand"] = "TOYO INK"


def extract_excel(path: Path):
    if path.suffix.lower() == ".xls":
        cells, sheets = load_xls_cells(path)
    else:
        cells, sheets = load_xlsx_cells(path)
    all_text = " ".join(cells.values())
    if re.search(r"HAWB\s*No\.?:", all_text, re.I):
        mapper = map_neutral
    elif any("air waybill" in s.lower() for s in sheets) and cells:
        mapper = map_jrt
    elif _g(cells, "D1") and _g(cells, "W1") and re.fullmatch(r"\d{3}[- ]\d{8}", str(_g(cells, "D1")).strip()):
        # TOYO INK 版式：D1=MAWB(3位前缀-8位号码)，W1=HAWB
        mapper = map_toyo
    else:
        mapper = None
    doc = new_doc(path.name, path.suffix.lstrip(".").lower())
    doc["source"]["sheets"] = sheets
    if mapper:
        try:
            mapper(cells, doc)
            doc["extras"]["raw_cells"] = cells
            return doc
        except Exception as e:
            doc["source"]["warnings"] = [f"template mapping failed: {e}"]
    doc["source"]["template"] = "unknown"
    doc["extras"]["raw_cells"] = cells
    doc["source"].setdefault("warnings", []).append("unknown template, raw cells dumped to extras")
    return doc


# --------------------------------------------------- 坐标盒子通用解析引擎 ----
# 标签匹配基于"压平文本"(无空格无标点小写), 对 OCR 丢空格免疫。

def _E(lit):            # exact
    return ("E", re.compile(r"^" + re.escape(_sq(lit)) + r"$"))
def _S(rx):             # substring on squashed text
    return ("S", re.compile(rx) if isinstance(rx, str) else rx)

LABELS = {
        # 宽容 OCR 水印污染: 正常 / "ShipeNmeandAdress" / "ShPeWFSNmeandAdress"
    "shipper":      _S(r"shippers?name(?:and|&)?ad[a-z]{0,2}ress|ship[a-z]{1,5}andad[a-z]{0,2}ress|sh[a-z]{0,8}(?:and)?ad[a-z]{0,2}ress"),
    "consignee":    _S(r"consignees?name(?:and|&)?ad[a-z]{0,2}ress"),
    "notify":       _S(r"^notif(y|ication)party$|^notify$"),
    "agent":        _S(r"issuingcarriersagentnameandcity"),
    "accounting":   _S(r"accountinginformation"),
    "iata":         _S(r"agentsiatacode"),
    "accountno":    _E("account no"),
    "dep":          _S(r"a[l1ir]{1,2}portofdep[a-z]{0,3}ture|airportofdeparture"),
    "dest":         _S(r"a[l1ir]{1,2}portofdest[a-z]{0,3}ation|airportofdestination"),
    "flight":       _E("flight/date"),
    "handling":     _S(r"handlinginformation"),
    "ref":          _S(r"referencenumber"),
    "optional":     _S(r"optionalshippinginformation"),
    "currency":     _E("currency"),
    "chgs":         _E("chgs"),
    "wtval":        _E("wt/val"),
    "otherchk":     _E("other"),
    "dvcarriage":   _S(r"declaredvalueforcarriage|dvforcarriage"),
    "dvcustoms":    _S(r"declaredvalueforcustoms"),
    "insurance":    _S(r"amountofinsurance|amountofinsur"),
    "pieces":       _S(r"no\.?ofpieces|numberofpieces"),
    "gross":        _S(r"grossweight|grosswght"),
    "rateclass":    _S(r"^rateclass$"),
    "commodity":    _S(r"commodityitemno"),
    "chargeable":   _S(r"chargeableweight"),
    "ratecharge":   _S(r"^ratecharge$|^rate$"),
    "totalcol":     _E("total"),
    "nature":       _S(r"natureandquantityofgoods"),
    "prepaid":      _E("prepaid"),
    "collect":      _E("collect"),
    "weightcharge": _S(r"weightcharge"),
    "valuation":    _S(r"valuationcharge"),
    "tax":          _E("tax"),
    "dueagent":     _S(r"totalotherchargesdueagent"),
    "duecarrier":   _S(r"totalotherchargesduecarrier"),
    "totalprepaid": _S(r"totalprepaid"),
    "totalcollect": _S(r"totalcollect(?!charges)"),
    "ccr":          _S(r"currencyconversionrates"),
    "cccharges":    _S(r"ccchargesindest"),
    "chargesdest":  _S(r"chargesatdestination"),
    "totalcollectcharges": _S(r"totalcollectcharges"),
    "executed":     _S(r"executedon"),
    "atplace":      _E("at (place)"),
    "sigs":         _S(r"signatureofshipper"),
    "sigc":         _S(r"signatureofissuingcarrier"),
    "sci":          _E("sci"),
    "hawbno":       _S(r"^hawbno"),
    "notneg":       _S(r"notnegotiable"),
    "certifies":    _S(r"shippercertifies"),
    "othercharges": _E("other charges"),
    "carrieruse":   _S(r"forcarriers?useonly"),
    "copyorig":     _S(r"original\d*forconsignee|^original\d"),
    "emailcopy":    _S(r"emailcopy"),
    "storecopy":    _S(r"storecopy"),
}
_LABEL_ALL = re.compile("|".join(f"(?:{rx.pattern})" for _, rx in LABELS.values()))


def _same_line(a, b, tol=0.65):
    ha = a["y1"] - a["y0"]
    hb = b["y1"] - b["y0"]
    return abs((a["y0"] + a["y1"]) / 2 - (b["y0"] + b["y1"]) / 2) <= max(tol * max(ha, hb), 5)


def _group_lines(boxes):
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: (b["y0"] + b["y1"]) / 2)
    lines = []
    for b in boxes:
        placed = False
        for ln in lines:
            if _same_line(ln[-1], b):
                ln.append(b)
                placed = True
                break
        if not placed:
            lines.append([b])
    lines.sort(key=lambda ln: min(b["y0"] for b in ln))
    for ln in lines:
        ln.sort(key=lambda b: b["x0"])
    return lines


def _windows(boxes, pw):
    """生成用于标签匹配的候选窗口: 同行连续拼接 + 上下两行拼接"""
    order = sorted(boxes, key=lambda b: (b["y0"], b["x0"]))
    wins = []
    for i, b in enumerate(order):
        grp = [b]
        for j in range(i + 1, min(i + 7, len(order))):
            c = order[j]
            gap = c["x0"] - grp[-1]["x1"]
            if _same_line(b, c) and -3 <= gap < 0.015 * pw:
                grp.append(c)
            else:
                break
        for k in range(1, len(grp) + 1):
            wins.append(grp[:k])
        # 竖排拼接: 下方与当前 x 重叠且贴近的框 (允许轻微重叠)
        hh = grp[-1]["y1"] - grp[-1]["y0"]
        below = next((c for c in order[i + 1:i + 12]
                      if c["x0"] < b["x1"] and c["x1"] > b["x0"]
                      and -0.8 * hh <= c["y0"] - grp[-1]["y1"] < max(14, 1.2 * hh)), None)
        if below is not None:
            wins.append(grp + [below])
    return wins


def find_labels(boxes, pw):
    labs = {k: [] for k in LABELS}
    for win in _windows(boxes, pw):
        txt = " ".join(b["text"] for b in win)
        s = _sq(txt)
        for name, (mode, rx) in LABELS.items():
            if rx.search(s):
                u = {"text": txt, "wlen": len(txt),
                     "x0": min(b["x0"] for b in win), "y0": min(b["y0"] for b in win),
                     "x1": max(b["x1"] for b in win), "y1": max(b["y1"] for b in win)}
                dup = next((e for e in labs[name]
                            if abs(u["x0"] - e["x0"]) < 3 and abs(u["y0"] - e["y0"]) < 3), None)
                if dup is None:
                    labs[name].append(u)
                elif u["wlen"] < dup["wlen"]:
                    labs[name][labs[name].index(dup)] = u
    return labs


def _labelish(b) -> bool:
    s = _sq(b["text"])
    return bool(s) and bool(_LABEL_ALL.search(s)) and len(s) <= 60


def _region_text(boxes, x0, y0, x1, y1, exclude=None, drop_labels=True, as_lines=False):
    T = 4
    ex = exclude or []
    def _vert_ok(b):
        # 起点在线下 4px 内, 或框的主体(中心)越过起始线 2px (OCR 首行与标签底部轻微重叠)
        return b["y0"] >= y0 - T or (b["y0"] + b["y1"]) / 2 > y0 - 2
    sel = [b for b in boxes
           if b["x0"] >= x0 - T and b["x1"] <= x1 + T and _vert_ok(b) and b["y1"] <= y1 + T
           and not any(b is e or (abs(b["x0"] - e["x0"]) < 2 and abs(b["y0"] - e["y0"]) < 2) for e in ex)]
    if drop_labels:
        sel = [b for b in sel if not _labelish(b)]
    lines = _group_lines(sel)
    texts = [" ".join(b["text"] for b in ln) for ln in lines]
    return texts if as_lines else "\n".join(texts)


def _is_number(txt):
    return bool(re.fullmatch(r"[\d,]+\.?\d{0,3}", str(txt).replace(" ", "")))


def _first_number(boxes, x0, y0, x1, y1, exclude=()):
    T = 4
    cands = [b for b in boxes
             if b["x0"] >= x0 - T and b["x1"] <= x1 + T and b["y0"] >= y0 - T and b["y1"] <= y1 + T
             and _is_number(b["text"])
             and not any(abs(b["x0"] - e["x0"]) < 3 and abs(b["y0"] - e["y0"]) < 3 for e in exclude)]
    if not cands:
        return None
    cands.sort(key=lambda b: (b["y0"], b["x0"]))
    return _norm_num(cands[0]["text"])


def _label_first(labs, name):
    ls = labs.get(name) or []
    if not ls:
        return None
    # 优先最短窗口(避免跨列粘连的表头), 再按位置
    return sorted(ls, key=lambda b: (b.get("wlen", 0), b["y0"], b["x0"]))[0]


def _vals_near(boxes, lb, dy=0.05, dxl=0.22, dxr=0.18):
    """标签同一行左/右、正下方找数字, 按距离排序"""
    ph_hint = max(8, (lb["y1"] - lb["y0"]))
    cands = []
    for b in boxes:
        if not _is_number(b["text"]):
            continue
        same_line = _same_line(b, lb, 0.9)
        below = lb["y1"] - 2 <= b["y0"] <= lb["y1"] + 0.05 * (lb["y1"] - lb["y0"] + 40)
        if same_line and (lb["x0"] - dxl * 1000 * 0.28 <= b["x1"] <= lb["x0"] - 2 or lb["x1"] + 2 <= b["x0"] <= lb["x1"] + dxr * 1000 * 0.28):
            cands.append((0, b))
        elif below and lb["x0"] - 0.05 * 1000 * 0.28 <= b["x0"] and b["x1"] <= lb["x1"] + dxr * 1000 * 0.28:
            cands.append((1, b))
    cands.sort(key=lambda t: (t[0], abs((t[1]["y0"] + t[1]["y1"]) / 2 - (lb["y0"] + lb["y1"]) / 2)))
    return [_norm_num(b["text"]) for _, b in cands]


def assemble_from_boxes(boxes, pw, ph, doc):
    """从带坐标的文本盒子解析 IATA HAWB 全字段"""
    labs = find_labels(boxes, pw)
    mid = pw * 0.47
    full_text = "\n".join(b["text"] for b in boxes)
    # 页底 customs 区的 EORI/VAT 文本 (不在收发货人块内, 供 project_raw doc 级兜底)
    tax_hits = [b["text"] for b in boxes if re.search(r"\bEORI\b|\bV\.?A\.?T\.?\b", b["text"], re.I)]
    if tax_hits:
        doc["tax_note_text"] = " | ".join(tax_hits)

    doc["carrier_brand"] = detect_brand(full_text)
    for key in ("copyorig", "emailcopy", "storecopy"):
        if labs.get(key):
            doc["copy_type"] = labs[key][0]["text"]

    # --- 顶部编号 (按行分组匹配, 避免不同行文本交错) ---
    top = [b for b in boxes if b["y1"] < ph * 0.09]
    top_lines = [" ".join(b["text"] for b in ln) for ln in _group_lines(top)]
    topj = " ".join(top_lines)
    mawb_rx = r"(\d{2,3})\s*[-\s|]\s*([A-Z]{3})?\s*(\d{4})\s*[-\s|]?(\d{4})"
    mawb35_rx = r"(\d{2,3})\s*[-\s|]\s*([A-Z]{3})\s*[-\s|]?(\d{4,5})\b"
    for tl in top_lines + [topj]:
        m = re.search(mawb_rx, tl)
        if m:
            doc["awb_stock_number"] = f"{m.group(1)}-{(m.group(2) or '')}{m.group(3)}{m.group(4)}"
            break
        m = re.search(mawb35_rx, tl)
        if m:  # 074-PEK-68614 / 131-WEH330596 式
            doc["awb_stock_number"] = f"{m.group(1)}-{m.group(2)}{m.group(3)}"
            break
    for b in top:
        d = re.sub(r"\D", "", b["text"])
        if len(d) >= 12 and (b["x1"] - b["x0"]) > 0.25 * pw:
            doc["barcode_number"] = d
            break
    m = re.search(r"HAWB\s*(?:No\.?)?\s*[:：]?\s*([A-Z][A-Z0-9\-]{5,14})", full_text, re.I)
    if m and _sq(m.group(1)) not in ("hawbno",):
        doc["hawb_number"] = m.group(1).strip("-")
    if not doc["hawb_number"]:
        m = re.search(r"\b([A-Z]{3}\d{7})\b", full_text)
        if m:
            doc["hawb_number"] = m.group(1)
    # "House Air Waybill NO AIRHEL050370" 式标注 (\s* 容忍 OCR 丢空格)
    if not doc["hawb_number"] and re.search(r"House\s*Air\s*Waybill", topj):
        m = re.search(r"\bN[Oo]\s*[:：]?\s*([A-Z][A-Z0-9\-]{5,14})\b", topj)
        if m and _sq(m.group(1)) not in ("negotiable", "house", "airwaybill"):
            doc["hawb_number"] = m.group(1).strip("-")
    # 顶栏 "TA0-5808 9795" / "SIA-82405250" 式分单号 (首字母+字母数字、4+4位，
    # OCR 常把 O 读成 0、连字符/空格丢失)
    if not doc["hawb_number"]:
        m = re.search(r"\b([A-Z][A-Z0-9]{2})[-\s]?(\d{4})[\s-]?(\d{4})\b", topj)
        if m:
            doc["hawb_number"] = f"{m.group(1)}-{m.group(2)}{m.group(3)}"
    # 顶栏纯数字stock号本身就是本分单单号 (如 131-33062536 / 999-30825351)
    if not doc["hawb_number"] and doc["awb_stock_number"] and re.fullmatch(
            r"\d{3}-?\d{8}", str(doc["awb_stock_number"]).replace(" ", "")):
        doc["hawb_number"] = doc["awb_stock_number"]

    # --- 收发货人区块 ---
    def block_below(label_name, col, stop_names):
        lb = _label_first(labs, label_name)
        if not lb:
            return None
        stops = [labs[s][0] for s in stop_names
                 if labs.get(s) and labs[s][0]["y0"] > lb["y1"] + 2]
        bottom = min((s["y0"] for s in stops), default=ph)
        # 左列(收发货人地址)常横跨中线, 加宽到 0.52pw 避免长地址行被截
        x0, x1 = (0, pw * 0.52) if col == "L" else (pw * 0.47, pw)
        return _region_text(boxes, x0, lb["y1"] + 1, x1, bottom, exclude=[lb])

    ship_blk = block_below("shipper", "L", ["consignee", "notify", "agent", "iata"])
    if ship_blk:
        _set_party(doc["shipper"], ship_blk)
    con_blk = block_below("consignee", "L", ["notify", "agent", "iata"])
    if con_blk:
        # 传真行常被下方 Agent 标签窗口吞并, 在区块紧下方补捞 FAX 行
        lb2 = _label_first(labs, "consignee")
        _stops = [labs[s][0] for s in ("notify", "agent", "iata")
                  if labs.get(s) and labs[s][0]["y0"] > lb2["y1"] + 2]
        _bottom = min((s["y0"] for s in _stops), default=ph)
        _zone = [b for b in boxes
                 if b["x1"] <= pw * 0.52 and _bottom - 2 <= b["y0"] and b["y1"] <= _bottom + 0.045 * ph
                 and (re.fullmatch(r"FAX\W*", b["text"], re.I) or re.search(r"\+\d[\d\-() ]{5,}", b["text"]))
                 and not re.search(r"agent|notify|carrier", b["text"], re.I)]
        if _zone:
            _zone.sort(key=lambda b: b["x0"])
            con_blk += "\n" + " ".join(b["text"] for b in _zone)
        _set_party(doc["consignee"], con_blk)
    ag_blk = block_below("agent", "L", ["iata", "dep"])
    if ag_blk:
        _set_party(doc["issuing_agent"], ag_blk)

    iata = _label_first(labs, "iata")
    if iata:
        doc["agents_iata_code"] = _region_text(boxes, iata["x0"] - 0.02 * pw, iata["y1"] + 1, iata["x1"] + 0.22 * pw, iata["y1"] + max(10, 0.022 * ph), exclude=[iata]) or None
    accno = _label_first(labs, "accountno")
    if accno:
        doc["account_no"] = _region_text(boxes, accno["x1"] + 2, accno["y0"] - 3, accno["x1"] + 0.22 * pw, accno["y1"] + 3, exclude=[accno]) or None

    acc = _label_first(labs, "accounting")
    if acc:
        stops = [labs[s][0] for s in ("dep", "ref", "optional")
                 if labs.get(s) and labs[s][0]["y0"] > acc["y1"] + 2]
        bottom = min((s["y0"] for s in stops), default=acc["y1"] + 0.08 * ph)
        doc["accounting_information"] = _region_text(boxes, mid, acc["y1"] + 1, pw, bottom, exclude=[acc]) or None

    ref = _label_first(labs, "ref")
    if ref:
        doc["reference_number"] = _region_text(boxes, ref["x0"] - 0.06 * pw, ref["y1"] + 1, ref["x1"] + 0.12 * pw, ref["y1"] + 0.042 * ph, exclude=[ref]) or None
    opt = _label_first(labs, "optional")
    if opt:
        doc["optional_shipping_information"] = _region_text(boxes, opt["x1"] + 2, opt["y0"] - 2, pw, opt["y1"] + 0.035 * ph, exclude=[opt]) or None

    dep = _label_first(labs, "dep")
    if dep:
        hdrs = [b for b in boxes if b["y0"] > dep["y1"] and _sq(b["text"]) in
                ("to", "byfirstcarrier", "routinganddestination", "to1", "by")]
        bottom = min((b["y0"] for b in hdrs), default=dep["y1"] + 0.045 * ph)
        doc["airport_of_departure"] = _region_text(boxes, 0, dep["y1"] + 1, mid, bottom, exclude=[dep, *hdrs]) or None

    dest = _label_first(labs, "dest")
    if dest:
        # 目的港值通常在标签正下方；右边界取同行下一个表头标签
        nxt = [b for b in boxes if b is not dest and _same_line(b, dest, 0.5) and b["x0"] > dest["x1"] + 2]
        right = min((b["x0"] for b in nxt), default=dest["x0"] + 0.14 * pw)
        doc["airport_of_destination"] = _region_text(
            boxes, 0, dest["y1"] - 12, right - 2, dest["y1"] + 0.048 * ph,
            exclude=[dest]) or None
        if doc["airport_of_destination"]:
            keep = [ln for ln in doc["airport_of_destination"].split("\n")
                    if not re.search(r"hand[a-z]{0,4}information|^(airport)?of?destination$", _sq(ln))]
            val = "\n".join(keep)
            # 清理混入的航班号碎片，如 "MXP-MILAN 949/02"
            val = re.sub(r"\b\w?\d{2,4}\s*/\s*\d{2,4}\b", "", val)
            val = re.sub(r"(?<![A-Z0-9])\d{2,4}(?![A-Z0-9])", "", val).strip()
            doc["airport_of_destination"] = val or None
    # --- 航班号 ---
    fl_cands = []
    if dest:
        fl_cands = [b for b in boxes
                    if dest["y0"] - 0.02 * ph <= b["y0"] <= dest["y1"] + 0.06 * ph
                    and not (b["x1"] < dest["x0"] + 2 and b["x0"] > dest["x0"] - 0.35 * pw and _same_line(b, dest, 0.5))
                    and b["x1"] > dest["x0"] and b is not dest]
        if not fl_cands:
            fl_cands = [b for b in boxes if _same_line(b, dest, 0.8) and b["x1"] < dest["x0"] - 2 and b is not dest]
    flj = " ".join(b["text"] for b in sorted(fl_cands, key=lambda b: (b["y0"], b["x0"])))
    m = re.search(r"\b([A-Z]{2}\s?\d{3,4}(?:\s*/\s*/?\s*\d+)+)", flj)
    if not m:
        m = re.search(r"\b([A-Z]{2}\d{3,4}(?:/\d+)?)\b", flj)
    doc["flight_date"] = m.group(1).replace(" ", "") if m else None
    dm = re.search(r"(?<!\d)\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}(?!\d)"
                  r"|(?<!\d)\d{1,2}[\s-][A-Z]{3}[A-Za-z ,\-]+\d{4}(?!\d)"
                  r"|[A-Z]{3}[\s.,]+\d{1,2}[A-Za-z ,]*\d{4}(?!\d)", flj)
    if dm:
        doc["extras"]["flight_date_raw"] = dm.group(0).strip()

    # --- 航路 ---
    if dep:
        rt_top = dep["y1"] + 1
        rt_bot = dest["y0"] - 1 if dest else ph * 0.42
        band = [b for b in boxes if rt_top <= b["y0"] and b["y1"] <= rt_bot + 8]
        bandj = " ".join(b["text"] for b in sorted(band, key=lambda b: (b["y0"], b["x0"])))
        routing = [{"to": a, "by": c} for a, c in re.findall(
            r"\b([A-Z]{3})\s+([A-Z]{2})(?=\s*\d|\s|$)", bandj)
            if c not in ("AI", "LI", "CO", "LT", "CC", "PP", "XX")
            and a not in ("USD", "NVD", "NCV", "NVC", "PPD", "COLL", "SCI", "COD", "WTV", "CHG",
                          "CNY", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "HKD", "SGD",
                          "SEK", "DKK", "NOK", "PLN", "CZK", "HUF", "RON", "INR", "KRW")]
        if not routing and any("first" in b["text"].lower() for b in band):
            # “to by First Carrier” 场景：按第一个 to 表头框定位数据行，
            # 收集其下方的孤立三字码为 to 点，by 找右侧 2 字母码
            tos = [b for b in band if re.fullmatch(r"(?i)to", b["text"].strip())]
            t0 = min(tos, key=lambda b: (b["y0"], b["x0"])) if tos else None
            if t0:
                zone = [b for b in band if t0["y1"] - 2 < b["y0"] < t0["y1"] + 0.04 * ph
                        and re.fullmatch(r"[A-Z]{3}", b["text"].strip())
                        and b["text"].strip() not in ("USD", "NVD", "NCV", "NVC", "PPD", "COLL", "SCI", "COD", "AIR",
                                                       "CNY", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "HKD",
                                                       "SGD", "SEK", "DKK", "NOK", "PLN", "CZK", "HUF", "INR")]
                rt = []
                for zb in sorted(zone, key=lambda b: (b["y0"], b["x0"]))[:2]:
                    byb = [b for b in band if _same_line(b, zb, 0.6) and b["x0"] > zb["x1"]
                           and re.fullmatch(r"[A-Z]{2}", b["text"].strip())]
                    rt.append({"to": zb["text"].strip(), "by": byb[0]["text"].strip() if byb else None})
                if rt and rt[0]["by"] is None:
                    mm = re.match(r"([A-Z]{2})", doc.get("flight_date") or "")
                    rt[0]["by"] = mm.group(1) if mm else "First Carrier"
                routing = rt
        if routing:
            doc["routing"] = routing[:4]

    cur = _label_first(labs, "currency")
    if cur:
        txts = _region_text(boxes, cur["x0"] - 0.02 * pw, cur["y1"] + 1, cur["x1"] + 0.1 * pw, cur["y1"] + 0.035 * ph,
                            exclude=[cur], as_lines=True)
        val = None
        for t in txts:
            m = re.search(r"\b([A-Z]{3})\b", t)
            if m and m.group(1) not in ("PPD", "COLL"):
                val = m.group(1)
                break
        doc["currency"] = val
    chgs = _label_first(labs, "chgs")
    if chgs:
        t = _region_text(boxes, chgs["x0"] - 0.01 * pw, chgs["y1"] + 1, chgs["x1"] + 0.05 * pw, chgs["y1"] + 0.035 * ph, exclude=[chgs])
        m = re.search(r"\b([A-Z]{1,2}\d?)\b", t or "")
        doc["chgs_code"] = m.group(1) if m else None
    # WT/VAL 与 Other 的 PPD/COLL 标记
    wtval = _label_first(labs, "wtval")
    otherc = _label_first(labs, "otherchk")
    dvc = _label_first(labs, "dvcarriage")
    marks = [b for b in boxes if re.fullmatch(r"[XxPp]", b["text"])]
    def marks_in(x0, x1, ytop):
        sel = [b for b in marks if x0 <= (b["x0"] + b["x1"]) / 2 <= x1 and ytop <= b["y0"] <= ytop + 0.05 * ph]
        return bool(sel)
    if wtval:
        right_edge = otherc["x0"] if otherc else wtval["x1"] + 0.16 * pw
        band_mid = (wtval["x0"] + right_edge) / 2
        ytop = wtval["y1"] - 2
        doc["wt_val"]["ppd"] = marks_in(wtval["x0"] - 10, band_mid, ytop) or None
        doc["wt_val"]["coll"] = marks_in(band_mid, right_edge, ytop) or None
    if otherc:
        right_edge = dvc["x0"] if dvc else otherc["x1"] + 0.16 * pw
        band_mid = (otherc["x0"] + right_edge) / 2
        ytop = otherc["y1"] - 2
        doc["other_charges"]["ppd"] = marks_in(otherc["x0"] - 10, band_mid, ytop) or None
        doc["other_charges"]["coll"] = marks_in(band_mid, right_edge, ytop) or None
    if dvc:
        doc["declared_value_carriage"] = _region_text(boxes, dvc["x0"] - 0.02 * pw, dvc["y1"] + 1, dvc["x1"] + 0.07 * pw, dvc["y1"] + 0.035 * ph, exclude=[dvc]) or None
    dvk = _label_first(labs, "dvcustoms")
    if dvk:
        doc["declared_value_customs"] = _region_text(boxes, dvk["x0"] - 0.02 * pw, dvk["y1"] + 1, pw, dvk["y1"] + 0.035 * ph, exclude=[dvk]) or None
    ins = _label_first(labs, "insurance")
    if ins:
        line = [b for b in boxes if _same_line(b, ins, 0.8) and b["x1"] < ins["x0"] + 2 and b is not ins]
        m = None
        for b in sorted(line, key=lambda b: -b["x1"]):
            if re.fullmatch(r"(XXX|XX|NIL|[Xx]{2,3})", b["text"]) or _is_number(b["text"]):
                m = b["text"]
                break
        doc["amount_of_insurance"] = m

    # --- Handling / SCI ---
    hd = _label_first(labs, "handling")
    if hd:
        stops = [labs[s][0] for s in ("pieces", "sci", "nature")
                 if labs.get(s) and labs[s][0]["y0"] > hd["y1"] + 2]
        bottom = min((s["y0"] for s in stops), default=hd["y1"] + 0.07 * ph)
        doc["handling_information"] = _region_text(boxes, 0, hd["y0"] - 1, pw * 0.88, bottom, exclude=[hd]) or None
    sci = _label_first(labs, "sci")
    if sci:
        doc["sci"] = _region_text(boxes, sci["x0"], sci["y1"] + 1, sci["x1"] + 0.08 * pw, sci["y1"] + 0.028 * ph, exclude=[sci]) or "SCI"

    # --- 货物表 ---
    cargo_hdrs = {k: _label_first(labs, k) for k in ("pieces", "gross", "chargeable", "nature")}
    if any(cargo_hdrs.values()):
        live = {k: v for k, v in cargo_hdrs.items() if v}
        top = min(v["y1"] for v in live.values())
        bots = [labs[k][0]["y0"] for k in ("prepaid", "weightcharge", "collect", "executed", "othercharges")
                if labs.get(k) and labs[k][0]["y0"] > top]
        bottom = min(bots) if bots else ph * 0.75
        colbounds = []
        for k in ("pieces", "gross", "rateclass", "commodity", "chargeable", "ratecharge", "totalcol", "nature"):
            b = _label_first(labs, k)
            if b:
                colbounds.append((k, (b["x0"] + b["x1"]) / 2))
        colbounds.sort(key=lambda t: t[1])
        # 冲突锚点按规范列顺序取舍 (rateclass 优先于 rate 等), 边界取相邻锚点中点
        canon = {k: i for i, k in enumerate(
            ("pieces", "gross", "rateclass", "commodity", "chargeable", "ratecharge", "totalcol", "nature"))}
        dedup = []
        for k, cx in colbounds:
            if dedup and cx - dedup[-1][1] <= 0.025 * pw:
                if canon[k] < canon[dedup[-1][0]]:
                    dedup[-1] = (k, cx)
                continue
            dedup.append((k, cx))
        colbounds = dedup
        edges = [0.0] + [(colbounds[i][1] + colbounds[i + 1][1]) / 2 for i in range(len(colbounds) - 1)] + [float(pw)]
        names = [k for k, _ in colbounds]
        def col_of(x):
            for i in range(len(edges) - 1):
                if edges[i] <= x < edges[i + 1]:
                    return names[i]
            return names[-1] if names else "nature"
        sel = [b for b in boxes if top < b["y1"] <= bottom + 6 and b["y0"] > top]
        # 过滤残留在表头带的表头碎片
        frag = {"no", "of", "pieces", "rcp", "gross", "weight", "kg", "lb", "ib", "kglb",
                "rate", "class", "rateclass", "commodity", "item", "itemno", "commodityitemno",
                "chargeable", "total", "nature", "quantity", "incl", "dimensions", "or", "volume"}
        hdr_cut = top + max(12, 0.012 * ph)
        sel = [b for b in sel if not (b["y1"] <= hdr_cut and _sq(b["text"]) in frag)]
        rows = []
        for ln in _group_lines(sel):
            cells = {}
            for b in ln:
                cells.setdefault(col_of((b["x0"] + b["x1"]) / 2), []).append(b["text"])
            flat = {k: " ".join(v) for k, v in cells.items()}
            # 行内出现数字即视为货物数据行 (毛重常带单位如 "1205.0K")
            if re.search(r"\d", flat.get("pieces") or "") or re.search(r"\d", flat.get("gross") or ""):
                rows.append({k: None for k in ("pieces", "gross_weight", "rate_class",
                                               "chargeable_weight", "rate", "total", "description")})
                r = rows[-1]
                r["pieces"] = _norm_num(flat.get("pieces"))
                gm = re.search(r"([\d,.]+)", flat.get("gross") or "")
                r["gross_weight"] = _norm_num(gm.group(1)) if gm else None
                rc = (flat.get("rateclass") or "").strip() or None
                if rc:
                    ms = re.fullmatch(r"([A-Z]{1,3})\s+([\d,.]+)", rc)
                    if ms:  # 版式粘连，如 "CW 427.00" = 等级CW + 运价427.00
                        r["rate_class"] = ms.group(1)
                        if r["rate"] is None:
                            r["rate"] = _norm_num(ms.group(2))
                    else:
                        r["rate_class"] = rc
                cm = re.search(r"([\d,.]+)", flat.get("chargeable") or "")
                r["chargeable_weight"] = _norm_num(cm.group(1)) if cm else None
                rm = re.search(r"([\d,.]+)", flat.get("ratecharge") or "")
                if rm:
                    r["rate"] = _norm_num(rm.group(1))
                elif (flat.get("ratecharge") or "").strip() and not flat.get("totalcol"):
                    r["total"] = flat["ratecharge"].strip()  # 如 "AS ARRANGED"
                if flat.get("totalcol"):
                    r["total"] = _norm_num(flat.get("totalcol")) if _is_number(flat.get("totalcol", "")) else flat["totalcol"]
                # 全空数据行 (如 "TYCO NO.1-9" 文字说明行) 丢弃
                if all(r[k] is None for k in ("pieces", "gross_weight", "chargeable_weight", "rate", "total", "description")):
                    rows.pop()
            if rows and flat.get("nature"):
                r = rows[-1]
                r["description"] = (r["description"] + "\n" if r["description"] else "") + flat["nature"]
        for r in rows:
            d = r["description"] or ""
            r["dims"] = re.findall(r"\d+\s*[xX×*]\s*\d+\s*[xX×*]\s*\d+(?:\s*CM)?(?:\s*/\s*\d+)?", d) or None
            mv = re.search(r"VOL\.?\s*W(?:EIGHT|GHT)\.?[:\s]*([\d.]+)", d, re.I)
            r["volume_weight"] = _norm_num(mv.group(1)) if mv else None
            vol = re.search(r"VOL(?:UME)?\.?\s*:?\s*([\d.]+)\s*(CBM|M3)?", d, re.I)
            vol_txt = None
            if vol:
                vol_txt = vol.group(1) + (vol.group(2) or "M3")
            else:
                m3 = re.search(r"(?:\b(?:M3|CBM)\s*:?\s*([\d.]+))|([\d.]+)\s*(?:CBM|M3\b)", d, re.I)
                if m3:
                    vol_txt = (m3.group(1) or m3.group(2)) + "M3"
            r["volume"] = vol_txt
            r["hs_codes"] = re.findall(r"HS\s*C(?:odes?|ODE)\s*[:：]?\s*([\d, ]+)", d, re.I) or None
            r["dims"] = [re.sub(r"\s+", "", x.replace("*", "x").replace("X", "x")).lower().replace("cm", " CM") for x in r["dims"]] if r["dims"] else None
        doc["cargo"] = rows

    # --- 底部费用 ---
    cs = doc["charges_summary"]
    def fill(lname, key):
        lbs = sorted(labs.get(lname) or [], key=lambda b: b["y0"])
        vals = []
        for lb in lbs:
            vals.extend(_vals_near(boxes, lb))
        if vals:
            cs[key]["prepaid"] = vals[0]
            if len(vals) > 1:
                cs[key]["collect"] = vals[1]
    for lname, key in [("weightcharge", "weight_charge"), ("valuation", "valuation_charge"),
                       ("tax", "tax"), ("dueagent", "total_other_charges_due_agent"),
                       ("duecarrier", "total_other_charges_due_carrier")]:
        fill(lname, key)
    def side_vals(lname):
        out = []
        for lb in sorted(labs.get(lname) or [], key=lambda b: b["y0"]):
            v = _first_number(boxes, lb["x0"] - 0.25 * pw, lb["y0"] - 4, lb["x0"] - 2, lb["y1"] + 4)
            if v is None:
                v = _first_number(boxes, lb["x1"] + 2, lb["y0"] - 4, lb["x1"] + 0.2 * pw, lb["y1"] + 4)
            if v is None:
                v = _first_number(boxes, lb["x0"] - 0.05 * pw, lb["y1"] - 2, lb["x1"] + 0.18 * pw, lb["y1"] + 0.045 * ph)
            if v is not None:
                out.append(v)
        return out
    tp = side_vals("totalprepaid")
    if tp:
        cs["total_prepaid"] = tp[0]
        if len(tp) > 1 and cs["total_collect"] is None:
            cs["total_collect"] = tp[1]
    tc = side_vals("totalcollect")
    if tc and cs["total_collect"] is None:
        cs["total_collect"] = tc[0]
    ccr = _label_first(labs, "ccr")
    if ccr:
        cs["currency_conversion_rates"] = _first_number(boxes, ccr["x0"] - 0.05 * pw, ccr["y1"] - 2, ccr["x1"] + 0.18 * pw, ccr["y1"] + 0.045 * ph)
    ccc = _label_first(labs, "cccharges")
    if ccc:
        cs["cc_charges_in_dest_currency"] = _first_number(boxes, ccc["x1"] + 2, ccc["y0"] - 4, ccc["x1"] + 0.2 * pw, ccc["y1"] + 4) \
            or _first_number(boxes, ccc["x0"] - 0.25 * pw, ccc["y0"] - 4, ccc["x0"] - 2, ccc["y1"] + 4)
    cd = _label_first(labs, "chargesdest")
    if cd:
        cs["charges_at_destination"] = _first_number(boxes, cd["x1"] + 2, cd["y0"] - 4, cd["x1"] + 0.2 * pw, cd["y1"] + 4)
    tcc = _label_first(labs, "totalcollectcharges")
    if tcc:
        cs["total_collect_charges"] = _first_number(boxes, tcc["x1"] + 2, tcc["y0"] - 4, tcc["x1"] + 0.2 * pw, tcc["y1"] + 4)

    # Other Charges 费用代码表
    oc = _label_first(labs, "othercharges")
    cert = _label_first(labs, "certifies")
    if oc:
        bottom = cert["y0"] if cert else (labs["sigs"][0]["y0"] if labs.get("sigs") else ph)
        sel = [b for b in boxes if b["y0"] > oc["y1"] - 2 and b["y1"] < bottom and b["x0"] > oc["x0"] - 0.02 * pw]
        for ln in _group_lines(sel):
            codes = [b for b in ln if re.fullmatch(r"[A-Z]{2,4}", b["text"])]
            nums = [b for b in ln if _is_number(b["text"])]
            for n in nums:
                code = min(codes, key=lambda c: abs((c["x0"] + c["x1"]) / 2 - (n["x0"] + n["x1"]) / 2)) if codes else None
                if code:
                    doc["other_charges_list"].append({"code": code["text"], "amount": _norm_num(n["text"])})

    # --- 签署区 ---
    ex = _label_first(labs, "executed")
    if ex:
        line = [b for b in boxes if _same_line(b, ex, 1.0) and b["x0"] > ex["x1"] - 0.01 * pw and b is not ex]
        linej = " ".join(b["text"] for b in sorted(line, key=lambda b: b["x0"]))
        m = re.search(r"(\d{1,2}[-/ ]\w{3,9}[-/ ]\d{2,4}|\d{4}-\d{2}-\d{2}[\d:.]*)", linej)
        if m:
            doc["executed_on"] = m.group(1)
        doc["extras"]["executed_line"] = linej or None
    ap = _label_first(labs, "atplace")
    if ap:
        doc["executed_at"] = _region_text(boxes, ap["x1"] + 2, ap["y0"] - 4, pw, ap["y1"] + 6, exclude=[ap]) or None
    sc = _label_first(labs, "sigc")
    if sc:
        doc["signature_of_issuing_carrier_or_agent"] = _region_text(boxes, pw * 0.40, max(0, sc["y0"] - 0.05 * ph), pw, sc["y0"] - 2) or None
    ss = _label_first(labs, "sigs")
    if ss:
        doc["signature_of_shipper_or_agent"] = _region_text(boxes, pw * 0.38, max(0, ss["y0"] - 0.05 * ph), pw, ss["y0"] - 2) or None
    return doc


def _score_doc(doc):
    n = 0
    for k, v in doc.items():
        if k in ("source", "extras"):
            continue
        if isinstance(v, dict):
            n += sum(1 for x in v.values() if x)
        elif isinstance(v, list):
            n += len(v)
        elif v:
            n += 1
    return n


# -------------------------------------------------------------- PDF 通道 ----

def _pdf_text_boxes(page):
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2.5, keep_blank_chars=False)
    return [{"text": w["text"], "x0": w["x0"], "y0": w["top"], "x1": w["x1"], "y1": w["bottom"], "score": 1.0}
            for w in words]


def extract_pdf(path: Path, dpi=220, allow_ocr=True):
    import pdfplumber
    doc = new_doc(path.name, "pdf")
    with pdfplumber.open(str(path)) as pdf:
        doc["source"]["pages"] = len(pdf.pages)
        page = pdf.pages[0]
        boxes = _pdf_text_boxes(page) if len(page.chars) > 20 else []
        pw, ph = page.width, page.height
        follow = []
        if len(pdf.pages) > 1:
            for p in pdf.pages[1:]:
                t = p.extract_text() or ""
                if t.strip():
                    follow.append(t)
    if boxes:
        assemble_from_boxes(boxes, pw, ph, doc)
        doc["source"]["extracted_by"] = "pdf-text"
        has_core = any(isinstance(c.get("gross_weight"), (int, float)) or c.get("pieces")
                       for c in (doc.get("cargo") or []))
        if (_score_doc(doc) < 15 or not has_core) and allow_ocr:
            doc2 = new_doc(path.name, "pdf")
            _ocr_pdf(path, doc2, dpi)
            if _score_doc(doc2) > _score_doc(doc):
                doc = doc2
            else:
                doc["source"]["warnings"] = doc["source"].get("warnings", []) + ["text-layer parse weak; OCR not better"]
    else:
        _ocr_pdf(path, doc, dpi)
    if follow and "follow_on_pages_text" not in doc["extras"]:
        doc["extras"]["follow_on_pages_text"] = "\n---- page ----\n".join(follow)
    return doc


# -------------------------------------------------------------- OCR 通道 ----

_OCR = None


def _get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        for kwargs in ({"params": {"Det.limit_side_len": 2432, "Det.limit_type": "max"}},
                       {"Det.limit_side_len": 2432, "Det.limit_type": "max"}, {}):
            try:
                _OCR = RapidOCR(**kwargs)
                break
            except Exception:
                continue
    return _OCR


def _ocr_image(img) -> list:
    res, _ = _get_ocr()(img)
    boxes = []
    if res:
        for quad, text, score in res:
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            boxes.append({"text": str(text), "score": float(score),
                          "x0": float(min(xs)), "y0": float(min(ys)),
                          "x1": float(max(xs)), "y1": float(max(ys))})
    return boxes


def _render_page(path: Path, page_no: int, dpi: int):
    import cv2
    import numpy as np
    import pymupdf
    d = pymupdf.open(str(path))
    page = d[page_no]
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)
    return img, float(pix.width), float(pix.height)


def _ocr_pdf(path: Path, doc: dict, dpi: int):
    doc["source"]["extracted_by"] = "ocr"
    texts = []
    for i in range(doc["source"]["pages"]):
        img, pw, ph = _render_page(path, i, dpi)
        boxes = _ocr_image(img)
        if i == 0:
            assemble_from_boxes(boxes, pw, ph, doc)
        else:
            texts.append("\n".join(b["text"] for b in boxes))
    if texts:
        doc["extras"]["follow_on_pages_text"] = "\n---- page ----\n".join(texts)


def extract_image(path: Path):
    import cv2
    import numpy as np
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cannot read image")
    h, w = img.shape[:2]
    doc = new_doc(path.name, path.suffix.lstrip(".").lower())
    doc["source"]["extracted_by"] = "ocr"
    boxes = _ocr_image(img)
    assemble_from_boxes(boxes, float(w), float(h), doc)
    low = [b["text"] for b in boxes if b["score"] < 0.75]
    if low:
        doc["source"]["low_confidence_ocr"] = low[:20]
    return doc


# ------------------------------------------- 16 字段目标 Schema 投影 ---------

TARGET_KEYS = ("MAWB_NO", "HAWB_NO", "SHIPPER_INFO", "CONSIGNEE_INFO", "ORIGIN_NAME",
               "TO1", "TO2", "TO3", "DEST_NAME", "GOODS_INFO", "GOODS_HS_CODE",
               "PIECES", "WEIGHT", "SLAC", "CREATE_TIME", "SEND_STATUS")

# 40 字段 = 基础 16 + 发货人 12 项 + 收货人 12 项 (CITTY 为库表原拼写, 勿"修正")
# TAX_ID 是票面税号（中国 USCI/统一社会信用代码、巴西 CNPJ、RFC、VAT NO、TAX ID）的唯一落点：
# 它按规则既不能进 TEL 也不能进 EORI，37 字段时代没有归处，模型直接丢弃且质检看不出来。
# GOODS_HS_CODE 是货物描述里 HS CODE(S) 的唯一落点；GOODS_INFO 保持照抄不动（保真口径）。
TARGET_KEYS_OUT = TARGET_KEYS[:2] + ("SHIPPER_INFO", "CONSIGNEE_INFO") + TARGET_KEYS[4:16] + \
    tuple(p + s for p in ("SHIPPER_INFO_", "CONSIGNEE_INFO_")
          for s in ("COMP_NAME", "COMP_ADDRESS", "CITY" if p == "SHIPPER_INFO_" else "CITTY",
                    "COUNTRY", "STATE", "POSTAL", "TEL", "FAX", "EORI", "AEO", "EMAIL", "TAX_ID"))

# 票面税号标签 -> 号码。只认带标签的，绝不拿裸数字猜（18 位纯数字也可能是货值/账号）。
# EORI 是 2026-09-26 CCSP 八票对格补的：欧票把海关号印成 `EORI IT03268900267`，
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


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

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


def _norm_phone(t):
    """+46 (0)70 31 76 762 -> +46703176762 (去空格/括号/横线, 去国家码后的干线0)"""
    t = re.sub(r"\(0\)", "", str(t or ""))
    return re.sub(r"[^+\d]", "", t)


def _lookup_city(name):
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


def _iata_place(v):
    """起降港名 -> IATA 三字码 (BEIJING->BJS / GOTHENBURG LANDVE->GOT)；已是三字码或未知则原样"""
    v = str(v or "").strip().upper()
    if not v:
        return ""
    return _lookup_city(v) or v


def _norm_date_text(t):
    """04.06.2024 / 29-MAY-2024 / JUN 06, 2024 -> 2024-06-04；无法解析返回 ''"""
    import calendar
    def _ok(y, mo, d):
        return 1 <= mo <= 12 and 1 <= d <= calendar.monthrange(y, mo)[1]
    if not t:
        return ""
    s = str(t).upper().replace(",", " ").replace("  ", " ")
    m = re.search(r"(?<!\d)(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?!\d)", s)  # YYYY-MM-DD / YYYY/MM/DD
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _ok(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})(?!\d)", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _ok(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2})(?!\d)", s)  # DD/MM/YY
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3)) + 2000
        if _ok(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"(?<!\d)(\d{1,2})[\s-]+([A-Z]{3})[A-Z]*[A-Za-z ,\-]+(\d{4})(?!\d)", s)   # 29-MAY-2024 / 30 MAY 2024
    if m:
        d, mon, y = int(m.group(1)), m.group(2), int(m.group(3))
        mo = _MONTHS.get(mon)
        if mo and _ok(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"\b([A-Z]{3})[A-Z]*[\s.,]+(\d{1,2})[A-Za-z ,]*(\d{4})\b", s)      # JUN 06, 2024 / JUN 06,2024
    if m:
        mon, d, y = m.group(1), int(m.group(2)), int(m.group(3))
        mo = _MONTHS.get(mon)
        if mo and _ok(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def _fmt_mawb(v):
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


def _fmt_hawb(v):
    """分单号规范化: SIA82405250 -> SIA-8240 5250；含字母前缀不规则的原样保留"""
    if v is None or str(v).strip() == "":
        return ""
    s = str(v).strip().upper().replace(" ", "")
    m = re.fullmatch(r"([A-Z]{3})-?(\d{8})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)[:4]} {m.group(2)[4:]}"
    return s


def _join_party(p):
    """收/发货人 -> '公司名, 地址行, TEL:.., FAX:..' 合并字符串 (地址已含则不重复追加)"""
    if not p:
        return ""
    parts = []
    if p.get("name"):
        parts.append(str(p["name"]).strip())
    addr = str(p.get("address") or "")
    if addr:
        for line in addr.splitlines():
            line = line.strip()
            if line:
                parts.append(line)
    addr_up = addr.upper()
    if p.get("tel") and "TEL" not in addr_up:
        parts.append("TEL:" + str(p["tel"]).strip())
    if p.get("fax") and "FAX" not in addr_up:
        parts.append("FAX:" + str(p["fax"]).strip())
    return ", ".join(parts)


def _goods_text(cargo):
    """品名 + HS Code；去掉 VOL.WGHT/体积/SLAC 等非品名行"""
    descs = []
    for c in cargo:
        for line in str(c.get("description") or "").splitlines():
            s = line.strip()
            if not s or re.match(r"^(VOL(?:\.?(?:WGHT|WEIGHT))?|SLAC)\b", s, re.I):
                continue
            if re.fullmatch(r"[\d.\s]*(?:CBM|M3)", s, re.I):
                continue
            descs.append(s)
    return " ".join(descs)


_HS_CODE_RE = re.compile(r"H\.?\s*S\.?\s*(?:CODES?|码)\s*[:：]?\s*([0-9][0-9.,，;；/\s]{3,})", re.I)


def _hs_code_text(cargo):
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


def _slac_value(cargo, doc=None):
    """SLAC = 小件数: 票面显式 "SLAC n" 计数; 没有则由调用方回退为件数"""
    texts = []
    for c in cargo:
        texts.append(str(c.get("description") or ""))
        texts.append(str(c.get("volume") or ""))
        texts += [str(d) for d in (c.get("dims") or [])]
    joined = "\n".join(texts)
    m = re.search(r"SLAC\W{0,5}(\d{1,5})(?!\.\d)", joined, re.I)
    if m:
        return int(m.group(1))
    if doc:  # 票面其他区域 (handling 等) 兜底
        m = re.search(r"SLAC\W{0,5}(\d{1,5})(?!\.\d)", json.dumps(doc, ensure_ascii=False), re.I)
        if m:
            return int(m.group(1))
    return None


def _split_consignee_leak(ship, cons):
    """CONSIGNEE 为空但 SHIPPER 文本里串入了 Consignee 标签时 (OCR 排版错乱),
    按标签切分并恢复收货人块"""
    if cons or not ship:
        return ship, cons
    rx = re.compile(r"consignee[\W_]*s?[\W_]*"
                    r"(?:name[\W_]*(?:and|end)?[\W_]*address|account[\W_]*number)", re.I)
    m = rx.search(ship)
    if not m:
        return ship, cons
    head = ship[:m.start()]
    # 去掉标签前粘连的 OCR 噪声 (含 >=6 个数字、数字被字母隔开的乱码, 如 TonsigneN2e98edre888)
    head = re.sub(r"[,\s]*[A-Za-z0-9]*\d(?:[A-Za-z]*\d){5,}[A-Za-z0-9]*\s*$", "", head).strip(" ,")
    tail = ship[m.end():]
    while True:
        m2 = rx.search(tail)
        if not m2:
            break
        tail = tail[m2.end():]
    tail = re.sub(r"^[\W_]*\d{5,}[\W_]*", "", tail).strip(" ,")
    return head, tail


_FW_TRANS = str.maketrans({"，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")",
                           "【": "[", "】": "]", "、": ",", "－": "-", "　": " "})


_SEG_PUNCT = " .;()[]{}"


def _strip_seg(s: str) -> str:
    """地址段两端去标点。摘掉尾部国码 '(SK)' 里的码后会留下不配对的 '(SK'，一并清掉。"""
    out = s.strip(_SEG_PUNCT)
    out = re.sub(r"[,(]\s*[A-Z]{0,4}\s*$", "", out).strip(_SEG_PUNCT)
    return out


def _parse_party(p):
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


def _party_info(p):
    """第一遍 INFO 合并串: 公司名, 完整地址, TEL/FAX (原文; 一号两用记作 TEL/FAX)"""
    pp = _parse_party(p)
    parts = [x for x in (pp["name"], pp["full_addr"]) if x]
    up = pp["full_addr"].upper()
    if pp["tel"] and "TEL" not in up:
        if pp["fax"] and pp["fax"] == pp["tel"]:
            parts.append("TEL/FAX: " + pp["tel"])
        else:
            parts.append("TEL: " + pp["tel"])
    if pp["fax"] and pp["fax"] != pp["tel"] and "FAX" not in up:
        parts.append("FAX: " + pp["fax"])
    return ", ".join(parts)


def project_raw(doc: dict) -> dict:
    """第一遍: 原文提取, 严格 39 字段 (基础 15 + 收/发货人各 12 项明细)"""
    d = _target_base(doc)
    sh = _parse_party(doc.get("shipper"))
    co = _parse_party(doc.get("consignee"))
    if not sh["eori"] and not co["eori"]:
        # EORI/VAT 常印在页底 customs 区 (不在收发货人块内): doc 级兜底归收货人
        dump = json.dumps(doc, ensure_ascii=False)
        m = (re.search(r"\bEORI\b[\s:：#.\"]*([A-Z]{2}[A-Z0-9]{6,15})", dump, re.I)
             or re.search(r"\bV\.?A\.?T\.?(?:\s*(?:NO|NR|NUMBER|ID))?\b[\s:：.#\"]*([A-Z]{2}[A-Z0-9]{6,15})",
                          dump, re.I))
        if m:
            co["eori"] = m.group(1).upper()
    d["SHIPPER_INFO"], d["CONSIGNEE_INFO"] = _split_consignee_leak(
        _party_info(doc.get("shipper")), _party_info(doc.get("consignee")))
    for pfx, pp in (("SHIPPER", sh), ("CONSIGNEE", co)):
        city_key = pfx + "_INFO_CITY" if pfx == "SHIPPER" else "CONSIGNEE_INFO_CITTY"
        d[pfx + "_INFO_COMP_NAME"] = pp["name"]
        d[pfx + "_INFO_COMP_ADDRESS"] = pp["full_addr"]
        d[city_key] = pp["city"]
        d[pfx + "_INFO_COUNTRY"] = pp["country"]
        d[pfx + "_INFO_STATE"] = pp["state"]
        d[pfx + "_INFO_POSTAL"] = pp["postal"]
        d[pfx + "_INFO_TEL"] = pp["tel"]
        d[pfx + "_INFO_FAX"] = pp["fax"]
        d[pfx + "_INFO_EORI"] = pp["eori"]
        d[pfx + "_INFO_AEO"] = pp["aeo"]
        d[pfx + "_INFO_EMAIL"] = pp["email"]
    return {k: d.get(k, "") for k in TARGET_KEYS_OUT}


def project_air(doc: dict) -> dict:
    """第二遍: 按航空运输要求处理 — 国家两位码, 城市/起降港 IATA 码, 电话去空格括号"""
    d = dict(project_raw(doc))
    d["ORIGIN_NAME"] = _iata_place(d["ORIGIN_NAME"])
    d["DEST_NAME"] = _iata_place(d["DEST_NAME"])
    for pfx, key in (("SHIPPER", "shipper"), ("CONSIGNEE", "consignee")):
        pp = _parse_party(doc.get(key))
        if not (pp["name"] or pp["full_addr"]):
            continue  # 串块切分等特殊场景: 保留第一遍文本
        city_key = pfx + "_INFO_CITY" if pfx == "SHIPPER" else "CONSIGNEE_INFO_CITTY"
        country2 = COUNTRY_ISO2.get(pp["country"], pp["country"])
        tel, fax = _norm_phone(pp["tel"]), _norm_phone(pp["fax"])
        d[city_key] = _lookup_city(pp["city"]) or pp["city"]
        d[pfx + "_INFO_COUNTRY"] = country2
        d[pfx + "_INFO_COMP_ADDRESS"] = pp["street"] or d[pfx + "_INFO_COMP_ADDRESS"]
        d[pfx + "_INFO_TEL"], d[pfx + "_INFO_FAX"] = tel, fax
        parts = [x for x in (pp["name"], pp["street"], pp["locality"], country2) if x]
        if tel:
            parts.append("TEL: " + tel)
            if fax and fax != tel:
                parts.append("FAX: " + fax)
        elif fax:
            parts.append("FAX: " + fax)
        if parts:
            d[pfx + "_INFO"] = ", ".join(parts)
    return {k: d.get(k, "") for k in TARGET_KEYS_OUT}


def _target_base(doc: dict) -> dict:
    """内部富 Schema -> 基础 15 字段 (原文口径)"""
    raw_cargo = doc.get("cargo") or []
    # 合理性过滤: 底部电话/账号/费用长数字误入货物表时剔除 (描述文本仍保留)
    cargo = [c for c in raw_cargo
             if not (isinstance(c.get("gross_weight"), (int, float)) and c["gross_weight"] > 100000)
             and not (isinstance(c.get("pieces"), (int, float)) and c["pieces"] > 10000)]
    # PIECES: 各行件数求和
    pc = [c["pieces"] for c in cargo if isinstance(c.get("pieces"), (int, float))]
    pieces = sum(pc) if pc else None
    # WEIGHT: 毛重去重求和 (SLAC 重复声明行不翻倍; 0 值噪声忽略)
    gw = [float(c["gross_weight"]) for c in cargo
          if isinstance(c.get("gross_weight"), (int, float)) and c["gross_weight"] > 0]
    weight = sum(set(gw)) if gw else None
    # TO1/TO2/TO3: 航路三字码
    routing = doc.get("routing") or []
    tos = [(r.get("to") or "").strip() for r in routing[:3]]
    tos += [""] * (3 - len(tos))
    # CREATE_TIME: 航班日期 -> 制单日期 -> 签名区日期
    ct = _norm_date_text((doc.get("extras") or {}).get("flight_date_raw"))
    if not ct:
        ct = _norm_date_text(doc.get("executed_on"))
    if not ct:
        ct = _norm_date_text(doc.get("signature_of_issuing_carrier_or_agent"))
    if not ct:
        ct = _norm_date_text(doc.get("flight_date"))
    ship_info, cons_info = _split_consignee_leak(
        _join_party(doc.get("shipper")), _join_party(doc.get("consignee")))
    # SLAC = 票面显式小件数; 无则默认件数
    slac = _slac_value(raw_cargo, doc)
    if slac is None:
        slac = pieces
    # ORIGIN: OCR 错乱防护 — 起运港区域串入电话/货代名/长数字的行剔除
    ori = " ".join(l.strip() for l in (doc.get("airport_of_departure") or "").splitlines()
                   if l.strip()
                   and not re.search(r"TEL\s*[:：]|@|\d{3,}", l, re.I)
                   and not re.search(r"FREIGHT|FORWARDER|LOGISTICS", l, re.I))
    return {
        "MAWB_NO": _fmt_mawb(doc.get("awb_stock_number")),
        "HAWB_NO": _fmt_hawb(doc.get("hawb_number")),
        "SHIPPER_INFO": ship_info,
        "CONSIGNEE_INFO": cons_info,
        "ORIGIN_NAME": ori.strip(),
        "TO1": tos[0],
        "TO2": tos[1],
        "TO3": tos[2],
        "DEST_NAME": (doc.get("airport_of_destination") or "").strip().splitlines()[0].strip()
        if (doc.get("airport_of_destination") or "").strip() else "",
        "GOODS_INFO": _goods_text(raw_cargo),
        "GOODS_HS_CODE": _hs_code_text(raw_cargo),
        "PIECES": pieces,
        "WEIGHT": weight,
        "SLAC": slac,
        "CREATE_TIME": ct,
        "SEND_STATUS": "PENDING",
    }


# ------------------------------------------------------------------ main ----

def convert_file(path: Path, dpi=220, allow_ocr=True):
    ext = path.suffix.lower()
    if ext in XLS_EXTS:
        return extract_excel(path)
    if ext == ".pdf":
        return extract_pdf(path, dpi, allow_ocr)
    if ext in IMG_EXTS:
        return extract_image(path)
    raise ValueError(f"unsupported: {ext}")


def validate(doc: dict) -> list:
    warns = []
    hn = doc.get("hawb_number")
    if not hn:
        warns.append("missing hawb_number")
    elif not re.fullmatch(r"[A-Z0-9]{2,3}-?[A-Z0-9]{5,12}", str(hn).replace(" ", "")):
        warns.append(f"hawb_number format unusual: {hn}")
    for i, r in enumerate(doc.get("cargo") or []):
        if r.get("pieces") is None:
            warns.append(f"cargo[{i}] pieces missing")
        if r.get("gross_weight") is None:
            warns.append(f"cargo[{i}] gross_weight missing")
    if not (doc.get("shipper") or {}).get("name"):
        warns.append("missing shipper.name")
    if not (doc.get("consignee") or {}).get("name"):
        warns.append("missing consignee.name")
    return warns


def main():
    ap = argparse.ArgumentParser(description="HAWB 分单批量转 JSON")
    ap.add_argument("input", help="输入文件或目录")
    ap.add_argument("-o", "--out", default=None, help="输出目录 (默认 <输入目录>/json)")
    ap.add_argument("--dpi", type=int, default=220, help="扫描 PDF 渲染 DPI (默认 220)")
    ap.add_argument("--no-ocr", action="store_true", help="跳过 OCR (只处理表格与 PDF 文字层)")
    ap.add_argument("--summary", default="all_hawbs.json", help="汇总文件名 (放在输出目录)")
    args = ap.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        print(f"not found: {inp}")
        sys.exit(1)
    if inp.is_dir():
        files = sorted(f for f in inp.rglob("*")
                       if f.is_file() and f.suffix.lower() in (XLS_EXTS | IMG_EXTS | {".pdf"})
                       and not f.name.startswith("_"))
    else:
        files = [inp]
    outdir = Path(args.out) if args.out else (inp / "json" if inp.is_dir() else inp.parent / "json")
    outdir.mkdir(parents=True, exist_ok=True)
    airdir = Path(str(outdir).rstrip("\\/") + "_air")   # 第二遍(航空运输要求)输出目录
    airdir.mkdir(parents=True, exist_ok=True)

    docs, docs_air, log = [], [], []
    for f in files:
        try:
            doc = convert_file(f, args.dpi, allow_ocr=not args.no_ocr)
            warns = validate(doc)
            if warns:
                doc["source"].setdefault("warnings", []).extend(warns)
            out = outdir / (f.stem + ".json")
            outa = airdir / (f.stem + ".json")
            raw = project_raw(doc)
            air = project_air(doc)
            out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            outa.write_text(json.dumps(air, ensure_ascii=False, indent=2), encoding="utf-8")
            docs.append(raw)
            docs_air.append(air)
            log.append(f"[OK] {f.name} -> {out.name} (fields={_score_doc(doc)}, by={doc['source']['extracted_by'] or 'excel'})"
                       + (f" warnings={warns}" if warns else ""))
        except Exception as e:
            log.append(f"[FAIL] {f.name}: {e}\n{traceback.format_exc()}")
        print(log[-1].splitlines()[0], flush=True)

    (outdir / args.summary).write_text(
        json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    (airdir / "all_hawbs_air.json").write_text(
        json.dumps(docs_air, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "_log.txt").write_text("\n".join(log), encoding="utf-8")
    ok = sum(1 for l in log if l.startswith("[OK]"))
    print(f"\ndone: {ok}/{len(files)} converted -> {outdir} + {airdir}")


if __name__ == "__main__":
    main()
