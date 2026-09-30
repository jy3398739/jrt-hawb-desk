# -*- coding: utf-8 -*-
"""模块边界（设计文档 §4.1/§4.2）：跨模块只许叫公共名，退役的规则层不许复活。

从前 to_air 直接调 hawb2json._parse_party、_lookup_city 这类下划线私产——那等于把另一个模块的
内部实现当公开接口用：它在原模块里随时会被重构，而调用点分散在三个文件里没人知道。
公共件现在都在 codes.py，这里用扫描把这条规矩钉住。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD = sorted(p for p in ROOT.glob("*.py"))
LOCAL = {p.stem for p in PROD}


def _aliases(src: str) -> dict:
    """本文件里 `import 本地模块 [as 别名]` 的别名表。"""
    out = {}
    for m in re.finditer(r"^import\s+(\w+)(?:\s+as\s+(\w+))?", src, re.M):
        mod, alias = m.group(1), m.group(2) or m.group(1)
        if mod in LOCAL and mod != "__init__":
            out[alias] = mod
    return out


def test_no_cross_module_private_calls():
    hits = []
    for p in PROD:
        src = p.read_text(encoding="utf-8")
        al = _aliases(src)
        for m in re.finditer(r"\b(\w+)\.(_[A-Za-z]\w*)", src):
            if m.group(1) in al:
                hits.append(f"{p.name}: {m.group(0)} 借用了 {al[m.group(1)]} 的私产")
        for m in re.finditer(r"from\s+(\w+)\s+import\s+([^\n]+)", src):
            if m.group(1) in LOCAL:
                bad = [x.strip().split(" ")[0] for x in m.group(2).split(",")
                       if x.strip().startswith("_")]
                hits += [f"{p.name}: from {m.group(1)} import {b}" for b in bad]
    assert not hits, "跨模块调私有函数（要共用就把它变成公共名）：\n    " + "\n    ".join(hits)


def test_retired_rule_layer_stays_gone():
    """本地 OCR / 版式规则层退役后（2026-09 定案，扫描件统一走 PDF + 视觉模型），
    重新引回 hawb2json 那套会让"同一张票两条答案"重新出现——代码没了才算退役。"""
    assert not (ROOT / "hawb2json.py").exists(), "退役规则层又回来了"
    for p in PROD:
        assert "hawb2json" not in p.read_text(encoding="utf-8"), f"{p.name} 还引用退役规则层"
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for dead in ("rapidocr", "opencv", "onnxruntime", "xlrd", "pdfplumber", "numpy"):
        assert re.search(r"^\s*%s" % dead, req, re.M) is None, \
            f"依赖 {dead} 只为退役层而装：留在 requirements 里就会继续拖累这台共享解释器"


def test_code_tables_have_one_home():
    """码表与票面写法只在 codes.py；别处再开一份 CITY_IATA/COUNTRY_ISO2/税号标签就是第二真源。"""
    dupes = []
    for p in PROD:
        if p.name == "codes.py":
            continue
        src = p.read_text(encoding="utf-8")
        for table in ("CITY_IATA = {", "COUNTRY_ISO2 = {", "TEL_COUNTRY_CODES = {", "TAX_LABEL_RE = "):
            if table in src:
                dupes.append(f"{p.name}: {table.strip()}")
    assert not dupes, "这些码表又长出第二份：\n    " + "\n    ".join(dupes)
