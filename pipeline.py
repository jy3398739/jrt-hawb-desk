# -*- coding: utf-8 -*-
"""三层处理管道：
  L0 归档（jobs 层） → L1 逐字转录 → L2 原文口径 全字段 → L3 航空口径 全字段
L1 一律优先直读 PDF 文字层（零 API 调用、逐字真值）：XLSX/XLS 先由 LibreOffice 按打印版面
导出 PDF 再直读；PDF 有文字层（业务系统直出）同样直读；照片型扫描件无文字层才退回 VLM 转录。
L2/L3 全通道同一条 VLM + to_air 链路。
"""
from pathlib import Path

import config
import vlm_extract
import transcribe
import to_air
import xlsx2pdf


def transcribe_l1(path) -> dict:
    """L1：能直读文字层就直读，读不到（扫描件）才让 VLM 转录。"""
    tr = None
    if Path(path).suffix.lower() == ".pdf":
        tr = transcribe.transcribe_pdf_text_layer(path, config.L1_TEXT_MIN_CHARS)
    return tr or transcribe.transcribe_one(path)


def process_file(path, do_transcript: bool = True) -> dict:
    """返回 {raw, air, channel, transcript, src}。src 是真正送进管道的文件：
    电子单是转出的 PDF，其余就是原件——审核台回看票面要的就是这一张。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in config.XLS_EXTS:
        src = xlsx2pdf.convert_excel_to_pdf(path)
    elif ext in config.IMG_EXTS:
        src = path
    else:
        raise ValueError(f"不支持的文件类型: {path.suffix}")
    tr = transcribe_l1(src) if do_transcript else None
    raw = vlm_extract.extract_one(src, transcript=tr)
    air = to_air.to_air(raw)
    channel = "excel" if ext in config.XLS_EXTS else "vlm"
    return {"raw": raw, "air": air, "channel": channel, "transcript": tr, "src": str(src)}
