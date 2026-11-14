# -*- coding: utf-8 -*-
"""第二只眼（2026-11-14 定案）：主链解析成功后，后台线程用**同一个模型强制开思考**把票面
重读一遍，逐格比对主链结果。它不改任何值、不拦暂存与提交，只往 qc 里留痕并挂旗：
- 号码类冲突（主分单号/HS/税号/EORI/航路/日期/起讫）→ qc.flags 挂「互核 …」旗，穿过现有
  红旗管道显示；票一旦离开暂存档（提交公司），前端不再挂现值旗（已交出去的不许再催）。
- 件数类（件数/毛重/SLAC）两边都可能错，旗面写明"以票面为准"。
- 整串类差异只进 second_eye 记录留痕，不顶人（实测互核冲突率 11%，一半是格式噪音）。
成本 ~0.03 元/票（思考 token 大头，走公司实例）；SECOND_EYE=0 全局关闭。
"""
import datetime
import json
import threading

import config
import fieldspec
import store
import to_air

# 号码类：这些列的冲突直接顶到人眼前——这道岗的初心就是兜漏抄（列名守卫见 test_second_eye）
SURFACE_COLS = ("MAWB_NO", "HAWB_NO", "GOODS_HS_CODE",
                "SHIPPER_INFO_EORI", "CONSIGNEE_INFO_EORI",
                "SHIPPER_INFO_TAX", "CONSIGNEE_INFO_TAX",
                "TO1", "TO2", "TO3", "CREATE_TIME", "ORIGIN_NAME", "DEST_NAME")
# 件数类：两边都可能错（实测开思考也把 3 件数成 6），旗面明示以票面为准
COUNT_COLS = ("PIECES", "WEIGHT", "SLAC")
_COLS = SURFACE_COLS + COUNT_COLS


def enabled() -> bool:
    return config.SECOND_EYE != "0"


def extra_body_for_preset(preset: dict) -> dict:
    """第二只眼的思考档写死"开"（mode="1"），不吃全局开关；渠道没声明思考参数就不加参数，
    第二只眼降级照跑（与 config.extra_body_for 的口径一致）。"""
    return config.extra_body_for(preset, "1")


def extra_body_for_preset_from_current(mode: str) -> dict:
    """当前生效渠道 + 指定思考档：走 config 的公共包装，不伸私有的手。"""
    return config.extra_body_for_current(mode)


def _v(x) -> str:
    return str(x if x is not None else "").strip()


def conflicts(main_air: dict, second_air: dict) -> list:
    out = []
    for c in _COLS:
        a, b = _v((main_air or {}).get(c)), _v((second_air or {}).get(c))
        if a == b:
            continue
        out.append({"col": c, "main": a, "second": b,
                    "kind": "count" if c in COUNT_COLS else "num"})
    return out


def flag_text(c: dict) -> str:
    def part(label, v):
        return f"{label} '{v}'" if v else f"{label}为空"

    head = f"互核 {c['col']}：{part('主链', c['main'])}，{part('第二只眼', c['second'])}"
    if c["kind"] == "count":
        head += "（两边都可能错，以票面为准）"
    return head


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def record(main_air: dict, second_air: dict, model: str, thinking: bool) -> dict:
    """留痕记录：全量差异都记（包括不顶人的整串类），surfaced 才挂旗。"""
    all_diffs = sorted(c for c in fieldspec.desk_keys()
                       if _v((main_air or {}).get(c)) != _v((second_air or {}).get(c)))
    return {"at": _now(), "model": model, "thinking": bool(thinking), "status": "ok",
            "surfaced": conflicts(main_air, second_air), "all_diffs": all_diffs}


def _is_blank(air: dict) -> bool:
    """整票空：L2 原文口径（to_air **之前**）所有列都空——不能查 to_air 之后的面，
    那一层会给发货人城市/邮编/国别填固定默认值（v1.34.0 发出口径），空票会被它填成假有货。"""
    return all(_v((air or {}).get(c)) == "" for c in fieldspec.desk_keys()
               if c != "SEND_STATUS")


def _extract(src, transcript, urls):
    """主链同款提取（同 prompt 同模型同转录），只多带一份强制开思考的 extra_body。"""
    import vlm_extract
    return vlm_extract.extract_one(src, transcript=transcript, urls=urls,
                                   extra_body=extra_body_for_preset_from_current("1"))


def run_one(stem, air, transcript, urls, src) -> dict:
    """跑一遍第二只眼并落 qc。任何失败都只记录、不抛——后台线程不许把解析带崩；
    整票空返回重试一次（DeepSeek 通道 1/10 实测过的坑），再空就只留痕不挂旗。"""
    model = config.VLM_MODEL
    thinking = bool(config.thinking()["supported"])
    try:
        raw = _extract(src, transcript, urls)
        if _is_blank(raw):
            raw = _extract(src, transcript, urls)
            if _is_blank(raw):
                rec = {"at": _now(), "model": model, "thinking": thinking,
                       "status": "empty", "surfaced": [], "all_diffs": []}
                append_record(stem, rec)
                return rec
        rec = record(air, to_air.to_air(raw), model=model, thinking=thinking)
        append_record(stem, rec)
        return rec
    except Exception as e:
        rec = {"at": _now(), "model": model, "thinking": thinking, "status": "error",
               "err": f"{type(e).__name__}: {str(e)[:160]}", "surfaced": [], "all_diffs": []}
        append_record(stem, rec)
        return rec


def append_record(stem: str, rec: dict) -> None:
    """把第二只眼记录并进这张票已落盘的质检记录；没有 qc（旧票/失败票）就不硬造一份。
    surfaced 冲突以「互核 …」旗并进 flags（去重）——旗在 qc 里，审核台打开票就能看到，
    不必等提交门；提交门的 recompute_flags 不产出互核旗，所以它们永远不拦暂存与提交。"""
    path = config.OUTPUT_QC_DIR / f"{stem}.json"
    try:
        qc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    qc["second_eye"] = rec
    old = list(qc.get("flags") or [])
    fresh = [flag_text(c) for c in (rec.get("surfaced") or [])]
    merged = old + [f for f in fresh if f not in old]
    if merged != old:
        qc["flags"] = merged
        qc["needs_review"] = bool(merged)
    store.save_qc(stem, qc)


def maybe_start(stem, air, transcript, urls, src) -> bool:
    """起了互核线程就返回 True——解析响应带着它，前端才敢亮「互核中…」。"""
    if not enabled():
        return False
    threading.Thread(target=run_one, args=(stem, air, transcript, urls, src),
                     daemon=True).start()
    return True
