# -*- coding: utf-8 -*-
"""结果落盘、L0 原件归档与汇总。"""
import json, hashlib, os, shutil, datetime, threading, re
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


# === 提交台账 + (主单号|分单号)→原件 索引 ===
# 仅制单员人工点提交、回传公司成功后写这里；机批/监控/站点只落盘、绝不进台账。
# 因此"进了台账"就等于"号齐全、过了门"——索引直接从台账派生，不必再防脏数据/冲突。
_LEDGER_LOCK = threading.Lock()        # 同进程内多制单员并发提交（handle_file 走线程池）时串行化读改写


def norm_no(value: str) -> str:
    """单号归一化：大写、去掉空格与连字符等非字母数字。挡的是格式差异（235-96146363 vs
    23596146363），挡不了语义差异（公司若不发主单前三位是另一回事，那要接真接口时对齐）。"""
    return re.sub(r"[^0-9A-Z]", "", str(value or "").upper())


def number_key(mawb: str, hawb: str) -> str:
    """复合索引键。主单号不重复 → (主单|分单) 天然唯一，不需要按分单号单独去重。"""
    return f"{norm_no(mawb)}|{norm_no(hawb)}"


_MAWB_DIR_RE = re.compile(r"[0-9A-Z]{6,20}")


def mawb_source_dir(mawb: str):
    """主单原件在本机的目录：output/mawb_source/<归一化主单号>/。

    主单号先归一化再校验字符集与长度，非法（含 '..'、斜杠、纯符号）一律当"没有"返回 None——
    目录名直接拼进路径就是穿越口子，不能只靠"目录存在"兜底。"""
    key = norm_no(mawb)
    if not _MAWB_DIR_RE.fullmatch(key):
        return None
    d = config.MAWB_SOURCE_DIR / key
    return d if d.is_dir() else None


def mawb_source_key(mawb: str) -> str:
    """归一化后可作目录名的主单号；不合格返回空串（检索结果里告诉前端有没有原件）。"""
    key = norm_no(mawb)
    return key if _MAWB_DIR_RE.fullmatch(key) else ""


def _load_ledger(path=None) -> dict:
    path = path or config.SUBMIT_LEDGER
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}          # 台账读坏宁可当空表重来，也别让一次提交把服务打挂


def _write_ledger(data: dict, path=None) -> None:
    """原子写并收紧权限：内容含复核人姓名与回执，posix 上收进 600（同 auth._write 的套路，
    否则 tmp 按 umask 建、os.replace 会把原文件 600 放宽成 664）。"""
    path = path or config.SUBMIT_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    mode = (path.stat().st_mode & 0o7777) if path.exists() else 0o600
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass                                  # Windows 的 chmod 只管只读位，失败不拦写入
    os.replace(tmp, path)


def mark_submitted(stem: str, mawb: str, hawb: str, reviewer: str, receipt: dict = None,
                   acked_flags: list = None) -> dict:
    """记一笔提交（覆盖同 stem 旧记录，重提交以最新为准）。返回写进去的条目。
    acked_flags：复核员在审核台点「确认无误」放行的红旗原文，留痕备查。"""
    with _LEDGER_LOCK:
        data = _load_ledger()
        entry = {"stem": stem, "mawb": str(mawb or "").strip(), "hawb": str(hawb or "").strip(),
                 "key": number_key(mawb, hawb), "reviewer": reviewer,
                 "submitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "acked_flags": [str(a) for a in (acked_flags or [])],
                 "receipt": receipt or {}}
        data[stem] = entry
        _write_ledger(data)
    return entry


def ledger() -> dict:
    """stem → 提交条目。给 /results 之类展示与索引用；纯读不加锁。"""
    return _load_ledger()


def mark_master_submitted(mawb: str, reviewer: str, receipt: dict = None,
                          acked_flags: list = None, snapshot: dict = None) -> dict:
    """主单提交台账（键=归一化主单号）。与分单台账分开：那边以 stem 为键，主单没有原件 stem。
    snapshot 存提交前公司侧的 AMS_RECORD——整表写回会把没给的列写成 NULL，出事时要能看出抹掉了什么。"""
    key = norm_no(mawb)
    with _LEDGER_LOCK:
        data = _load_ledger(config.MASTER_LEDGER)
        entry = {"mawb": str(mawb or "").strip(), "reviewer": reviewer,
                 "submitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "action": (receipt or {}).get("action"),
                 "acked_flags": [str(a) for a in (acked_flags or [])],
                 "receipt": receipt or {}, "snapshot": snapshot or {}}
        data[key] = entry
        _write_ledger(data, config.MASTER_LEDGER)
    return entry


def master_ledger() -> dict:
    """归一化主单号 → 主单提交条目。"""
    return _load_ledger(config.MASTER_LEDGER)


def number_index() -> dict:
    """派生：复合键 → {stem, mawb, hawb}。只含已提交集，号天然齐全。
    每次从台账现算（台账是本地小 JSON，几十~几千条，读一遍毫秒级）；量大再上内存缓存。"""
    out = {}
    for e in _load_ledger().values():
        k = e.get("key") or number_key(e.get("mawb", ""), e.get("hawb", ""))
        if k and k != "|":                    # 两个号都空的坏条目直接跳过，不进索引
            out[k] = {"stem": e.get("stem"), "mawb": e.get("mawb", ""), "hawb": e.get("hawb", "")}
    return out


def lookup_stem(mawb: str, hawb: str):
    """录入员检索用：给主单号+分单号 → 本机已归档原件的 stem（打不开原件时返回 None）。"""
    return (number_index().get(number_key(mawb, hawb)) or {}).get("stem")


def submitted_by_mawb(mawb: str) -> list:
    """按主单号列出本机已提交（=号齐全过了门、原件在 ARCHIVE）的分单条目。
    录入员检索的本地数据源：真实的公司"该主单下全部分单"接口待 IT 契约，先用这个把链路跑通。
    归一化后比对，挡 235-96146363 / 23596146363 这类格式差异。"""
    want = norm_no(mawb)
    if not want:
        return []
    return [e for e in _load_ledger().values() if norm_no(e.get("mawb", "")) == want]
