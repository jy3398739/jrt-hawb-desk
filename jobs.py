# -*- coding: utf-8 -*-
"""单个分单文件的完整任务：L0 归档 → L1 转录 → L2 原文 → L3 航空 → 保真校验 → 落盘。
批处理/监控/HTTP 共用。"""
import datetime, time
from pathlib import Path

import store
from pipeline import process_file
from validator import validate_raw, validate_air
from fidelity import verify_fidelity


def build_qc(path: Path, r: dict, meta: dict | None = None) -> dict:
    """把散在 validator/fidelity 里的红旗收成一条可交付的质检记录。
    红旗只打在 JSON 里没人看，必须跟着 Excel/数据库走，否则等于没检。

    meta 带上传人（来自登录会话）；批处理/监控没有会话，那两样记空但不能因此写不出记录。
    model/model_choice 是**解析那一刻**的模型（由 pipeline 带回来），不是建记录时现读 config：
    审核台能在解析中途热切模型，现读会把这张票记到别的模型头上。"""
    flags = list(r["warns"]) + [f"保真 {w}" for w in (r["fidelity"] or {}).get("warns", [])]
    if r["error"]:
        flags.insert(0, f"处理失败 {r['error']}")
    fid = r["fidelity"]
    meta = meta or {}
    return {"source_name": path.name, "md5": (r["archive"] or {}).get("md5", ""),
            "channel": r["channel"], "elapsed": r["elapsed"],
            "uploader": str(meta.get("uploader") or ""),
            "uploader_role": str(meta.get("uploader_role") or ""),
            "model": r.get("model") or "", "model_choice": r.get("model_choice") or "",
            "processed_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "needs_review": bool(flags), "flags": flags,
            "fidelity": ({"checked": fid["checked"], "passed": fid["passed"],
                          "failed": fid["failed"], "sources": fid["sources"]} if fid else None),
            "error": r["error"]}


def recompute_flags(raw: dict | None, air: dict, transcript: dict | None = None) -> list:
    """**提交时刻**按当前值重算红旗，与 handle_file 用同一批判据、同一套文案
    （字符串要对得上人工确认的那一条）。

    拿不到 L2 原文或 L1 转录时只算拿得到的那层，绝不因为"没文件"就凭空报"MAWB_NO 缺失"——
    在别的机器解析过的票也要提得出去。"""
    warns = list(validate_raw(raw)) if raw else []
    warns += ["L3 " + w for w in validate_air(air or {})]
    if raw and transcript:
        fid = verify_fidelity(raw, transcript)
        warns += [f"保真 {x['field']}: {x['reason']}（值={x['value']!r}）" for x in fid["failed"]]
    return warns


def handle_file(path, save: bool = True, meta: dict | None = None) -> dict:
    """返回 {stem, channel, elapsed, raw, air, transcript, warns, fidelity, archive, qc, error}。
    meta={"uploader","uploader_role"} 是给质检记录留痕用的（谁传的这张票）。"""
    path = Path(path)
    t0 = time.time()
    r = {"stem": path.stem, "channel": "", "elapsed": 0.0, "raw": None, "air": None,
         "transcript": None, "warns": [], "fidelity": None, "archive": None,
         "model": "", "model_choice": "", "qc": None, "error": ""}
    try:
        if save:
            r["archive"] = store.archive_original(path)          # L0
        res = process_file(path)                                 # L1+L2+L3
        r["raw"], r["air"], r["channel"], r["transcript"] = (
            res["raw"], res["air"], res["channel"], res["transcript"])
        r["model"], r["model_choice"] = res.get("model") or "", res.get("model_choice") or ""
        src = res.get("src")
        if save and src and Path(src) != path:
            store.save_preview(path.stem, src)      # 电子单转出的 PDF：审核台回看票面靠它
        r["warns"] = validate_raw(r["raw"]) + ["L3 " + w for w in validate_air(r["air"])]
        if r["transcript"] is not None:
            fid = verify_fidelity(r["raw"], r["transcript"])  # L2⊆L1，两通道同口径
            r["fidelity"] = fid
            r["fidelity"]["warns"] = [f"{x['field']}: {x['reason']}（值={x['value']!r}）"
                                      for x in fid["failed"]]
    except (Exception, SystemExit) as e:
        # SystemExit 是 BaseException，不显式列出来会绕过这里：HTTP 端点只剩光秃秃 500，
        # 批处理在第一张票上直接退出。require_api_key 的"没配令牌"正属于这种可预期失败。
        r["error"] = f"{type(e).__name__}: {e}"
    finally:
        r["elapsed"] = round(time.time() - t0, 1)
        r["qc"] = build_qc(path, r, meta)
        if save:
            if r["error"]:
                store.save_qc(path.stem, r["qc"])     # 失败的票更要留痕，别让它在 output 里隐身
            else:
                if r["transcript"] is not None:
                    store.save_transcript(path.stem, r["transcript"])
                store.save_result(path.stem, r["raw"], r["air"])
                store.save_qc(path.stem, r["qc"])
    return r
