# -*- coding: utf-8 -*-
"""规则留言板：谁在什么时候说了什么、后来怎么定的。只采集，不生效（设计：docs/留言板-design.md）。

三条不变式，动这个文件之前先读：

① **不销毁**——改判只追加一条判定，撤回留成灰行，历次的话连"谁说的、什么时候"一起留着。
   这本台账唯一值钱的就是"当初为什么收/为什么不做"，覆盖一次就永久查不回了。
② **当前判定不写进文件**——它是 `decisions` 最后一条派生出来的。同一件事存两份，迟早对不上，
   而对不上的时候没人知道该信哪份。
③ **本模块不判权限**——"谁说的"由调用方按登录会话传进来（前端传什么都不认），
   这里只守数据形状，以及"已撤回的那条不许再动"这条数据不变式。
"""
import datetime
import json
import re
import threading

import atomic
import config
import fieldspec

TEXT_MAX = 2000         # 正文上限。**这是自己定的数**，不是从列面或别处推出来的——别以为改列面会改到它
NOTE_MAX = 200          # 判定那句"为什么"的上限：短到必须说重点
REF_MAX = 60            # 规则号或版本号（polish.POSTAL / v1.33.0 这种），不是论文
STR_MAX = 160           # 票号与国家标签
STATUS = ("open", "accepted", "rejected", "parked")     # 待看 / 已收 / 不采纳 / 先放一放
_COUNTS = ("all", "open", "accepted", "rejected", "parked", "gone", "mine")
# 控制字符要挡：这些串会进 JSON 也会进前端模板，留着等于给自己埋一个看不见的排版问题
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_LOCK = threading.Lock()


def column_options() -> list:
    """能挂的列名与它的中文名：两条链的列面各回各的家取，这里只拼接、不抄名单。

    下拉里的这一份与 `POST` 挡人的那一份是同一个函数算出来的——分开抄迟早一份比另一份宽，
    而那种偏差表现为"界面能选、提交报 400"，最难查。"""
    import master_fields
    out, seen = [], set()
    for src in (fieldspec.DESK_FIELDS, master_fields.MASTER_FIELDS):
        for key, lab, _group in src:
            if key in seen:
                continue        # MAWB_NO 这类两链都有的列只出现一次
            seen.add(key)
            out.append([key, lab])
    return out


def columns_allowed() -> set:
    """白名单就是上面那份的键名——一处定义，两处用（分单面 + 主单面：通知人那几列只在主单里，
    只按分单列面挡会把「通知人这格该怎么填」这类留言挡在门外）。"""
    return {k for k, _lab in column_options()}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _text(value, limit: int, label: str) -> str:
    s = _CTRL.sub("", str(value if value is not None else "")).strip()
    if len(s) > limit:
        raise ValueError(f"{label}太长了：最多 {limit} 字，现在 {len(s)} 字")
    return s


def _read() -> tuple:
    """(seq, items)。文件坏了或读不出就当空的——服务不能因为一本台账起不来，
    而且这本里没有跑业务要用的值，最多是看不见同事提过什么（那条由面板上的"读不出"提示负责）。"""
    try:
        data = json.loads(config.BOARD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0, []
    if not isinstance(data, dict):
        return 0, []
    seq = data.get("seq")
    items = data.get("items")
    if not isinstance(items, list):
        items = []
    good = [it for it in items if isinstance(it, dict) and isinstance(it.get("id"), int)]
    return (seq if isinstance(seq, int) and seq > 0 else 0, good)


def _write(seq: int, items: list) -> None:
    atomic.write_json(config.BOARD_FILE, {"seq": seq, "items": items}, mode=0o600)


def view(item: dict) -> dict:
    """一条留言的对外形状 = 存的那份 + 派生的当前判定。"""
    out = dict(item)
    dec = [d for d in (item.get("decisions") or []) if isinstance(d, dict)]
    out["decisions"] = dec
    last = dec[-1] if dec else None
    out["status"] = str((last or {}).get("status") or "open")
    out["decided_by"] = str((last or {}).get("by") or "")
    out["decided_at"] = str((last or {}).get("at") or "")
    out["ref"] = str((last or {}).get("ref") or "")
    out["note"] = str((last or {}).get("note") or "")
    out.setdefault("retracted", None)
    return out


def listing(viewer: str = "") -> dict:
    """整本台账 + 计数。**计数在这里算，前端一个都不许再数一遍**（两处判据迟早给出两个数）。

    已撤回的行不进任何一档计数，但仍在这份列表里——它自成一档「已撤回」，看得见、不占待办。
    """
    _, items = _read()
    counts = {k: 0 for k in _COUNTS}
    rows = []
    who = str(viewer or "")
    for it in items:
        row = view(it)
        rows.append(row)
        if row["retracted"]:
            counts["gone"] += 1
            continue
        counts["all"] += 1
        if row["status"] in counts:
            counts[row["status"]] += 1
        else:
            counts["open"] += 1
        if who and row["by"] == who:
            counts["mine"] += 1
    rows.sort(key=lambda r: r["id"], reverse=True)      # 最新的在上面：一屏就是最近发生的事
    return {"items": rows, "counts": counts, "me": who}


def get(item_id: int):
    _, items = _read()
    for it in items:
        if it.get("id") == item_id:
            return view(it)
    return None


def add(text: str, by: str = "", role: str = "", tags: dict = None, ticket: dict = None) -> dict:
    """记一条留言。署名与角色由服务端按会话给，本函数不猜。"""
    body = _text(text, TEXT_MAX, "留言正文")
    if not body:
        raise ValueError("留言是空的：想说什么写下来才有用")
    tg = tags if isinstance(tags, dict) else {}
    out_tags = {}
    country = _text(tg.get("country"), STR_MAX, "国家")
    if country:
        out_tags["country"] = country
    col = _text(tg.get("column"), STR_MAX, "列名")
    if col:
        if col not in columns_allowed():
            raise ValueError(f"列名「{col}」不在两条链的列面里：这一格要写清楚是哪一列")
        out_tags["column"] = col
    tk = ticket if isinstance(ticket, dict) else {}
    out_ticket = {k: _text(tk.get(k), STR_MAX, k) for k in ("stem", "mawb", "hawb")}
    out_ticket = {k: v for k, v in out_ticket.items() if v}
    who = _text(by, STR_MAX, "署名")
    if not who:
        raise ValueError("没有署名：不知道是谁提的，这条规则以后没法回头问")
    item = {"id": None, "by": who, "role": _text(role, 20, "角色"), "at": _now(), "text": body,
            "tags": out_tags, "ticket": out_ticket, "decisions": [], "revised": [],
            "retracted": None}
    with _LOCK:
        seq, items = _read()
        item["id"] = seq + 1
        items.append(item)
        _write(item["id"], items)
    return view(item)


def revise(item_id: int, text: str, by: str = "") -> dict:
    """本人改正文：**旧正文不覆盖**，进 `revised`。改完还是"他说的话"，不是别人代笔。"""
    body = _text(text, TEXT_MAX, "留言正文")
    if not body:
        raise ValueError("留言不能改成空的：那就直接撤回")
    return _patch(item_id, lambda it: _do_revise(it, body, by))


def _do_revise(item: dict, body: str, by: str) -> bool:
    if item["text"] == body:
        return False
    item["revised"] = (item.get("revised") or []) + [{"at": _now(), "by": by, "text": item["text"]}]
    item["text"] = body
    return True


def decide(item_id: int, status: str, note: str, by: str = "", ref: str = "") -> dict:
    """管理员定性。**必须附一句为什么**——这句话是同一件事明年不再被提一遍的唯一凭据。"""
    st = _text(status, 20, "状态")
    if st not in STATUS:
        raise ValueError("状态只能是 待看/已收/不采纳/先放一放 这四档之一")
    why = _text(note, NOTE_MAX, "为什么")
    if not why:
        raise ValueError("要改判定得写一句为什么：没有这句话，以后谁也说不清当初为什么收/不做")
    tag = _text(ref, REF_MAX, "规则号或版本号")
    return _patch(item_id, lambda it: _do_decide(it, st, why, tag, by))


def _do_decide(item: dict, st: str, why: str, tag: str, by: str) -> None:
    item.setdefault("decisions", []).append({"status": st, "note": why, "ref": tag,
                                             "by": by, "at": _now()})


def retract(item_id: int, by: str = "") -> dict:
    """本人撤回：留一条灰行，不删。已经撤回过就原样回，不覆盖成第二次（幂等）。

    走 `_patch` 的 `allow_retracted`：对已撤回的行再点一次撤回**不是**"改一条已撤回的留言"，
    它是重复点了一下同一个按钮。"""
    return _patch(item_id, lambda it: _do_retract(it, by), allow_retracted=True)


def _do_retract(item: dict, by: str) -> None:
    if item.get("retracted"):
        return                                    # 第二次撤回什么都不改，也不报错
    item["retracted"] = {"at": _now(), "by": by}


def remove(item_id: int) -> bool:
    """硬删。只留给误发与含个人信息两种情况，日常"不同意"走 `不采纳`——删了就没人知道说过。"""
    with _LOCK:
        seq, items = _read()
        left = [it for it in items if it.get("id") != item_id]
        if len(left) == len(items):
            return False
        _write(seq, left)             # seq 不回退：删过的号不再给新留言用
    return True


def _patch(item_id: int, fn, allow_retracted: bool = False) -> dict:
    with _LOCK:
        seq, items = _read()
        hit = next((it for it in items if it.get("id") == item_id), None)
        if hit is None:
            raise KeyError(item_id)
        if hit.get("retracted") and not allow_retracted:
            raise ValueError("这条已经撤回了，不能再改：要说不清楚的就再发一条新的")
        fn(hit)
        _write(seq, items)
        return view(hit)
