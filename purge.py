# -*- coding: utf-8 -*-
"""自动清理（彻底删除）：判据怎么算、一张票要动哪些路径、执行的顺序。

判据是「这张票最后一次被碰之后，安静躺满了 N 天」。**不再看有没有人点过作废**——
上一版把准入做成"已作废且满 N 天"，维护成本就又回来了：为「别让我手工清理」设计的功能，
被实现成了"要先由人做一次判断"，制单员不点作废，数据就永远留在服务器上越积越多。
作废现在只是候选行上的一个标记（管理员由此看得出这张是"没人判断过、纯粹躺够了"，
还是"谁收起来过"）。

与「作废」的分工因此是这样：作废仍是软闸门（把行从台账与正确率里收起来，什么都不删，
随时能翻回来，登录就能点）；这里做的是不可逆的那一步，而且它自己会走——
`run_purge.py --auto` 由 systemd timer 每天调一次，不挂在 web 进程里。

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

import atomic
import company_api
import config
import store

# server 启动时往这里塞一个"按 stem 清渲染缓存"的回调：purge 不 import server（循环依赖），
# 但删完票还让页图缓存留着，等于从缓存里就能把已删的票面翻出来。
CACHE_HOOKS = []


def retention() -> int:
    """当前生效的安静期（天）。坏值不采信，落回默认 30——0 天等于没有安静期。"""
    lo, hi = config.PURGE_DAYS_RANGE
    v = config.PURGE_AFTER_DAYS
    return v if isinstance(v, int) and lo <= v <= hi else 30


def set_retention(value):
    """改安静期并落 .env（重启后还在）。成功返回改完的值，**没收下的一律返回 None**
    ——非数字与越界都给同一个信号，路由才好统一回 400。
    静默保留原值看着安全，实际最坏：管理员填了个 0、界面没报错、他以为改成了，
    而盘上还是 30 天。"这个值我没收"必须是听得见的。"""
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


def _now(now):
    return _at(now) or datetime.now()


def _aged(at, days, now):
    """躺够日子了吗。时刻读不出来的（有人手工改过 json、或将来换了时间格式）一律当"没满"——
    认不出就别删，这条判据错了只可能是"少删"，不会是多删。"""
    t = _at(at)
    return bool(t) and now - t >= timedelta(days=max(0, int(days)))


def _idle_days(at, now):
    """已经安静了多少天。面板要答"为什么这张能删"，光给一个时刻不够直观。"""
    t = _at(at)
    return int((now - t).days) if t else None


def _mtime_iso(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _json(path: Path) -> dict:
    """读一份落盘的 JSON，**读不出就当没有**。这里的 try 必须把 `is_file()` 一起包住：
    单段文件名超过 Linux 的 255 字节上限时 `stat` 是直接抛 ENAMETOOLONG 的，
    而清理这一路不能因为"有一个路径没法 stat"就整次崩掉——它要说的是这张票不在盘上。"""
    try:
        if not path.is_file():
            return {}
        got = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _latest(*vals) -> str:
    """这些时刻里最晚的那一个（ISO 串）。全都读不出来就给空串——
    空串过不了 _aged，于是"时间算不出"一律站在少删那一侧。"""
    ts = [t for t in (_at(v) for v in vals) if t]
    return max(ts).isoformat(timespec="seconds") if ts else ""


def _staged_touched(stem: str) -> str:
    """暂存记录里"最后一次有人动它"：events 追加不覆盖，取最后一条；
    提交也算一次动（submitted_at 单独盖在记录上）。"""
    rec = _json(config.STAGED_DIR / (stem + ".json"))
    events = rec.get("events") or []
    last = events[-1].get("at") if events and isinstance(events[-1], dict) else ""
    return _latest(last, rec.get("staged_at"), rec.get("submitted_at"))


def quiet_at_hawb(stem: str, qc: dict = None) -> str:
    """一张分单的最后被碰时刻 = 三处里最晚的：解析时间（qc.processed_at）、
    暂存记录里最后一次人动它、归档目录的 mtime（原件事后被换过/放回来也看得到）。

    三处都要看是因为只看一处会多删：一张上周刚被复核并暂存过的票，
    qc.processed_at 可能还停在四十天前那次解析上。"""
    qc = _json(config.OUTPUT_QC_DIR / (stem + ".json")) if qc is None else qc
    return _latest(qc.get("processed_at"), _staged_touched(stem),
                   _mtime_iso(config.ARCHIVE_DIR / stem))


def quiet_at_master(mkey: str) -> str:
    """主单同理：缓存文件与原件目录的 mtime，再加上主单提交台账里那次提交的时间。"""
    led = store.master_ledger().get(mkey) or {}
    return _latest(_mtime_iso(config.MASTER_DIR / (mkey + ".json")),
                   _mtime_iso(config.MAWB_SOURCE_DIR / mkey), led.get("submitted_at"))


def _paths(p: dict) -> list:
    """这张票要动的全部路径。顺序不作数——真正管顺序的是 remove() 那三段。"""
    return list(p["files"]) + list(p["dirs"]) + list(p.get("sources") or [])


def _size(p: Path) -> int:
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size if p.exists() else 0
    except OSError:
        return 0


def _void_mark(voided: dict, kind: str, key: str) -> dict:
    """作废只是行上的一个标记：没作废过的票要能写成"没作废"，而不是把空串糊上去。"""
    v = voided.get(kind + "|" + key) or {}
    return {"voided": bool(v), "void_by": str(v.get("by") or ""), "void_at": str(v.get("at") or "")}


def _due(quiet, days, now, aged):
    """还有几天到期。读不出盘上时间的给 None——面板要写"算不出"，不许拿一个假天数糊人。"""
    if aged:
        return 0
    idle = _idle_days(quiet, now)
    return None if idle is None else max(0, int(days) - idle)


def _hawb_plan(stem, days, now, ledger, voided, only_aged=True):
    """一张分单：把它的落点列出来。没满安静期、或任何一步不合法，都返回 None
    （不产生任何删除动作）。

    清单分三段，`remove()` 一段删完没出错才走下一段：
      files    派生物（上传件、L1/L2/L3、红旗、预览、幂等锁）
      sources  安静时间的两处来源（qc 与 staged）
      dirs     归档目录（第三处来源）
    这个顺序是判据改了之后新加的硬要求：中途崩一次时只要三处来源还剩一处，这张票就还在
    名册上、下一轮能接着删完。反过来（来源先没了），quiet 算不出来 → 判"没满" → 它永远
    不再进名单，剩下那半截文件就成了没人来找的孤儿数据——和"先删台账条目"是同一个坑，
    只是这回到了"时间"上。"""
    entry = ledger.get(stem) or {}
    qc = _json(config.OUTPUT_QC_DIR / (stem + ".json"))
    air = _json(config.OUTPUT_AIR_DIR / (stem + ".json"))
    quiet = quiet_at_hawb(stem, qc)
    aged = _aged(quiet, days, now)
    if only_aged and not aged:
        return None
    files, dirs = [], []
    # input 那一格：source_name 是当年用户传上来的文件名，进过台账也进过 qc，
    # 一样要过单段校验才敢拼路径——不能因为"是我们自己写进去的"就省了。
    src = store.safe_stem(str(qc.get("source_name") or ""))
    if src:
        p = config.INPUT_DIR / src
        if p.is_file():
            files.append(p)
    for p in (config.TRANSCRIPT_DIR / (stem + ".json"), config.OUTPUT_RAW_DIR / (stem + ".json"),
              config.OUTPUT_AIR_DIR / (stem + ".json"), config.PREVIEW_DIR / (stem + ".pdf")):
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
    # 判据是"安静了多久"之后多出来的一条硬要求：能重算出安静时间的那几份要留到最后，
    # 而且**名单的扫描键（qc）排在这段的最后一个**。remove() 是一段删干净才走下一段——
    # 中途任何一步失败，这张票要么还在 qc 上、要么还在 staged/归档目录上，下一轮照样能
    # 在名册里看到它接着删完。反过来（qc 先没了、后面还剩个 preview），它就永久失踪了：
    # 安静时间算不出来 → 判"没满" → 再也没人来收那半截文件。
    sources = [p for p in (config.STAGED_DIR / (stem + ".json"),
                           config.OUTPUT_QC_DIR / (stem + ".json")) if p.exists()]
    p = {"kind": "hawb", "stem": stem, "key": "hawb|" + stem,
         "mawb": mawb, "hawb": hawb, "in_ledger": stem in ledger, "days": days,
         "quiet_at": quiet, "idle_days": _idle_days(quiet, now),
         "aged": aged, "due_in_days": _due(quiet, days, now, aged),
         "submitted_at": str(entry.get("submitted_at") or ""),
         "receipt_action": str((entry.get("receipt") or {}).get("action") or ""),
         "files": files, "dirs": dirs, "sources": sources,
         "drop": {"ledger": stem, "void": "hawb|" + stem},
         "rebuild": True}
    p.update(_void_mark(voided, "hawb", stem))
    return p


def _mawb_plan(mkey, days, now, voided, only_aged=True):
    if store.submitted_by_mawb(mkey):
        return None          # 名下还有已提交的分单：先把它们清掉，再谈删主单
    quiet = quiet_at_master(mkey)
    aged = _aged(quiet, days, now)
    if only_aged and not aged:
        return None
    files, dirs, sources = [], [], []
    src = config.MAWB_SOURCE_DIR / mkey
    if src.is_dir():
        dirs.append(src)
    cache = config.MASTER_DIR / (mkey + ".json")
    if cache.is_file():
        sources.append(cache)      # 名册是按这份缓存扫主单的，它必须最后一个动
    led = store.master_ledger().get(mkey) or {}
    p = {"kind": "mawb", "stem": "", "key": "mawb|" + mkey,
         "mawb": mkey, "hawb": "", "in_ledger": bool(led), "days": days,
         "quiet_at": quiet, "idle_days": _idle_days(quiet, now),
         "aged": aged, "due_in_days": _due(quiet, days, now, aged),
         "submitted_at": str(led.get("submitted_at") or ""), "receipt_action": "",
         "files": files, "dirs": dirs, "sources": sources,
         "drop": {"master": mkey, "void": "mawb|" + mkey}, "rebuild": False}
    p.update(_void_mark(voided, "mawb", mkey))
    return p


def plan(kind, key, days, now=None):
    """这张票现在能不能删、删的话要动哪些路径。不能删一律返回 None。

    kind 只认 hawb/mawb；days 由调用方给——**预览可以传 0（"现在就给我看"），
    真正执行的那一路只读服务端存的安静期**，否则请求里带个 days=0 就把它绕过去了。"""
    k = str(kind or "").strip().lower()
    raw = str(key or "").strip()
    if k not in ("hawb", "mawb") or not raw:
        return None
    now = _now(now)
    voided = store.voided_map()
    if k == "hawb":
        stem = store.safe_stem(raw)
        if not stem:
            return None
        p = _hawb_plan(stem, days, now, store.ledger(), voided)
    else:
        # 合法性只看字符集与长度（mawb_source_key）；**不能拿 mawb_source_dir() 当校验用**——
        # 那个函数还要求原件目录存在，而线上没有任何一处代码会往 output/mawb_source/ 写
        # （公司主单原件接口待 IT），于是每张主单都会被它判成"不合法"、永远删不掉。
        # "这台机器上没这张主单"不在这里判：`_mawb_plan` 里"安静时间读不出来就不删"已经盖住了
        # （没痕迹 ⇒ 读不出时间），这里再写一遍就是个骗下一人的死分支。那句文案在 refuse 里。
        mkey = store.mawb_source_key(raw)
        if not mkey:
            return None
        p = _mawb_plan(mkey, days, now, voided)
    if not p:
        return None
    p["bytes"] = sum(_size(x) for x in _paths(p))
    return p


def _hawb_stems() -> list:
    """票的名册 = output/qc/ 里那几份 json（一张经手过的票必有一份；台账那一行的骨架也是它）。
    不再拿 voided.json 当名单——那本只记"谁收起来过"，而绝大多数躺够日子的票没人收过。"""
    out = []
    try:
        for p in sorted(config.OUTPUT_QC_DIR.glob("*.json")):
            stem = store.safe_stem(p.stem)
            if stem:
                out.append(stem)
    except OSError:
        return []
    return out


def _master_keys() -> list:
    out = []
    try:
        for p in sorted(config.MASTER_DIR.glob("*.json")):
            mkey = store.norm_no(p.stem)
            if mkey and mkey == p.stem:          # 文件名必须是归一化后的号，不然不拼路径
                out.append(mkey)
    except OSError:
        return []
    return out


def candidates(days=None, now=None, with_bytes=True, only_aged=True):
    """面板、顶栏徽标与 run_purge.py --auto 读的同一份名册。
    徽标那条路径传 with_bytes=False：stat 归档目录比读两份本地 JSON 贵得多，而它只要一个条数。
    `only_aged=False` 是给面板的"全部票 + 倒计时"视图用的——**徽标与自动那一路永远不传它**，
    否则"有票到期该清了"那个数字会变成"这台机器一共有几张票"。"""
    d = retention() if days is None else max(0, int(days))
    now = _now(now)
    voided, ledger = store.voided_map(), store.ledger()
    out = []
    for stem in _hawb_stems():
        p = _hawb_plan(stem, d, now, ledger, voided, only_aged)
        if p:
            out.append(p)
    for mkey in _master_keys():
        p = _mawb_plan(mkey, d, now, voided, only_aged)
        if p:
            out.append(p)
    for p in out:
        # 没到期的那些不算字节：一行一次目录遍历，票数一多这页就慢得没法看，
        # 而那一格对"还要等几天"的人也没有意义。None 让界面写"—"，0 才是"真的不占"。
        if not with_bytes:
            p["bytes"] = 0
        elif not p["aged"]:
            p["bytes"] = None
        else:
            p["bytes"] = sum(_size(x) for x in _paths(p))
    return sorted(out, key=lambda x: (x["quiet_at"], x["key"]))


def _tell_caches(stem: str):
    for hook in CACHE_HOOKS:
        try:
            hook(stem)
        except Exception:
            pass          # 缓存清不掉不该让整次删除失败：文件是真删了，缓存只是内存里的一份副本


def _row(p, auto=False, who="", early=False):
    """凭据那一行的内容。刻意只放号与文件名——想知道删了哪几个文件，不想知道票上写了什么。

    auto 与 by/who 分两格记：自动档 by 是 "auto"、who 留空（没有"那个人"）；人工档 by 就是
    那个登录名。事后翻这本要能分清"日子到了自己走的"和"某个管理员挑出来按下去的"。
    early（v1.18.0）再分一档：那张还没到安静期就被人删了，这是绕过了安全期，得单独查得到。"""
    who = str(who or "")
    return {"at": datetime.now().isoformat(timespec="seconds"), "kind": p["kind"], "key": p["key"],
            "stem": p["stem"], "mawb": p["mawb"], "hawb": p["hawb"], "days": p["days"],
            "quiet_at": p["quiet_at"], "idle_days": p["idle_days"], "voided": p["voided"],
            "void_by": p["void_by"], "void_at": p["void_at"], "submitted_at": p["submitted_at"],
            "receipt_action": p["receipt_action"], "in_ledger": p["in_ledger"],
            "auto": bool(auto), "by": "auto" if auto else who, "who": "" if auto else who,
            "early": bool(early),
            "files": [x.name for x in _paths(p)]}


def _append_tombstone(rec):
    config.PURGED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    # 0600 要在"打开新建"那一次就给：这本里有 void_by 与 by/who（人名）。先按默认 umask 落成 644
    # 再补一次 chmod，中间那个窗口里 output/ 在这台共用机上别的进程就读得到了。
    # 文件已存在时这个 mode 被内核忽略——不会把运维手工调过的模式改回去。
    fd = os.open(str(config.PURGED_LEDGER), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def remove(p, by, now=None, auto=False, early=False):
    """执行一张。顺序见模块 docstring 第 1 条——凭据先落，台账最后动。

    删文件分两段，**上一段有失败就不走下一段**：
      一段 派生物（input / L1 / L2 / L3 / 红旗 / 预览 / 幂等锁 / 主单原件目录）
      二段 安静时间的来源（归档目录 → qc / staged / 主单缓存）
    第二段留着的是"还能算出这张安静了多久"的那几份，所以中途崩一次这张票仍会在
    下一轮的名册里出现，能把剩下的半截接着删完。"""
    removed, errors = [], []
    _append_tombstone(_row(p, auto=auto, who=by, early=early))      # 1) 先留痕
    for stage in (list(p["files"]), list(p["dirs"]) + list(p.get("sources") or [])):
        for f in stage:                                # 2) 再删文件，一段一段来
            try:
                if f.is_dir():
                    shutil.rmtree(f)
                elif f.exists():
                    f.unlink()
                removed.append(f.name)
            except OSError as e:
                errors.append("%s: %s" % (f.name, e))
        if errors:
            break                                      # 来源那一整段就此不碰
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
        store.unvoid_row(kind, key)                   # 票都没了，作废戳留着就是个悬空键
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


def refuse(kind, key, days, now=None) -> str:
    """这张为什么不在名册里——那句要说给人看（面板与 run_purge.py 都用它），
    不能只回一个"不在候选里"。"""
    now = _now(now)
    k = str(kind or "").strip().lower()
    raw = str(key or "").strip()
    if k not in ("hawb", "mawb"):
        return "票种只认 hawb/mawb"
    if k == "hawb":
        stem = store.safe_stem(raw)
        if not stem:
            return "这个单号/票名不合法（带路径分隔符或太长），不做任何删除"
        if not (config.OUTPUT_QC_DIR / (stem + ".json")).is_file():
            return "这台机器上查无这张票（连质检记录都没有），没有可删的东西"
        quiet = quiet_at_hawb(stem)
    else:
        mkey = store.mawb_source_key(raw)      # 只判字符集与长度，不问原件目录在不在
        if not mkey:
            return "这个单号/票名不合法（带路径分隔符或太长），不做任何删除"
        if not (config.MASTER_DIR / (mkey + ".json")).is_file() \
                and not (config.MAWB_SOURCE_DIR / mkey).is_dir():
            return "这台机器上查无这张主单（既没有解析缓存也没有原件目录），没有可删的东西"
        if store.submitted_by_mawb(mkey):
            return "这张主单名下还有已提交的分单，先把分单删掉才轮到它"
        quiet = quiet_at_master(mkey)
    if not quiet:
        return "这张票的盘上时间读不出来，宁可不删（判据只能往少删一侧错）"
    if not _aged(quiet, days, now):
        return "还没安静满 %s 天：最后一次被碰是 %s，到现在已经安静 %s 天" % (
            days, quiet, _idle_days(quiet, now))
    return "这张票在名册里算不出来"


def purge(items, by, days=None, now=None, auto=False):
    """逐张执行。days 不传就用服务端那一份；**调用方是路由时不要从请求里取这个参数**。
    不在名册里的项（没满安静期 / 票已经不在盘上 / 主单名下还挂着分单）逐张报原因，
    不影响同批其余的。auto=True 是定时器那一路，凭据里 by 写 "auto"、who 留空。

    项上带 `early`（v1.18.0 的手动提前删）才允许越过安静期：内部按 days=0 重算落点，
    其余各条拒绝照旧生效（票不在盘上、名字带穿越、主单名下还有分单、时间读不出）。
    凭据里那一格 `early` **由服务端按"这张安静了多久 vs 当下窗口"算出来**，不信请求说的——
    满了的票谎报 early 也会被记成正常清理。"""
    d = retention() if days is None else max(0, int(days))
    out = []
    for it in items or []:
        kind = str((it or {}).get("kind") or "").strip().lower()
        key = str((it or {}).get("key") or "").split("|", 1)[-1].strip()
        want_early = bool((it or {}).get("early"))
        p = plan(kind, key, 0 if want_early else d, now)
        if not p:
            out.append({"ok": False, "key": (it or {}).get("key", ""), "removed": [],
                        "error": refuse(kind, key, d, now)})
            continue
        early = p["idle_days"] is None or p["idle_days"] < d
        p["days"] = d              # 凭据与面板看的都是当下窗口，不是内部那个 0
        out.append(remove(p, by, now, auto=auto, early=early))
    return out


def write_run_state(res, days, at=None) -> dict:
    """自动清理跑完把这一步写在盘上：面板顶上那句「上次自动清理」读的就是它。
    定时器是独立进程，web 进程既不知道它跑没跑、也不知道它失败过——不写下来就是静默失败。"""
    bad = [r for r in res or [] if not r.get("ok")]
    rec = {"at": str(at or datetime.now().isoformat(timespec="seconds")), "days": days,
           "checked": len(res or []), "removed": len(res or []) - len(bad), "failed": len(bad),
           "errors": [str(r.get("key") or "") + "：" + str(r.get("error") or "") for r in bad][:8]}
    atomic.write_json(config.PURGE_RUN_STATE, rec, mode=0o600)
    return rec


def read_run_state() -> dict:
    """没有这本书＝这台机器上自动清理从没跑过。面板要这么说，
    而不是显示一个空日期——那看着像"跑过、什么都没删"。"""
    return _json(config.PURGE_RUN_STATE)
