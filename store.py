# -*- coding: utf-8 -*-
"""结果落盘、L0 原件归档与汇总。"""
import json, hashlib, shutil, datetime, threading, re
from pathlib import Path

import config
import atomic
import fieldspec


def _write(path: Path, data: dict):
    atomic.write_json(path, data)


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
    atomic.write_json(manifest_p, manifest)      # 归档台账也不能裸写：截断一次这份原件就查无来源
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
    """质检记录单独落一份：全字段结果要保持干净（业务系统直读），红旗另存。"""
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
    atomic.write_json(config.OUTPUT_RAW_DIR / "all_hawbs.json", raws)
    atomic.write_json(config.OUTPUT_AIR_DIR / "all_hawbs_air.json", airs)
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


def safe_stem(stem: str):
    """URL/请求体里的 stem 只当单段文件名的那半段用：带 '/'、'\\'、'..'、空名一律不认。

    暂存文件的路径是 `STAGED_DIR/<stem>.json`，stem 由前端给——不先剥掉穿越就拼路径，
    ../../ 就能写到服务器上任何地方。转录目录那侧从前在 server 里有一份同样的判断，
    现在两边共用这一份（写文件的入口比读文件的入口更需要它）。"""
    name = str(stem or "").replace("\\", "/").split("/")[-1]
    if not name or name in (".", "..") or name != str(stem or ""):
        return None
    return name


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
    """原子写并收紧权限：内容含复核人姓名与回执，posix 上新建的台账收进 600（已存在沿用原权限）。"""
    atomic.write_json(path or config.SUBMIT_LEDGER, data, mode=0o600)


def mark_submitted(stem: str, mawb: str, hawb: str, reviewer: str, receipt: dict = None,
                   acked_flags: list = None, edited_fields: list = None,
                   sent_fingerprint: str = "", company_action: str = "",
                   air_sent: dict = None, stager: str = "", staged_at: str = "",
                   no_inputter_review: bool = False) -> dict:
    """记一笔提交（覆盖同 stem 旧记录，重提交以最新为准）。返回写进去的条目。
    acked_flags：复核员在审核台点「确认无误」放行的红旗原文，留痕备查。
    sent_fingerprint / company_action / before：这次到底发了什么、公司怎么回的、发之前库里是什么——
    出事时要能回答"这列的 NULL 是谁写进去的"（j9 整表写回，没带的列就是 NULL）。
    air_sent：这次真发给公司的那份全量值。光有哈希对不回内容，重算一次又会被"人已经改了"污染；
    stager/staged_at：发之前是谁暂存（核对）过的——按人算修改量与"未经录入员复核"都读它。
    no_inputter_review：这批里没有录入员复核过的暂存记录、制单员点了确认就直发的记号。
    它必须逐条留在台账里：以后问"跳过复核直接发的那批错得多不多"，分母就在这儿。"""
    with _LEDGER_LOCK:
        data = _load_ledger()
        entry = {"stem": stem, "mawb": str(mawb or "").strip(), "hawb": str(hawb or "").strip(),
                 "key": number_key(mawb, hawb), "reviewer": reviewer,
                 "submitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "acked_flags": [str(a) for a in (acked_flags or [])],
                 "edited_fields": [str(x) for x in (edited_fields or [])],
                 "sent_fingerprint": sent_fingerprint or "",
                 "company_action": company_action or "",
                 "air_sent": air_sent if isinstance(air_sent, dict) else {},
                 "stager": str(stager or ""), "staged_at": str(staged_at or ""),
                 "no_inputter_review": bool(no_inputter_review),
                 "receipt": receipt or {}}
        if isinstance((receipt or {}).get("before"), dict):
            entry["before"] = receipt["before"]
        data[stem] = entry
        _write_ledger(data)
    return entry


def mark_staged_submitted(stem: str, submitter: str, sent_fingerprint: str) -> dict | None:
    """给暂存记录盖上"已发出"的章：谁发的、什么时候、发的那份指纹。

    只盖章、不搬文件也不删文件——状态仍然由"到哪一层"决定，历史留在这台机器上。
    没暂存过返回 None（提交不该顺手造一份暂存记录，那会把"没人核对过"伪装成"核对过"）。"""
    with _STAGED_LOCK:
        rec = load_staged(stem)
        if rec is None:
            return None
        rec["submitted_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        rec["submitter"] = str(submitter or "")
        rec["sent_fingerprint"] = sent_fingerprint or ""
        atomic.write_json(_staged_file(rec.get("stem") or stem), rec, mode=0o600)
    return rec


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
    """录入员检索用：给主单号+分单号 → 本机**点得开**原件的 stem，打不开一律返回 None。

    光在台账里有一条记录 ≠ 原件还在这台机器上：归档可能被清、票也可能是在别的机器解析后
    在这里提交的。带着这种 stem 去渲染「查看原件」，点开只剩一坨裸 JSON 404，录入员对不了
    票面还会以为系统坏了——所以这里就地把"有没有原件"查实，让 stem 这一个字段说一件事。"""
    stem = (number_index().get(number_key(mawb, hawb)) or {}).get("stem")
    if not stem:
        return None
    return stem if (config.ARCHIVE_DIR / str(stem)).is_dir() else None


def submitted_by_mawb(mawb: str) -> list:
    """按主单号列出本机已提交（=号齐全过了门、原件在 ARCHIVE）的分单条目。
    录入员检索的本地数据源：真实的公司"该主单下全部分单"接口待 IT 契约，先用这个把链路跑通。
    归一化后比对，挡 235-96146363 / 23596146363 这类格式差异。"""
    want = norm_no(mawb)
    if not want:
        return []
    return [e for e in _load_ledger().values() if norm_no(e.get("mawb", "")) == want]


def ticket_rows(date: str = None, state: str = None, mawb: str = None) -> list:
    """这台服务器经手过的票，一行一张，**状态从盘上现算**。

    状态只有四个来源，不看任何前端状态：有质检记录=parsed；另有暂存文件=staged；
    进了提交台账=submitted；质检记录带 error=failed。
    "解析了但没提交"从前只活在某个人浏览器的 localStorage 里（desk.js 的 pendingUnder），
    换个人查主单就看不见——这张表就是把它搬到服务器上（需求一、需求二共用）。

    date 给定时只看那一天（今日台账走这条路）；state/mawb 给定时筛。
    计数不在这里算：那套"今天 N 张 · 已提交 M · 待提交 K"的口径归 logic.js 一处，
    服务器再算一遍就是两套算术，迟早对不上（test_day_ledger 盯着）。
    """
    if not config.OUTPUT_QC_DIR.exists():
        return []
    led = _load_ledger()
    found = []
    for f in sorted(config.OUTPUT_QC_DIR.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue                     # 一条坏记录不该拖挂整页（与 load_qc 同口径）
        when = str(rec.get("processed_at", ""))
        if date and not when.startswith(date):
            continue
        found.append((f.stem, rec))
    found.sort(key=lambda kv: str(kv[1].get("processed_at")))
    want_mawb = norm_no(mawb) if mawb else ""
    rows = []
    for stem, rec in found:
        try:
            air = json.loads((config.OUTPUT_AIR_DIR / f"{stem}.json").read_text(encoding="utf-8"))
        except Exception:
            air = {}
        e = led.get(stem) or {}
        st = load_staged(stem) or {}
        if rec.get("error"):
            state_of = "failed"
        elif e:
            state_of = "submitted"
        elif st:
            state_of = "staged"
        else:
            state_of = "parsed"
        if state and state_of != state:
            continue
        row_mawb = e.get("mawb") or air.get("MAWB_NO", "") or st.get("mawb", "")
        if want_mawb and norm_no(row_mawb) != want_mawb:
            continue
        rows.append({"stem": stem, "source_name": rec.get("source_name") or stem,
                     "channel": rec.get("channel") or "", "elapsed": rec.get("elapsed"),
                     "processed_at": rec.get("processed_at") or "",
                     "state": state_of,
                     "uploader": rec.get("uploader") or "",
                     "uploader_role": rec.get("uploader_role") or "",
                     "model": rec.get("model") or st.get("model") or "",
                     "mawb": row_mawb,
                     "hawb": e.get("hawb") or air.get("HAWB_NO", "") or st.get("hawb", ""),
                     "flags": len(rec.get("flags") or []),
                     "needs_review": bool(rec.get("needs_review")),
                     "failed": bool(rec.get("error")),
                     "error": rec.get("error") or "",
                     "stager": st.get("stager") or "", "stager_role": st.get("stager_role") or "",
                     "staged_at": st.get("staged_at") or "",
                     "edited": len(st.get("edits") or {}) if st else 0,
                     "reviewed_by_inputter": reviewed_by_inputter(st) if st else False,
                     "submitted": ({"reviewer": e.get("reviewer") or "", "at": e.get("submitted_at") or "",
                                    "action": e.get("company_action") or ""} if e else None),
                     "has_original": (config.ARCHIVE_DIR / stem).is_dir()})
    return rows


def day_rows(date: str) -> list:
    """某一天经手过的票。今日台账那一页读的就是它（`ticket_rows` 的按天视图）。"""
    return ticket_rows(date=date)


# === 暂存：人工核对结果落服务器，但不发公司 ===
# 状态到这里为止都只是"存工作进度给同事看"。为什么不并进提交台账：那边一条=一次真回传，
# 混进来会让"进了台账就等于号齐全、过了门、回传过公司"这句前提失效（复合键索引靠它建）。
_STAGED_LOCK = threading.Lock()        # 同进程两人同时暂存同一张票：读改写要串行
_NUM_FIELDS = set(fieldspec.NUM)       # 件数/SLAC/重量：按数值比，170.0 与 170 不算改
_NO_FIELDS = ("MAWB_NO", "HAWB_NO")    # 两个单号：按归一化比，连字符有无不算改


def _fold(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def _as_num(v):
    try:
        return float(str(v).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def same_value(field, a, b) -> bool:
    """人有没有真改这一列。统计要的是"模型读错了"，不是"人重新敲了一遍"：
    空格数、单号里的连字符、170.0 与 170 都不算改动；大小写算（Italy→ITALY 是人动过）。

    这里不套 fidelity 的归一：那边为了"在转录里找到原文"还要把全角标点折成半角，
    而这里比的是人工回填值与模型输出值，多折一层会把人真改过的标点藏掉。"""
    if not _fold(a) and not _fold(b):
        return True
    if field in _NO_FIELDS:
        return norm_no(a) == norm_no(b)
    if field in _NUM_FIELDS:
        na, nb = _as_num(a), _as_num(b)
        if na is not None and nb is not None:
            return na == nb
    return _fold(a) == _fold(b)


def field_diff(old: dict, new: dict) -> dict:
    """逐字段差异 {列: {"from": 旧值, "to": 新值}}，只收真改过的那几列。"""
    out = {}
    for k in sorted(set(old or {}) | set(new or {})):
        a, b = (old or {}).get(k), (new or {}).get(k)
        if not same_value(k, a, b):
            out[k] = {"from": a if a is not None else "", "to": b if b is not None else ""}
    return out


def _staged_file(stem: str):
    name = safe_stem(stem)
    return None if name is None else config.STAGED_DIR / f"{name}.json"


def load_staged(stem: str):
    """读一份暂存记录；没有、名字不合法、或文件写坏了都返回 None（不能让一张坏票拖垮列表）。"""
    p = _staged_file(stem)
    if p is None or not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def stage_ticket(stem: str, air: dict, by: str, role: str, acked_flags: list = None) -> dict:
    """把这张票当前的人工核对结果存到服务器上，并记下"这一步是谁、改了哪几列"。

    三条口径是刻意的：
    ① **基线是模型原样**，取 output/air 那份且只在第一次暂存时快照——重解析会覆盖 air，
       不做快照的话，改过一次的票再重跑一遍，"模型当初读了什么"就永远丢了；
    ② **差异由服务器算**，不信前端传来的 edited_fields（前端说没改就是没改的话，
       统计可以被浏览器随意抹平）；
    ③ **每次暂存追加一条事件**而不是覆盖：同一张票先制单员改、再录入员改，
       盖成一份就答不出"这一列是模型错还是人错"，而需求三要的正是分开算。
    """
    name = safe_stem(stem)
    if name is None:
        raise ValueError(f"stem 不合法（不接受带路径的名字）：{stem!r}")
    air = air if isinstance(air, dict) else {}
    qc = {}
    try:
        qc = json.loads((config.OUTPUT_QC_DIR / f"{name}.json").read_text(encoding="utf-8"))
    except Exception:
        pass                       # 别的机器解析的票没有 qc：留痕照记，模型名空着就是了
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _STAGED_LOCK:
        prev = load_staged(name) or {}
        base = prev.get("air_model") if isinstance(prev.get("air_model"), dict) else None
        if base is None:
            try:
                base = json.loads((config.OUTPUT_AIR_DIR / f"{name}.json").read_text(encoding="utf-8"))
            except Exception:
                base = {}
        last = prev.get("air_final") if isinstance(prev.get("air_final"), dict) else base
        edits = field_diff(last, air)
        events = list(prev.get("events") or [])
        events.append({"by": str(by or ""), "role": str(role or ""), "at": now,
                       "edits": edits, "acked_flags": [str(x) for x in (acked_flags or [])]})
        mawb, hawb = str(air.get("MAWB_NO", "") or "").strip(), str(air.get("HAWB_NO", "") or "").strip()
        rec = {"stem": name, "source_name": qc.get("source_name") or name,
               "mawb": mawb, "hawb": hawb, "key": number_key(mawb, hawb),
               "model": qc.get("model") or "", "model_choice": qc.get("model_choice") or "",
               "channel": qc.get("channel") or "", "parsed_at": qc.get("processed_at") or "",
               "uploader": qc.get("uploader") or "", "uploader_role": qc.get("uploader_role") or "",
               "air_model": base, "air_final": air, "edits": field_diff(base, air),
               "events": events, "staged_at": now, "stager": str(by or ""), "stager_role": str(role or ""),
               "submitted_at": prev.get("submitted_at") or "",
               "submitter": prev.get("submitter") or "",
               "sent_fingerprint": prev.get("sent_fingerprint") or ""}
        atomic.write_json(_staged_file(name), rec, mode=0o600)
    return rec


def reviewed_by_inputter(rec: dict) -> bool:
    """这张票有没有被录入员核对过（暂存过至少一次）。/submit 的"未经复核"门读的就是它。"""
    return any(str(e.get("role") or "") == "inputter" for e in (rec or {}).get("events") or [])
