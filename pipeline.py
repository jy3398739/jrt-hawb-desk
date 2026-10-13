# -*- coding: utf-8 -*-
"""三层处理管道：
  L0 归档（jobs 层） → L1 逐字转录 → L2 原文口径 全字段 → L3 航空口径 全字段
L1 一律优先直读 PDF 文字层（零 API 调用、逐字真值）：XLSX/XLS 先由 LibreOffice 按打印版面
导出 PDF 再直读；PDF 有文字层（业务系统直出）同样直读；照片型扫描件无文字层才退回 VLM 转录。
L2/L3 全通道同一条 VLM + to_air 链路。
"""
from pathlib import Path
import time

import config
import vlm_extract
import transcribe
import to_air
import xlsx2pdf


def render_pages(src) -> list:
    """票面页图渲染一次、L1 与 L2 共用。

    从前两层各调一遍 `img_to_data_urls`：同一份 PDF 开两次、每页 pymupdf 渲染两次、PNG+base64
    各编两次——一张四页扫描件白付约 1 秒，而四个解析槽位并发时内存里同时握着四份 base64
    （撞的是 MemoryMax）。这里不降采样也不改倍率：送进模型的字节与从前逐字相同。
    纯文本模型不下发票面图片（它会顺着编，见 config 注记），那一份永远是空的。"""
    return vlm_extract.img_to_data_urls(src) if config.MODEL_VISION else []


def transcribe_l1(path, urls=None) -> dict:
    """L1：能直读文字层就直读，读不到（扫描件）才让 VLM 转录。"""
    tr = None
    if Path(path).suffix.lower() == ".pdf":
        tr = transcribe.transcribe_pdf_text_layer(path, config.L1_TEXT_MIN_CHARS)
    return tr or transcribe.transcribe_one(path, urls=urls)


def process_file(path, do_transcript: bool = True) -> dict:
    """返回 {raw, air, channel, transcript, src, model, model_choice, timings}。
    src 是真正送进管道的文件：电子单是转出的 PDF，其余就是原件——审核台回看票面要的就是这一张。
    timings 是分段秒数（render/l1/l2/l3）：没有它，"这一秒值不值得省"只能靠猜。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in config.XLS_EXTS:
        src = xlsx2pdf.convert_excel_to_pdf(path)
    elif ext in config.IMG_EXTS:
        src = path
    else:
        raise ValueError(f"不支持的文件类型: {path.suffix}")
    timings, t0 = {}, time.perf_counter()
    urls = render_pages(src)
    timings["render"] = round(time.perf_counter() - t0, 2)
    t0 = time.perf_counter()
    tr = transcribe_l1(src, urls) if do_transcript else None
    timings["l1"] = round(time.perf_counter() - t0, 2)
    # 就在调用前一刻取模型名：管理员能在审核台热切模型，等建质检记录时再去读 config，
    # 一张解析了十分钟的票会被记到切换后的那个模型头上，按模型分账的统计就全错了。
    used = {"model": config.VLM_MODEL, "model_choice": config.VLM_MODEL_CHOICE}
    t0 = time.perf_counter()
    raw = vlm_extract.extract_one(src, transcript=tr, urls=urls)
    timings["l2"] = round(time.perf_counter() - t0, 2)
    t0 = time.perf_counter()
    air = to_air.to_air(raw)
    timings["l3"] = round(time.perf_counter() - t0, 2)
    channel = "excel" if ext in config.XLS_EXTS else "vlm"
    return {"raw": raw, "air": air, "channel": channel, "transcript": tr, "src": str(src),
            "timings": timings, **used}
