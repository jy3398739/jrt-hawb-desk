# -*- coding: utf-8 -*-
"""主单（MAWB）解析管道：公司资料 → L1 文本 → L2 主单 36 列 → 清洗 → 红旗 + 保真回查 → 缓存。

与分单的关系（2026-09-28 用户定案「主单是主单，分单是分单」）：共用**引擎件**——转录形状、
VLM 调用与重试、保真回查、红旗/提交门/台账范式；不共用字段表——主单不过 `to_air` 的 IATA 归一，
也不进 `output/raw|air|qc`、Excel、数据库。解析结果就是 `mawb2/` 要的 `AMS_RECORD`，
人工核对后可提交回公司。

花钱口径：每次检索都自动解析（用户定案），但缓存按**资料指纹** `text_md5` 失效——公司改了
才重跑，没改不再调模型。同一主单并发只有一个真跑。
"""
import datetime
import hashlib
import json
import os
import re
import threading
import time

import config
import company_api
import master_fields as mf
import store
import vlm_extract
from fidelity import verify_fidelity
from validator import validate_master

MASTER_COLS = mf.MASTER_COLS
# 保真回查按主单列面走：公司名/地址是长字段（跨行排版，用词元覆盖率兜底）；
# SLAC 是数值、GOODS_INFO_HSCODE 是公司要求归一的列（去点号逗号连接）——逐字回查它们等于自己打自己脸，
# 都交给 validator 与反向漏抄核查管（分单侧同样不回查 GOODS_HS_CODE）。
_LONG = [c for c in MASTER_COLS if c.endswith(("_COMP_NAME", "_COMP_ADDRESS"))]
_SHORT = [c for c in MASTER_COLS if c not in _LONG and c not in ("SLAC", "GOODS_INFO_HSCODE")]

_lock = threading.Lock()


class MasterFlagged(RuntimeError):
    """主单提交门拦下：还有没清、也没逐条「确认无误」的红旗，或者没人署名。
    `flags` 带着原文，前端据此逐条出「确认无误」按钮，不用解析提示文案。"""

    def __init__(self, msg: str, flags: list = None):
        super().__init__(msg)
        self.flags = list(flags or [])


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def key_for(mawb: str) -> str:
    """主单号 → 安全的缓存文件名。归一化顺带把穿越字符剥光，再按字符集/长度校验。"""
    return store.mawb_source_key(mawb)


def path_for(mawb: str):
    key = key_for(mawb)
    return config.MASTER_DIR / (key + ".json") if key else None


def read_master(mawb: str) -> dict | None:
    p = path_for(mawb)
    if not p or not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def _write(rec: dict) -> dict:
    p = path_for(rec.get("mawb", ""))
    if p is None:
        raise ValueError(f"主单号不合法，无法落盘: {rec.get('mawb')!r}")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass                                  # Windows 的 chmod 只管只读位
    os.replace(tmp, p)
    rec["path"] = str(p)
    return rec


def master_flags(ams: dict, transcript: dict | None = None) -> list:
    """主单红旗清单（服务端重算用，前端传来的旗一律不信）。

    漏取也算旗：提交门重算时若不算它，"资料里有值但这次没填"的列会直接被写成 NULL。"""
    cleaned = mf.clean_ams(ams)
    flags = validate_master(cleaned, transcript)
    if transcript:
        flags += _missed_flags(cleaned, transcript)[0]
    return flags


def _face_md5() -> str:
    """提示词 + 列面的指纹。改了 prompt、加了列，旧解析结果必须作废——否则页面会一直
    投喂过期结果（2026-09-29 用户实撞：176 那条显示 5/36，重读公司接口实有 21 列）。"""
    return _md5(vlm_extract.MASTER_PROMPT_HEAD + "|" + ",".join(mf.MASTER_COLS))


_AMS_LINE = re.compile(r"^([A-Z][A-Z0-9_]*):[ \t]*(.+)$")
_COL_SET = set(MASTER_COLS)


def missed_cols(ams: dict, transcript: dict) -> list:
    """L1 里明明有「列名: 值」、结果却是空 → 模型漏取，不是"资料里没有"。

    这两件事必须分开说：混在一起，复核人只能对着一屏空格子猜；而 mawb2 是整表写回，
    漏取的那一列提交后会被写成 NULL，等于用一次提交把公司库里已有的值抹掉。"""
    have: dict[str, str] = {}
    for x in (transcript or {}).get("lines") or []:
        m = _AMS_LINE.match(str(x.get("text", "")))
        if m and m.group(1) in _COL_SET and m.group(2).strip():
            have.setdefault(m.group(1), m.group(2).strip())
    return [k for k in MASTER_COLS if not (ams or {}).get(k) and have.get(k)]


def _missed_flags(ams: dict, transcript: dict) -> tuple[list, list]:
    cols = missed_cols(ams, transcript)
    have = {}
    for x in (transcript or {}).get("lines") or []:
        m = _AMS_LINE.match(str(x.get("text", "")))
        if m:
            have.setdefault(m.group(1), m.group(2).strip())
    flags = [f"{k} 漏取：资料里有「{have[k][:60]}」而这一列是空，直接提交会把公司库里这列写成 NULL"
             for k in cols]
    return flags, cols


def fresh_record(mawb: str, mawb_order: dict, force: bool = False) -> dict | None:
    """缓存里那份还作不作数（资料、提示词/列面、模型三项都对得上）。

    路由必须先问这个再回前端：过期却报 `done`，前端就不轮询了，页面会停在一次过期解析上
    ——而改了提示词或换模型后每条主单都会命中这个坑。"""
    if force:
        return None
    rec = read_master(mawb)
    if not rec or rec.get("state") not in ("done", "failed"):
        return None
    tr = mf.build_transcript(mawb_order)
    if (rec.get("text_md5") == _md5(tr["full_text"]) and rec.get("face_md5") == _face_md5()
            and rec.get("model") == config.master_model_bundle()["model"]):
        return rec
    return None


def run_master(mawb: str, mawb_order: dict) -> dict:
    """就地解析一条主单并落盘。失败也落盘（state=failed + 原因），别让它在页面上隐身。"""
    tr = mf.build_transcript(mawb_order)
    bundle = config.master_model_bundle()
    t0 = time.time()
    rec = {"mawb": str(mawb or "").strip(), "text_md5": _md5(tr["full_text"]),
           "face_md5": _face_md5(), "model": bundle["model"],
           "started_at": _now(), "transcript": tr,
           "outer": {k: (mawb_order or {}).get(k) for k in mf.OUTER_REF_COLS},
           "ams": {}, "ams_raw": {}, "qc": None, "error": ""}
    try:
        raw = vlm_extract.extract_master(tr, model=bundle)
        ams = mf.clean_ams(raw)
        fid = verify_fidelity(raw, tr, short=_SHORT, long=_LONG,
                              hs_field="GOODS_INFO_HSCODE", tax=False)
        flags = validate_master(ams, tr) + _missed_flags(ams, tr)[0]
        rec.update({"state": "done", "ams_raw": raw, "ams": ams,
                    "qc": {"flags": flags, "needs_review": bool(flags),
                           "missed_cols": missed_cols(ams, tr),
                           "fidelity": {"checked": fid["checked"], "passed": fid["passed"],
                                        "failed": fid["failed"], "sources": fid["sources"]},
                           "missing_mawb": [], "source_name": "公司主单资料"}})
    except Exception as e:
        rec.update({"state": "failed", "error": f"{type(e).__name__}: {e}",
                    "qc": {"flags": [f"主单解析失败 {e}"], "needs_review": True,
                           "missed_cols": [], "fidelity": None}})
    rec["elapsed"] = round(time.time() - t0, 1)
    rec["updated_at"] = _now()
    return _write(rec)


def public(rec: dict | None) -> dict | None:
    """给前端的形态：附上 36 列的列名/中文名/分组，以及只读 meta 行（JOB_ID 那类主单多出来、
    不参与提交的列）。这份表只有 master_fields 一个真源，前端抄一份迟早和后端漂移。"""
    if rec is None:
        return None
    out = dict(rec)
    out["fields"] = [list(f) for f in mf.MASTER_FIELDS]
    out["groups"] = dict(mf.MASTER_GROUP_LABELS)
    outer = rec.get("outer") or {}
    out["meta"] = [{"col": c, "label": lab, "group": g, "value": outer.get(c)}
                   for c, lab, g in mf.MASTER_META_COLS if outer.get(c) not in (None, "")]
    return out


def ensure(mawb: str, mawb_order: dict, force: bool = False) -> dict:
    """保证有一份"跟当前公司资料对齐"的解析结果，返回该记录。

    指纹命中直接回缓存（不再花钱）；没资料就地失败（不能让模型凭空编一条主单）；
    要重跑时先把 state=parsing 写下去再干活——并发的第二个请求看到 parsing 就只跟不抢。
    force=True 越过缓存：换了模型或想把上一次的失败重试一次。"""
    tr = mf.build_transcript(mawb_order)
    md5, face = _md5(tr["full_text"]), _face_md5()
    model = config.master_model_bundle()["model"]
    with _lock:
        rec = read_master(mawb)
        if rec and rec.get("state") == "parsing":
            return rec
        if (rec and not force and rec.get("text_md5") == md5 and rec.get("face_md5") == face
                and rec.get("model") == model and rec.get("state") in ("done", "failed")):
            return rec
        if not tr["lines"]:
            return _write({"mawb": str(mawb or "").strip(), "text_md5": md5, "face_md5": face,
                           "model": model, "state": "failed",
                           "transcript": tr, "ams": {}, "ams_raw": {},
                           "error": "主单资料文本为空，无从解析：公司接口没返回这条主单的资料",
                           "qc": {"flags": ["主单资料为空"], "needs_review": True, "fidelity": None},
                           "started_at": _now(), "updated_at": _now(), "elapsed": 0.0})
        _write({"mawb": str(mawb or "").strip(), "text_md5": md5, "face_md5": face, "model": model,
                "state": "parsing", "transcript": tr, "ams": {}, "ams_raw": {},
                "error": "", "qc": None, "started_at": _now(), "updated_at": _now()})
    return run_master(mawb, mawb_order)


def submit_master(payload: dict) -> dict:
    """把人工核对过的主单回传公司（mawb2）。门与分单同构：署名 + 红旗清零或逐条确认留痕，
    而且**红旗在服务端按当前值重算**——前端把旗藏了也提不出去。"""
    p = payload or {}
    mawb = str(p.get("mawb") or "").strip()
    reviewer = str(p.get("reviewer") or "").strip()
    ams = p.get("ams") or {}
    acked = [str(a) for a in (p.get("acked_flags") or [])]
    if not reviewer:
        raise MasterFlagged("提交主单要先署名：复核人不能为空")
    cached = read_master(mawb) or {}
    flags = master_flags(ams, cached.get("transcript"))
    left = [f for f in flags if f not in acked]
    if left:
        raise MasterFlagged("主单还有未确认的红旗，逐条点「确认无误」后才能回传公司：\n" +
                            "\n".join(left), left)
    # 只发调用方给过的列（clean_ams 会丢掉没给的）：mawb2 是整表写回，
    # "这次没碰"要由 company_api 用库里的现值补齐，"人工清空"才是显式 null。
    res = company_api.submit_master({"mawb": mawb, "ams": mf.clean_ams(ams)})
    store.mark_master_submitted(mawb, reviewer, receipt=res, acked_flags=acked,
                                snapshot=res.get("before"))
    return res
