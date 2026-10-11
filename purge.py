# -*- coding: utf-8 -*-
"""彻底删除（数据清理）：候选怎么算、一张票要动哪些路径、执行的顺序。

与「作废」的分工是这整个文件的前提：**作废是软闸门**（只把行从台账与正确率里收起来，
什么都不删，随时能翻回来，登录就能点）；**这里做的是不可逆的那一步**——人已经判过"不作数"、
而且在台账里又躺够了保留期的票，才允许连票面原件与全部解析产物一起抹掉。
所以这个模块唯一的准入条件是「已作废且满 N 天」。没作废过的票，无论界面怎么搜、
请求怎么构造，都不该出现在能删的名单里（服务端重算候选，不信任前端勾了什么）。

三件容易做错、所以每条都有用例钉住的事：
  1. 顺序：先写删除凭据，再删文件，最后才动台账条目。反过来做一次半途崩掉，
     就留下一张"文件还在盘上、台账里已经查无此票"的孤儿数据——那种数据没人能找到，
     也就永远删不掉。
  2. 副本：删掉 output/raw/S1.json 不等于删干净，all_hawbs.json 里还整份留着那张票的值；
     server 那个按 stem 缓存的页图 PNG 同理。两处都要跟着清。
  3. 共享：一张主单的原件与缓存，归它名下所有分单共用。清一张分单绝不能顺手把主单删了。
"""
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import company_api
import config
import store

# server 启动时往这里塞一个"按 stem 清渲染缓存"的回调：purge 不 import server（循环依赖），
# 但删完票还让页图缓存留着，等于从缓存里就能把已删的票面翻出来。
CACHE_HOOKS = []


def retention() -> int:
    """当前生效的保留期（天）。坏值不采信，落回默认 30——0 天等于没有保留期。"""
    lo, hi = config.PURGE_DAYS_RANGE
    v = config.PURGE_AFTER_DAYS
    return v if isinstance(v, int) and lo <= v <= hi else 30


def set_retention(value):
    """改保留期并落 .env（重启后还在）。成功返回改完的值，**没收下的一律返回 None**
    ——非数字与越界都给同一个信号，路由才好统一回 400。
    静默保留原值看着安全，实际最坏：管理员填了个 0、界面没报错、他以为改成了，
    而盘上还是 15 天。"这个值我没收"必须是听得见的。"""
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    lo, hi = config.PURGE_DAYS_RANGE
    if not lo <= days <= hi:
        return None
    config.PURGE_AFTER_DAYS = days
    config.persist_env("PURGE_AFTER_DAYS", str(days))
    return days


def _at(v):
    try:
        return datetime.fromisoformat(str(v or ""))
    except (TypeError, ValueError):
        return None


def _aged(at, days, now):
    """作废戳够老了吗。时刻读不出来的（有人手工改过 voided.json）一律当"不够老"——
    认不出就别删，这条判据错了只可能是"少删"，不会是多删。"""
    t = _at(at)
    return bool(t) and now - t >= timedelta(days=max(0, int(days)))


def _size(p: Path) -> int:
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size if p.exists() else 0
    except OSError:
        return 0


def _hawb_plan(stem, v, days, now, ledger):
    """一张分单：把它的落点列出来。任何一步不合法就返回 None（不产生任何删除动作）。"""
    if stem not in ledger:
        entry = {}
    else:
        entry = ledger.get(stem) or {}
    files, dirs = [], []
    qc = {}
    qp = config.OUTPUT_QC_DIR / (stem + ".json")
    if qp.is_file():
        try:
            qc = json.loads(qp.read_text(encoding="utf-8"))
        except Exception:
            qc = {}
    air = {}
    ap = config.OUTPUT_AIR_DIR / (stem + ".json")
    if ap.is_file():
        try:
            air = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:
            air = {}
    # input 那一格：source_name 是当年用户传上来的文件名，进过台账也进过 qc，
    # 一样要过单段校验才敢拼路径——不能因为"是我们自己写进去的"就省了。
    src = store.safe_stem(str(qc.get("source_name") or ""))
    if src:
        p = config.INPUT_DIR / src
        if p.is_file():
            files.append(p)
    for p in (config.TRANSCRIPT_DIR / (stem + ".json"), config.OUTPUT_RAW_DIR / (stem + ".json"),
              config.OUTPUT_AIR_DIR / (stem + ".json"), config.OUTPUT_QC_DIR / (stem + ".json"),
              config.PREVIEW_DIR / (stem + ".pdf"), config.STAGED_DIR / (stem + ".json")):
        if p.exists():
            files.append(p)
    arch = config.ARCHIVE_DIR / stem
    if arch.is_dir():
        dirs.append(arch)
    # 号：台账优先（那是"回传过公司"的那一份），没有台账（只暂存过、或台账条目已被清掉）
    # 就从 air 那份取——候选行与删除凭据都靠这两格认票，两格都空着等于让人盲选一张不可逆的删除。
    mawb = str(entry.get("mawb") or air.get("MAWB_NO") or "")
    hawb = str(entry.get("hawb") or air.get("HAWB_NO") or "")
    key = str(entry.get("key") or store.number_key(mawb, hawb))
    lock = company_api.guard_file(key) if key and key != "|" else None
    if lock and lock.is_file():
        files.append(lock)
    return {"kind": "hawb", "stem": stem, "key": "hawb|" + stem,
            "mawb": mawb, "hawb": hawb,
            "void_by": str(v.get("by") or ""), "void_at": str(v.get("at") or ""),
            "in_ledger": stem in ledger, "days": days,
            "submitted_at": str(entry.get("submitted_at") or ""),
            "receipt_action": str((entry.get("receipt") or {}).get("action") or ""),
            "files": files, "dirs": dirs,
            "drop": {"ledger": stem, "void": "hawb|" + stem},
            "rebuild": True}


def _mawb_plan(mkey, v, days, now):
    if store.submitted_by_mawb(mkey):
        return None          # 名下还有已提交的分单：先把它们清掉，再谈删主单
    files, dirs = [], []
    cache = config.MASTER_DIR / (mkey + ".json")
    if cache.is_file():
        files.append(cache)
    src = config.MAWB_SOURCE_DIR / mkey
    if src.is_dir():
        dirs.append(src)
    return {"kind": "mawb", "stem": "", "key": "mawb|" + mkey,
            "mawb": mkey, "hawb": "", "void_by": str(v.get("by") or ""),
            "void_at": str(v.get("at") or ""), "in_ledger": mkey in store.master_ledger(),
            "days": days, "submitted_at": "", "receipt_action": "",
            "files": files, "dirs": dirs,
            "drop": {"master": mkey, "void": "mawb|" + mkey}, "rebuild": False}


def plan(kind, key, days, now=None):
    """这张票现在能不能删、删的话要动哪些路径。不能删一律返回 None。

    kind 只认 hawb/mawb；days 由调用方给——**预览可以传 0（"现在就给我看"），
    真正执行的那一路只读服务端存的保留期**，否则请求里带个 days=0 就绕过保留了。"""
    k = str(kind or "").strip().lower()
    raw = str(key or "").strip()
    if k not in ("hawb", "mawb") or not raw:
        return None
    voided = store.voided_map()
    if k == "hawb":
        stem = store.safe_stem(raw)
        if not stem:
            return None
        v = voided.get("hawb|" + stem)
        if not v or not _aged(v.get("at"), days, _now(now)):
            return None
        p = _hawb_plan(stem, v, days, _now(now), store.ledger())
    else:
        mkey = store.norm_no(raw)
        if not store.mawb_source_dir(mkey):       # 归一化 + 字符集校验都在里面
            return None
        v = voided.get("mawb|" + mkey)
        if not v or not _aged(v.get("at"), days, _now(now)):
            return None
        p = _mawb_plan(mkey, v, days, _now(now))
        if p is None:
            return None
    p["bytes"] = sum(_size(x) for x in p["files"] + p["dirs"])
    return p


def _now(now):
    return _at(now) or datetime.now()


def candidates(days=None, now=None, with_bytes=True):
    """面板与顶栏徽标读的同一份候选。徽标那条路径传 with_bytes=False：
    stat 归档目录比读两份本地 JSON 贵得多，而它只需要一个条数。"""
    d = retention() if days is None else max(0, int(days))
    out = []
    for kk, v in store.voided_map().items():
        kind, _, key = kk.partition("|")
        if not key:
            continue
        if kind == "hawb":
            stem = store.safe_stem(key)
            if not stem or not _aged(v.get("at"), d, _now(now)):
                continue
            p = _hawb_plan(stem, v, d, _now(now), store.ledger())
        elif kind == "mawb":
            if not store.mawb_source_dir(key) or not _aged(v.get("at"), d, _now(now)):
                continue
            p = _mawb_plan(key, v, d, _now(now))
        else:
            continue
        if not p:
            continue
        p["bytes"] = sum(_size(x) for x in p["files"] + p["dirs"]) if with_bytes else 0
        out.append(p)
    return sorted(out, key=lambda x: (x["void_at"], x["key"]))


def _tell_caches(stem: str):
    for hook in CACHE_HOOKS:
        try:
            hook(stem)
        except Exception:
            pass          # 缓存清不掉不该让整次删除失败：文件是真删了，缓存只是内存里的一份副本


def _row(p):
    """凭据那一行的内容。刻意只放号与文件名——想知道删了哪几个文件，不想知道票上写了什么。"""
    return {"at": datetime.now().isoformat(timespec="seconds"), "kind": p["kind"], "key": p["key"],
            "stem": p["stem"], "mawb": p["mawb"], "hawb": p["hawb"], "days": p["days"],
            "void_by": p["void_by"], "void_at": p["void_at"], "submitted_at": p["submitted_at"],
            "receipt_action": p["receipt_action"], "in_ledger": p["in_ledger"], "auto": False,
            "files": [x.name for x in p["files"]] + [x.name + "/" for x in p["dirs"]]}


def _append_tombstone(row, by):
    rec = dict(row, by=str(by or ""))
    config.PURGED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    # 0600 要在"打开新建"那一次就给：这本里有 void_by 与 by（人名）。先按默认 umask 落成 644
    # 再补一次 chmod，中间那个窗口里 output/ 在这台共用机上别的进程就读得到了。
    # 文件已存在时这个 mode 被内核忽略——不会把运维手工调过的模式改回去。
    fd = os.open(str(config.PURGED_LEDGER), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def remove(p, by, now=None):
    """执行一张。顺序见模块 docstring 第 1 条——凭据先落，台账最后动。"""
    removed, errors = [], []
    _append_tombstone(_row(p), by)                     # 1) 先留痕
    for f in list(p["files"]) + list(p["dirs"]):      # 2) 再删文件
        try:
            if f.is_dir():
                shutil.rmtree(f)
            elif f.exists():
                f.unlink()
            removed.append(f.name)
        except OSError as e:
            errors.append("%s: %s" % (f.name, e))
    if errors:                                         # 没删干净就别动台账：下次还能被挑出来接着删
        return {"ok": False, "key": p["key"], "removed": removed,
                "error": "有文件删不掉（盘或权限的问题），台账与作废标记都没动，可以再删一次：" + errors[0]}
    d = p["drop"]
    if d.get("ledger"):
        store.drop_submitted(d["ledger"])             # 3) 台账条目最后动
    if d.get("master"):
        store.drop_master_submitted(d["master"])
    if d.get("void"):
        kind, _, key = str(d["void"]).partition("|")
        store.unvoid_row(kind, key)
    try:
        from db_writer import delete_ticket            # 4) 配了 DB_URL 才有的那一份
        delete_ticket(p["stem"] or p["mawb"])
    except Exception as e:
        errors.append("数据库行没删掉：%s" % e)
    if p["rebuild"]:                                   # 5) 聚合副本
        store.rebuild_summary()
    _tell_caches(p["stem"] or p["mawb"])
    if errors:
        return {"ok": True, "key": p["key"], "removed": removed, "error": errors[0], "warn": True}
    return {"ok": True, "key": p["key"], "removed": removed, "error": ""}


def purge(items, by, days=None, now=None):
    """逐张执行。days 不传就用服务端那一份；**调用方是路由时不要从请求里取这个参数**。
    不在候选里的项（没作废 / 没满保留期 / 票已经不在盘上）逐张报原因，不影响同批其余的。"""
    d = retention() if days is None else max(0, int(days))
    out = []
    for it in items or []:
        kind = str((it or {}).get("kind") or "").strip().lower()
        key = str((it or {}).get("key") or "").split("|", 1)[-1].strip()
        p = plan(kind, key, d, now)
        if not p:
            v = store.voided_map().get(kind + "|" + store.norm_no(key) if kind == "mawb"
                                        else kind + "|" + key)
            if not v:
                msg = "这张票没有作废标记，彻底删除只针对已作废的票——要先在台账里点作废"
            elif not _aged(v.get("at"), d, _now(now)):
                msg = "作废还没满保留期（当前 %s 天），到期前这张随时能反悔" % d
            else:
                msg = "这张票在候选里算不出来（单号不合法，或主单名下还有已提交的分单）"
            out.append({"ok": False, "key": (it or {}).get("key", ""), "removed": [], "error": msg})
            continue
        out.append(remove(p, by, now))
    return out
