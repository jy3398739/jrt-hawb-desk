# -*- coding: utf-8 -*-
"""L1 文字层直读的回归：不依赖样本文件，测试自己造 PDF。
这条守住的是"电子单/业务系统 PDF 零 API 出转录，扫描件退回 VLM"的路由判定。
"""
import atexit
import shutil
import tempfile
from pathlib import Path

import pymupdf

import config
from transcribe import transcribe_pdf_text_layer

# 固定一个临时目录、退出时收走：原来每造一份 PDF 就 mkdtemp 一次且从不清理，
# 跑一次回归在 %TEMP% 里留 5 个空壳目录（本机一度攒到 384 个）。
_TMP = Path(tempfile.mkdtemp(prefix="hawb_l1_"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)


def _make_pdf(pages, path):
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page(width=595, height=842)     # A4 pt
        for i, t in enumerate(lines):
            page.insert_text((50, 60 + i * 20), t, fontsize=11)
    doc.save(str(path))
    doc.close()


def _tmp_pdf(pages):
    p = _TMP / "t.pdf"
    _make_pdf(pages, p)
    return p


def test_lines_and_bbox():
    p = _tmp_pdf([["MAWB 020-19025661", "HAWB TAO-5808 9795"]])
    tr = transcribe_pdf_text_layer(p, 1)
    assert tr, "有文字层的 PDF 不该返回 None"
    assert [x["text"] for x in tr["lines"]] == ["MAWB 020-19025661", "HAWB TAO-5808 9795"]
    assert [x["i"] for x in tr["lines"]] == [1, 2]
    assert tr["full_text"] == "MAWB 020-19025661\nHAWB TAO-5808 9795"
    for x in tr["lines"]:
        x1, y1, x2, y2 = x["bbox"]
        assert all(0 <= v <= 1000 for v in (x1, y1, x2, y2)), f"bbox 未归一化到 0-1000: {x['bbox']}"
        assert x1 < x2 and y1 < y2
    assert "page" not in tr["lines"][0], "单页不该带 page 标"
    # 自上而下的阅读顺序
    assert tr["lines"][0]["bbox"][1] < tr["lines"][1]["bbox"][1]


def test_multipage_lines_are_numbered():
    p = _tmp_pdf([["PAGE ONE SHIPPER"], ["PAGE TWO CONSIGNEE"]])
    tr = transcribe_pdf_text_layer(p, 1)
    assert [x.get("page") for x in tr["lines"]] == [1, 2]
    assert [x["i"] for x in tr["lines"]] == [1, 2], "行号要跨页连续"


def test_scan_like_pdf_falls_back_to_vlm():
    """无文字层（照片型扫描件）必须返回 None，由 pipeline 退回 VLM 转录。"""
    blank = _tmp_pdf([[]])
    assert transcribe_pdf_text_layer(blank, config.L1_TEXT_MIN_CHARS) is None
    thin = _tmp_pdf([["020-19025661"]])
    assert transcribe_pdf_text_layer(thin, 200) is None, "薄文字层应判为扫描件"
    assert transcribe_pdf_text_layer(thin, 1), "低于门限才退回 VLM，别把有字的票也退回"
