# -*- coding: utf-8 -*-
"""解析正确率（需求三）：模型那一版与人最后定下的那一版，逐列比。

为什么单独一个模块：这套数要拿去回答"能不能不经过人、直接把提取结果喂给平台"。
一个没有分母的百分比、或把"没留痕的历史票"当成"零改动"，都会把决定推向错误的方向，
所以分母、归属和"哪些不算"三件事写在这里，并由 tests/test_stats.py 钉住。

算术只在这一处（Python）。前端只渲染，不重算：同一套算术写两遍，迟早给出两个百分比
——今日台账那页就是为这件事专门留了一条用例。导出也走这里，保证页面与 Excel 同源。
"""
import json

import config
import store
from fieldspec import desk_keys

# 模型硬写的列（`vlm_extract` 里恒为 "PENDING"）：算进去等于凭空多一列"读对了"。
SKIP = {"SEND_STATUS"}
GROUPS = ("model", "role", "user", "field")
# 分组轴 → 行里的字段名。role/user 用的是上传人（谁把这张票传上来的），不是提交人：
# 判断"哪个人手上的票模型读得差"要的是同一个分母里的人。
_GROUP_FIELD = {"model": "model", "role": "uploader_role", "user": "uploader"}


def _keys() -> list:
    return [k for k in desk_keys() if k not in SKIP]


def _nonempty(v) -> bool:
    return str(v if v is not None else "").strip() != ""


def _read_json(p):
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _model_air(stem: str, staged: dict) -> dict:
    """基线 = 模型那一版。暂存时快照的那份优先：重解析会覆盖 output/air，
    现读的话"模型当初读了什么"就跟着第二次解析跑了。"""
    a = staged.get("air_model")
    if isinstance(a, dict) and a:
        return a
    return _read_json(config.OUTPUT_AIR_DIR / f"{stem}.json")


def _when(stem: str, qc: dict, staged: dict, entry: dict) -> str:
    """时间窗按"这张票什么时候被模型读过"筛——这是正确率的自变量。
    没有 qc 的老记录退回提交/暂存时刻，再没有就空（带窗口时自然筛掉）。"""
    return (str(qc.get("processed_at") or "") or str(entry.get("submitted_at") or "")
            or str(staged.get("staged_at") or ""))


def _numbers(entry: dict, staged: dict) -> tuple:
    """这张票"是哪一张"用主单号 + 分单号报（2026-10-09 用户定案）：票名是文件名，
    拿着它去公司系统对还得先查一遍是谁。

    取法只有一条：**提交过的以台账为准**（那是要跟公司系统对上的号），**没提交过的看暂存记录**
    （两处存的都已经是人定下的那一版，不是模型那一版）。都没有就留空——不拿票名硬凑，
    缺号本身是要看得见的事实。"""
    en = entry if isinstance(entry, dict) else {}
    st = staged if isinstance(staged, dict) else {}
    return (str(en.get("mawb") or st.get("mawb") or "").strip(),
            str(en.get("hawb") or st.get("hawb") or "").strip())


def ticket_row(stem: str, qc: dict = None, staged: dict = None, entry: dict = None) -> dict | None:
    """一张票的对照行。没有人工结果（既没暂存也没提交）就不产行：没有答案就没有正确率可言。

    归属规则（doc=制单员档，inp=录入员档）：
      ① 有暂存记录 → 各角色事件动过的列分别记账；暂存之后到真正发出去之间又改的列算录入员档
         （那一步只有录入员/管理员会做，且是"复核之后还剩的活"，正是对接可行性要的数）；
      ② 没有暂存记录 → 模型到发送值的全部差异记到制单员档。这是事实而不是省事：
         那些值就是坐在核对台前的人改的，只是没有录入员复核过。记成 0 会系统性抬高正确率，
         而抬高方向的误判最贵（会答应下本来不该接的对接）。
    """
    qc = qc if qc is not None else _read_json(config.OUTPUT_QC_DIR / f"{stem}.json")
    staged = staged if staged is not None else (store.load_staged(stem) or {})
    entry = entry if entry is not None else (store.ledger().get(stem) or {})
    if not staged and not entry:
        return None
    model = _model_air(stem, staged)
    submitted = bool(entry)
    final = entry.get("air_sent") if submitted else staged.get("air_final")
    # 台账里 air_sent 是空壳（本功能之前的条目只有字段名与哈希）＝没有值可比：算 legacy。
    legacy = submitted and not (isinstance(final, dict) and final)
    if legacy:
        # 本功能之前的台账只存过字段名与哈希，没有值：算不了就别混进分母。
        lm, lh = _numbers(entry, staged)
        return {"stem": stem, "mawb": lm, "hawb": lh, "state": "submitted", "legacy": True, "total": None,
                "doc": [], "inp": [], "comparable": 0, "fields": [],
                "model": qc.get("model") or staged.get("model") or "",
                "uploader": qc.get("uploader") or "", "uploader_role": qc.get("uploader_role") or "",
                "stager": "", "no_inputter_review": True, "when": _when(stem, qc, staged, entry),
                "source_name": qc.get("source_name") or stem}
    final = final if isinstance(final, dict) else (staged.get("air_final") or {})
    keys = [k for k in _keys() if _nonempty(model.get(k)) or _nonempty(final.get(k))]
    diff_all = set(store.field_diff({k: model.get(k) for k in keys},
                                    {k: final.get(k) for k in keys}))
    doc, inp = set(), set()
    if staged:
        for ev in staged.get("events") or []:
            touched = set((ev.get("edits") or {}).keys())
            if str(ev.get("role") or "") == "inputter":
                inp |= touched
            else:
                doc |= touched
        inp |= set(store.field_diff(staged.get("air_final") or {}, final))
    else:
        doc = set(diff_all)
    nm, nh = _numbers(entry, staged)
    return {"stem": stem, "mawb": nm, "hawb": nh, "state": "submitted" if submitted else "staged",
            "legacy": False,
            "total": len(diff_all), "doc": sorted(doc), "inp": sorted(inp),
            "comparable": len(keys), "fields": keys,
            "model": qc.get("model") or staged.get("model") or "",
            "model_choice": qc.get("model_choice") or staged.get("model_choice") or "",
            "uploader": qc.get("uploader") or staged.get("uploader") or "",
            "uploader_role": qc.get("uploader_role") or staged.get("uploader_role") or "",
            "stager": staged.get("stager") or entry.get("stager") or "",
            "no_inputter_review": bool(entry.get("no_inputter_review", True)),
            "when": _when(stem, qc, staged, entry),
            "source_name": qc.get("source_name") or stem}


def _stems() -> list:
    out = set()
    for d in (config.OUTPUT_QC_DIR, config.STAGED_DIR):
        if d.exists():
            out |= {p.stem for p in d.glob("*.json") if not p.name.startswith("all_")}
    out |= set(store.ledger().keys())
    return sorted(out)


def collect(date_from: str = "", date_to: str = "") -> list:
    """时间窗内每张有人工结果的票一行（from/to 含端点，按 yyyy-mm-dd 比）。"""
    led = store.ledger()
    rows = []
    for stem in _stems():
        row = ticket_row(stem, entry=led.get(stem) or {})
        if not row:
            continue
        day = str(row.get("when") or "")[:10]
        if date_from and (not day or day < date_from):
            continue
        if date_to and (not day or day > date_to):
            continue
        rows.append(row)
    return rows


def _rate(numerator: float, denominator: float):
    """百分比：没有分母就是 None，不许编一个 100% 出来。"""
    return None if not denominator else round(1 - numerator / denominator, 4)


# 表头给人看的中文列名（导出与页面用同一份，别让两边各翻一遍）
_CN = {"stem": "票名", "state": "状态", "when": "解析时刻", "model": "模型",
       "model_choice": "渠道预设", "uploader": "上传人", "uploader_role": "上传人角色",
       "stager": "暂存人", "comparable": "可比列数", "total": "改动列数",
       "no_inputter_review": "未经录入员复核", "legacy": "无逐字段留痕(历史)",
       "field": "列", "edits": "被改次数", "tickets": "票数", "fields": "可比列数",
       "clean": "零改动票数", "clean_rate": "一次通过率", "acc_field": "逐字段准确率",
       "acc_after_review": "复核后准确率（能不能对接看这个）", "edits_total": "改动列合计",
       "edits_doc": "制单员改的列", "edits_inp": "录入员改的列",
       "fields_comparable": "可比列合计", "unreviewed": "未经复核就发的张数"}


def _cn(d: dict) -> dict:
    return {_CN.get(k, k): v for k, v in d.items()}


def summary(rows: list, group_by: str = None) -> dict:
    """汇总：分母只用"可比列数"（两边都空的列不进），历史没留痕的票不进分母、单独报张数。"""
    usable = [r for r in rows if not r.get("legacy")]
    fields = sum(int(r.get("comparable") or 0) for r in usable)
    total = sum(int(r.get("total") or 0) for r in usable)
    doc = sum(len(r.get("doc") or []) for r in usable)
    inp = sum(len(r.get("inp") or []) for r in usable)
    by_field: dict = {}
    for r in usable:
        for f in (r.get("doc") or []) + (r.get("inp") or []):
            e = by_field.setdefault(f, {"field": f, "edits": 0, "tickets": 0})
            e["edits"] += 1
            e["tickets"] += 1
    out = {"tickets": len(usable), "legacy": len(rows) - len(usable),
           "fields_comparable": fields, "edits_total": total, "edits_doc": doc, "edits_inp": inp,
           "clean": sum(1 for r in usable if not r.get("total")),
           "clean_rate": (None if not usable
                          else round(sum(1 for r in usable if not r.get("total")) / len(usable), 4)),
           "acc_field": _rate(total, fields),
           "acc_after_review": _rate(inp, fields),
           # 只数真的发出去那批：只暂存没发的票同样还没有 inputter 事件，把它算进来，
           # 这个数会随谁把工作停在暂存一档而涨——而它是拿去看要不要对接的数。
           "unreviewed": sum(1 for r in usable
                             if r.get("no_inputter_review") and r.get("state") == "submitted"),
           "by_field": sorted(by_field.values(), key=lambda e: (-e["edits"], e["field"]))}
    if group_by in GROUPS:
        buckets: dict = {}
        for r in usable:
            if group_by == "field":
                for e in out["by_field"]:
                    g = buckets.setdefault(e["field"], {group_by: e["field"], "edits": 0,
                                                             "tickets": 0, "fields": 0})
                    g["edits"] += e["edits"]
                    g["tickets"] += 1
                continue
            key = r.get(_GROUP_FIELD[group_by]) or "（未记录）"
            g = buckets.setdefault(key, {group_by: key, "tickets": 0, "fields": 0, "edits": 0})
            g["tickets"] += 1
            g["fields"] += int(r.get("comparable") or 0)
            g["edits"] += int(r.get("total") or 0)
        out["groups"] = sorted(buckets.values(), key=lambda g: (-g["tickets"], str(g[group_by])))
    return out


def export_bytes(rows: list, summary: dict) -> bytes:
    """导成三张表的 Excel：口径 / 每张票 / 逐列。

    页面与导出必须同源，所以这里不重算任何数——只把 `summary` 与 `rows` 摊平成表格。
    在内存里生成：落到磁盘上就要管临时文件的生命周期，而这份文件下载完就没用了。
    """
    import io

    import pandas as pd

    head = pd.DataFrame([{"指标": _CN.get(k, k), "值": v}
                         for k, v in summary.items() if not isinstance(v, list)])
    per = []
    for r in rows:
        one = {k: r.get(k) for k in ("stem", "state", "when", "model", "uploader",
                                     "uploader_role", "stager", "comparable", "total",
                                     "no_inputter_review", "legacy")}
        one["制单员改的列"] = "、".join(r.get("doc") or [])
        one["录入员改的列"] = "、".join(r.get("inp") or [])
        per.append(_cn(one))
    fields = pd.DataFrame([_cn(e) for e in summary.get("by_field") or []])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as w:
        head.to_excel(w, sheet_name="口径", index=False)
        (pd.DataFrame(per) if per else pd.DataFrame([{_CN["stem"]: "（窗口内没有可算的票）"}])) \
            .to_excel(w, sheet_name="每张票", index=False)
        (fields if not fields.empty else pd.DataFrame([{_CN["field"]: "（窗口内没有改动）"}])) \
            .to_excel(w, sheet_name="逐列", index=False)
    return buf.getvalue()
