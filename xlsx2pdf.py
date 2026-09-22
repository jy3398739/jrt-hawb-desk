# -*- coding: utf-8 -*-
"""Excel → PDF：用 LibreOffice 无头模式按票面打印版面导出 PDF，交给 VLM 通道。
版面还原度远高于自绘网格（合并单元格、边框、套打标签、字体都在 PDF 里）。
"""
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import config

_WIN_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def find_soffice() -> Path:
    """定位 soffice 可执行文件；找不到时抛错并提示安装。"""
    override = config.SOFFICE_PATH
    if override:
        p = Path(override)
        if not p.exists():
            raise FileNotFoundError(f"SOFFICE_PATH 指向的文件不存在: {override}")
        return p
    for cand in _WIN_CANDIDATES:
        p = Path(cand)
        if p.exists():
            return p
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise FileNotFoundError(
        "未找到 LibreOffice。电子单需转成 PDF 后走 VLM，请安装 LibreOffice "
        "(https://www.libreoffice.org/download/) 或在 .env 里设 SOFFICE_PATH 指向 soffice.exe"
    )


def convert_excel_to_pdf(path, out_dir=None) -> Path:
    """返回生成的 PDF 路径。out_dir 默认系统临时目录下的 hawb_xlsx_pdf。"""
    path = Path(path)
    if out_dir is None:
        out_dir = Path(tempfile.gettempdir()) / "hawb_xlsx_pdf"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    soffice = find_soffice()
    # 独立用户配置目录：避开与已打开的 LibreOffice 窗口抢用户配置锁，
    # 也保证并发/连续调用互不干扰（--headless 复用到运行中的实例会静默失败）。
    profile = Path(tempfile.gettempdir()) / f"hawb_lo_profile_{uuid.uuid4().hex[:8]}"
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(soffice),
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless", "--norestore", "--invisible",
        "--convert-to", "pdf:calc_pdf_Export",
        "--outdir", str(out_dir),
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=config.XLSX_PDF_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Excel 转 PDF 超时({config.XLSX_PDF_TIMEOUT}s): {path.name}")
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    pdf = out_dir / (path.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(
            f"Excel 转 PDF 失败: {path.name} (rc={proc.returncode}) "
            f"{(proc.stdout or '').strip()[:200]} {(proc.stderr or '').strip()[:200]}"
        )
    return pdf
