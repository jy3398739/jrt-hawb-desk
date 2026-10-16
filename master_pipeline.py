# -*- coding: utf-8 -*-
"""主单（MAWB）解析管道：公司资料 → L1 文本 → L2 主单 36 列 → 清洗 → 红旗 + 保真回查 → 缓存。

与分单的关系（2026-09-28 用户定案「主单是主单，分单是分单」）：共用**引擎件**——转录形状、
VLM 调用与重试、保真回查、红旗/提交门/台账范式；不共用字段表——主单不过 `to_air` 的 IATA 归一，
也不进 `output/raw|air|qc`、Excel、数据库。解析结果就是 `mawb3/`（提交发送）要的 `AMS_RECORD`，
人工核对后可提交回公司。

花钱口径：每次检索都自动解析（用户定案），但缓存按**资料指纹** `text_md5` 失效——公司改了
才重跑，没改不再调模型。同一主单并发只有一个真跑。
"""
import datetime
import hashlib
import json
import re
import threading
import time

import atomic
import config
import company_api
import master_fields as mf
import store
import usage
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
    atomic.write_json(p, rec, mode=0o600)      # 主单缓存里是公司资料，新建就收 600
    rec["path"] = str(p)
    return rec


def stage_master(mawb: str, ams: dict, by: str, role: str, acked_flags: list = None) -> dict:
    """把主单 36 列的当前人工值存进这条主单缓存（**不发公司**）。

    与分单的 `store.stage_ticket` 同一套形态（快照基线 + 追加事件 + 服务器算差异），
    但字段面是主单自己的 36 列——统一的是机制，不是业务契约。
    基线取第一次暂存时的 `ams`：重解析会覆盖它，不快照就查不回模型当初读了什么。"""
    rec = read_master(mawb)
    if not rec:
        raise ValueError("这条主单还没有解析结果（先在审核台检索一次主单号）")
    ams = ams if isinstance(ams, dict) else {}
    base = rec.get("ams_model")
    if not isinstance(base, dict) or not base:
        base = rec.get("ams") if isinstance(rec.get("ams"), dict) else {}
    last = rec.get("ams_final") if isinstance(rec.get("ams_final"), dict) else base
    now = _now()
    events = list(rec.get("events") or [])
    events.append({"by": str(by or ""), "role": str(role or ""), "at": now,
                   "edits": store.field_diff(last, ams),
                   "acked_flags": [str(x) for x in (acked_flags or [])]})
    rec.update({"ams_model": base, "ams_final": ams, "events": events,
                "edits": store.field_diff(base, ams),
                # 顶上这一格是"此刻还确认着哪些红旗"：每暂存一次照这次的清单覆盖，
                # 撤回的那条就跟着收回去。事件里逐次那份是历史，重开时还原现状读的是这一格。
                "acked_flags": [str(x) for x in (acked_flags or [])],
                "staged_at": now, "stager": str(by or ""), "stager_role": str(role or ""),
                "updated_at": now})
    return _write(rec)


def review_master(mawb: str, by: str, role: str) -> dict:
    """录入员"这条主单我看过了，先不发"——只追加一条留痕事件。

    与分单侧 `store.review_ticket` 同一套语义：留痕不是交付，所以 `ams_final`、暂存人、
    暂存时间一个都不动，公司那边也不碰（这张主单可能还要回去问制单员）。
    判据也不另起一处——`store.reviewed_by_inputter()` 认的就是事件里出现过 inputter。"""
    rec = read_master(mawb)
    if not rec:
        raise ValueError("这条主单还没有解析结果（先在审核台检索一次主单号）")
    events = list(rec.get("events") or [])
    events.append({"by": str(by or ""), "role": str(role or ""), "at": _now(),
                   "edits": {}, "kind": "review", "acked_flags": []})
    rec["events"] = events
    return _write(rec)


def mark_master_sent(mawb: str, by: str) -> dict | None:
    """给主单记录盖"已发出"的章；没暂存过就不盖章也不造记录。
    只留过复核痕（kind=review）也不算"暂存过"——判据是 `store.staged_edits` 那一处。"""
    rec = read_master(mawb)
    if not rec or not store.staged_edits(rec):
        return rec
    rec["submitted_at"] = _now()
    rec["submitter"] = str(by or "")
    return _write(rec)


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

    这两件事必须分开说：混在一起，复核人只能对着一屏空格子猜；而公司写口是整表写回，
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
    tr, _ = build_input(mawb, mawb_order, prev=rec)
    if (rec.get("text_md5") == _md5(tr["full_text"]) and rec.get("face_md5") == _face_md5()
            and rec.get("model") == config.master_model_bundle()["model"]):
        return rec
    return None


def _sent_back(mawb: str, prev: dict) -> dict:
    """本机发给过公司的那一版值：提交台账里的 `ams_sent`，加上一次暂存写出去的那份 `ams_final`
    （暂存走 `mawb2` 不写提交台账，只看台账就会漏掉"点了暂存还没提交"这一大一路），
    再并上记录里那份 `sent_back`——重解析会把 `ams_final` 盖掉，那笔账得自己活着。"""
    out = {}
    if isinstance((prev or {}).get("sent_back"), dict):
        out.update(prev["sent_back"])
    led = store.master_ledger().get(store.norm_no(mawb)) or {}
    if isinstance(led.get("ams_sent"), dict):
        out.update(led["ams_sent"])
    if isinstance((prev or {}).get("ams_final"), dict):
        out.update(prev["ams_final"])
    return out


def build_input(mawb: str, mawb_order: dict, prev: dict | None = None) -> tuple:
    """喂给模型的资料 = 公司录的那几行（本机写回的按 mf.original_lines 剔掉）。

    这一处必须只有算法的一份调用：`fresh_record`、`ensure`、`run_master` 三处都算指纹，
    各剔各的就会出现"界面显示的行与缓存认的行不是同一份"。返回 `(资料, 那几份账)`。"""
    raw = mf.build_transcript(mawb_order)
    if prev is None:
        prev = read_master(mawb) or {}
    if prev.get("state") == "parsing":
        prev = {}          # ensure 写的占位记录不是"上一次解析"，拿它当基线会把上一份洗掉
    sent = _sent_back(mawb, prev)
    had = [str(x or "").strip() for x in (prev.get("baseline") or []) if str(x or "").strip()]
    # 老票没钉过基线：退到上一次解析读到的那批行当对照，并把这个"证据不是当场钉的"如实带下去
    dirty = bool(prev.get("baseline_dirty")) or (bool(prev) and not had)
    if not had:
        had = [str(x.get("text", "")).split(":", 1)[0].strip()
               for x in ((prev.get("transcript") or {}).get("lines") or [])]
        had = [x for x in had if x]
    kept, gone, unknown = mf.original_lines(raw["lines"], had, sent)
    tr = {"lines": kept, "full_text": "\n".join(x["text"] for x in kept)}
    return tr, {"baseline": sorted(set(had)) if had
                else sorted({str(x["text"]).split(":", 1)[0].strip() for x in raw["lines"]}),
                "baseline_dirty": dirty,
                "sent_back": sent, "excluded_self": gone, "unknown_self": unknown,
                "self_filled": [x["label"] for x in gone] + [x["label"] for x in unknown]}


def run_master(mawb: str, mawb_order: dict, prev: dict | None = None) -> dict:
    """就地解析一条主单并落盘。失败也落盘（state=failed + 原因），别让它在页面上隐身。"""
    tr, pin = build_input(mawb, mawb_order, prev)
    bundle = config.master_model_bundle()
    t0 = time.time()
    usage.begin()
    rec = {"mawb": str(mawb or "").strip(), "text_md5": _md5(tr["full_text"]),
           "face_md5": _face_md5(), "model": bundle["model"],
           "started_at": _now(), "transcript": tr,
           # 原件基线 + 本机发过的值 + 被剔/分不清的行：判据见 mf.original_lines
           **pin,
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
    if isinstance(rec.get("qc"), dict):
        rec["qc"]["usage"] = usage.take()   # 主单这条链更贵（默认公司百炼 + 可开思考），账要单独看得见
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
    force=True 越过缓存：换了模型或想把上一次的失败重试一次。

    占位那次 `_write` 是把上一份整个盖掉：原件基线、本机发过的值、那份脏标记必须带过去，
    不然"解析到一半"就把唯一的可比对象洗没了（v1.27.0 的标注在这条真链路上从来不出，就是这个洞）。"""
    prev = read_master(mawb) or {}
    if prev.get("state") == "parsing":
        prev = {}
    tr, pin = build_input(mawb, mawb_order, prev)
    carry = {k: pin[k] for k in ("baseline", "baseline_dirty", "sent_back")}
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
                           "transcript": tr, "ams": {}, "ams_raw": {}, **carry,
                           "error": "主单资料文本为空，无从解析：公司接口没返回这条主单的资料",
                           "qc": {"flags": ["主单资料为空"], "needs_review": True, "fidelity": None},
                           "started_at": _now(), "updated_at": _now(), "elapsed": 0.0})
        _write({"mawb": str(mawb or "").strip(), "text_md5": md5, "face_md5": face, "model": model,
                "state": "parsing", "transcript": tr, "ams": {}, "ams_raw": {}, **carry,
                "error": "", "qc": None, "started_at": _now(), "updated_at": _now()})
    return run_master(mawb, mawb_order, prev=prev)


def submit_master(payload: dict) -> dict:
    """把人工核对过的主单**提交发送**给公司（mawb3，落库状态 1）。门与分单同构：署名 + 红旗清零或逐条确认留痕，
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
    reviewed = store.reviewed_by_inputter(cached)
    # 与分单同一条口径（2026-10-14 用户把那次确认删了，留痕不动）：没复核过照样发，
    # 台账记 no_inputter_review。提交人自己就是录入员时记 inputter_self_submit、
    # 不记「未经录入员复核」——那一档是"制单员跳过复核直发"的对照组，
    # 混进录入员自己的提交，对照就废了（2026-10-12 现场：录入员被这样问）。
    self_submit = (not reviewed) and str(p.get("reviewer_role") or "") == "inputter"
    # 只发调用方给过的列（clean_ams 会丢掉没给的）：公司写口是整表写回，
    # "这次没碰"要由 company_api 用库里的现值补齐，"人工清空"才是显式 null。
    res = company_api.submit_master({"mawb": mawb, "ams": mf.clean_ams(ams)})
    store.mark_master_submitted(mawb, reviewer, receipt=res, acked_flags=acked,
                                snapshot=res.get("before"), ams_sent=ams,
                                stager=cached.get("stager") or "",
                                staged_at=cached.get("staged_at") or "",
                                no_inputter_review=not reviewed and not self_submit,
                                inputter_self_submit=self_submit)
    if cached:
        # 提交时人确认着的红旗也留在解析记录顶上：重开这张主单时前端拿它还原，不然确认过的
        # 旗第二天又红一遍。只在本来就有记录时写——提交不该顺手造出一份解析缓存来。
        _write(dict(cached, acked_flags=acked))
    mark_master_sent(mawb, reviewer)
    return res
