# -*- coding: utf-8 -*-
"""L1 票面逐字转录层：让 VLM 充当"纯 OCR 转录员"，零加工。
输出 {lines:[{i, text, bbox:[x1,y1,x2,y2](0-1000归一化, 粗定位用)}], full_text}。
保真铁律：不纠错、不补全、不改大小写/空格/标点、不翻译、不合并。
"""
import json
from pathlib import Path
from openai import OpenAI

import config
import retry
from vlm_extract import img_to_data_urls

TRANSCRIBE_PROMPT = """你是一台纯OCR转录机。请把这张航空分单(HAWB)上印刷/打印的所有文字逐行转录出来。

铁律（违反任何一条即为错误）：
1. 逐字符照抄：保留票面原始的大小写、空格、连字符、竖线|、标点、数字分组方式。
   例：票面是 "999-0686 5456" 就输出带空格的 "999-0686 5456"；票面是 "Italy" 不得改成 "ITALY"；
   票面是两字母国家码 "CN"/"SE" 不得还原成国家全称；票面印 "CO.1" 就保留 "CO.1"，禁止"纠正"成 "CO.,"。
2. 禁止纠错、补全、翻译、改写、同义替换；看不清的字符用问号?代替，不允许猜。
3. 按人类阅读顺序逐行输出：顶部栏 → 左侧发货人区 → 右侧收货人区 → 航路/航班表格 → 货物明细 → 费用区 → 底部签发区。
   表格每个横行转录成一行，竖线分隔的内容保留竖线。
4. 不要输出字段名解释、不要分类、不要 markdown 代码块，只输出 JSON。
5. bbox 为该行文字在图上的位置框，坐标归一化到 0-1000 的整数 [x1,y1,x2,y2]（左上→右下），尽量准确。

输出格式（纯JSON，不要任何其他文字）：
{"lines":[{"i":1,"text":"逐字原文","bbox":[x1,y1,x2,y2]}, ...]}"""

_client = None
_client_channel = None


def _get_client():
    """客户端按端点缓存：审核台热切模型可能连渠道一起换（魔搭↔北龙），端点一变就重建，
    绝不让整批票继续跑在旧渠道的客户端上。密钥只在构造时取，缺密钥照常抛 SystemExit。"""
    global _client, _client_channel
    channel = config.vlm_base_url()
    if _client is None or _client_channel != channel:
        _client = OpenAI(api_key=config.require_vlm_api_key(), base_url=channel,
                         timeout=config.VLM_TIMEOUT, max_retries=0)
        _client_channel = channel
    return _client


def _parse(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
    data = json.loads(text)
    lines = data.get("lines", [])
    norm = []
    for i, ln in enumerate(lines, 1):
        if isinstance(ln, dict):
            t = str(ln.get("text", ""))
            bb = ln.get("bbox")
        else:
            t, bb = str(ln), None
        if not t:
            continue
        if not (isinstance(bb, list) and len(bb) == 4):
            bb = None
        norm.append({"i": len(norm) + 1, "text": t, "bbox": bb})
    return {"lines": norm, "full_text": "\n".join(x["text"] for x in norm)}


def transcribe_one(path) -> dict:
    """L1 转录单张图片/PDF（VLM 纯 OCR），失败自动重试。多页 PDF 每页一起下发。
    当前模型无视觉时直接拒绝：扫描件没有文字层，纯文本模型看不见票面，
    硬跑只会拿回一段编造的"转录"。"""
    if not config.MODEL_VISION:
        raise RuntimeError(
            f"当前模型不支持图片输入（{config.VLM_MODEL}），没法转录扫描件："
            "请在审核台顶栏或 .env 的 VLM_MODEL 切到视觉模型（intern-s2-official）")
    path = Path(path)
    client = _get_client()
    content = [{"type": "image_url", "image_url": {"url": u}} for u in img_to_data_urls(path)]
    content.append({"type": "text", "text": TRANSCRIBE_PROMPT})
    last = None
    timeout_tries = 0
    for attempt in range(config.VLM_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=config.VLM_MODEL,
                messages=[{"role": "user", "content": content}],
                max_tokens=config.model_max_tokens(),
                temperature=0.0,
                extra_body=config.model_extra_body(),
            )
            return _parse(resp.choices[0].message.content)
        except Exception as e:
            last = e
            if "timeout" in type(e).__name__.lower():
                timeout_tries += 1
                if timeout_tries >= config.VLM_TIMEOUT_TRIES:      # 与 L2 同一条：超时不连撞三次
                    break
            if attempt < config.VLM_RETRIES - 1:
                retry.wait(attempt)
    raise RuntimeError(f"L1 转录失败 {path.name}: {last}")


def transcribe_pdf_text_layer(pdf_path, min_chars: int = 0) -> dict | None:
    """L1 转录（零调用）：直读 PDF 自带文字层，逐字取排印行。
    电子单导出的 PDF 与业务系统直接生成的分单 PDF 都有完整文字层，
    它即票面逐字真值，比 VLM 转录更硬且省一次 API 调用；
    照片型扫描件没有文字层，返回 None 由调用方退回 VLM 转录。
    bbox 归一化到 0-1000（与 VLM 转录同口径），多页时带 page 标。
    """
    import pymupdf
    doc = pymupdf.open(str(Path(pdf_path)))
    norm = []
    try:
        for pno, page in enumerate(doc, 1):
            pr = page.rect
            for blk in page.get_text("dict")["blocks"]:
                if blk.get("type") != 0:
                    continue
                for ln in blk["lines"]:
                    text = "".join(s.get("text", "") for s in ln["spans"]).strip()
                    if not text:
                        continue
                    x0, y0, x1, y1 = ln["bbox"]
                    entry = {
                        "i": len(norm) + 1,
                        "text": text,
                        "bbox": [
                            max(0, min(1000, round(x0 / pr.width * 1000))),
                            max(0, min(1000, round(y0 / pr.height * 1000))),
                            max(0, min(1000, round(x1 / pr.width * 1000))),
                            max(0, min(1000, round(y1 / pr.height * 1000))),
                        ],
                    }
                    if len(doc) > 1:
                        entry["page"] = pno
                    norm.append(entry)
    finally:
        doc.close()
    full_text = "\n".join(x["text"] for x in norm)
    if len(full_text) < min_chars:
        return None  # 无/过薄文字层 → 扫描件，交给 VLM 转录
    return {"lines": norm, "full_text": full_text}
