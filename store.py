# -*- coding: utf-8 -*-
"""结果落盘、L0 原件归档与汇总。"""
import json, hashlib, shutil, datetime
from pathlib import Path

import config


def _write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_original(path: Path) -> dict:
    """L0：原件原样复制进 archive/<stem>/，写 manifest（MD5/时间/大小）。
    同名同 MD5 不重复复制；同名不同 MD5（内容变了）覆盖并记录。"""
    path = Path(path)
    dst_dir = config.ARCHIVE_DIR / path.stem
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / path.name
    digest = md5_of(path)
    manifest_p = dst_dir / "manifest.json"
    if dst.exists() and manifest_p.exists():
        try:
            old = json.loads(manifest_p.read_text(encoding="utf-8"))
            if old.get("md5") == digest:
                return old  # 已归档且未变化
        except Exception:
            pass
    shutil.copy2(path, dst)
    manifest = {
        "source_name": path.name,
        "archived_name": dst.name,
        "md5": digest,
        "size_bytes": path.stat().st_size,
        "archived_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    manifest_p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def save_transcript(stem: str, transcript: dict):
    _write(config.TRANSCRIPT_DIR / f"{stem}.json", transcript)


def already_done(path: Path) -> bool:
    """该原件是否已处理过：L2 输出在、且 L0 归档记录的 MD5 与当前文件一致才算。
    监控重启靠它决定要不要再调一次模型——只看文件名存在就跳过，会把改过的票漏掉。"""
    path = Path(path)
    if not (config.OUTPUT_RAW_DIR / f"{path.stem}.json").exists():
        return False
    man = config.ARCHIVE_DIR / path.stem / "manifest.json"
    if not man.exists():
        return False                      # 没有归档凭证，宁可重跑一次也别漏单
    try:
        return json.loads(man.read_text(encoding="utf-8")).get("md5") == md5_of(path)
    except Exception:
        return False


def save_result(stem: str, raw: dict, air: dict):
    _write(config.OUTPUT_RAW_DIR / f"{stem}.json", raw)
    _write(config.OUTPUT_AIR_DIR / f"{stem}.json", air)


def save_qc(stem: str, qc: dict):
    """质检记录单独落一份：39 字段结果要保持干净（业务系统直读），红旗另存。"""
    _write(config.OUTPUT_QC_DIR / f"{stem}.json", qc)


def save_preview(stem: str, src: Path) -> Path:
    """电子单：把 LibreOffice 转出的 PDF 留一份。原件是 xlsx，浏览器渲染不了，
    审核台回看票面只能靠它——而且它正是模型实际看过的那张，对账口径一致。"""
    dst = config.PREVIEW_DIR / f"{stem}.pdf"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def load_qc() -> dict:
    """stem -> 质检记录，供 Excel/数据库按来源文件名联结。没有目录就是空表。"""
    if not config.OUTPUT_QC_DIR.exists():
        return {}
    out = {}
    for f in sorted(config.OUTPUT_QC_DIR.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue    # 单条质检记录坏了不能拖垮整表导出
    return out


def rebuild_summary():
    """扫描输出目录重建 all_hawbs.json / all_hawbs_air.json。"""
    def collect(d: Path):
        return [json.loads(f.read_text(encoding="utf-8"))
                for f in sorted(d.glob("*.json"))
                if not f.name.startswith("all_")]
    raws = collect(config.OUTPUT_RAW_DIR)
    airs = collect(config.OUTPUT_AIR_DIR)
    (config.OUTPUT_RAW_DIR / "all_hawbs.json").write_text(
        json.dumps(raws, ensure_ascii=False, indent=2), encoding="utf-8")
    (config.OUTPUT_AIR_DIR / "all_hawbs_air.json").write_text(
        json.dumps(airs, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(raws), len(airs)


def list_inputs(inp: Path):
    if inp.is_file():
        return [inp]
    return sorted(f for f in inp.iterdir()
                  if f.suffix.lower() in config.ALL_EXTS and not f.name.startswith("~$"))
