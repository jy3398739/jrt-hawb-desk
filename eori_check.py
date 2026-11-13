# -*- coding: utf-8 -*-
"""EORI 欧盟官网核验（2026-11-14 业主定案，方案 1C）。

口径三条，都是业主逐条定的：
① 解析完自动后台查 + 核对区 EORI 格有「查官网」手动重查按钮；
② 查出 invalid 挂红旗（列名打头，自动挂到那一列），走人工确认软闸门——确认过就能交；
③ 查不通（超时/页面认不出）只算"这台机器到不了欧盟"，中性提示，**不挂旗**。

事实底账（2026-11-13 探针实测）：
- 官网就是 GET 一个 URL 即 API：eori_validation.jsp?Screen=0&EoriNumb=<号>&Expand=false&Lang=en；
- 有效号回页含 "Valid EORI number"，无效号含 "is not valid"，单次 6-10 秒 → 必须后台 + 缓存；
- VM 与本机到 ec.europa.eu 网络不通（超时），公司那台通 → ③ 正是为这台兜的；
- GB 前缀的号是英国 HMRC 发的（脱欧后不在欧盟 EOS 里），官网查它必回 invalid——
  真号会吃假旗，所以跳过不查。

缓存 output/eori_cache.json：valid/invalid 留 30 天，unreachable 只留 1 小时
（网络恢复自动重查）；手动重查绕过缓存强制刷新并覆盖缓存。
"""
import datetime
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import atomic
import codes
import config
import store

EORI_URL = "https://ec.europa.eu/taxation_customs/dds2/eos/eori_validation.jsp"
SKIP_PREFIX = ("GB",)
VALID_DAYS = 30
UNREACHABLE_HOURS = 1
# 识别号格的三个列名：分单两列、主单三列共用（TAX 列不是 EORI，不查）
EORI_COLS = ("SHIPPER_INFO_EORI", "CONSIGNEE_INFO_EORI", "NOTIFY_INFO_EORI")

CACHE_PATH = Path(os.getenv("EORI_CACHE", str(config.BASE_DIR / "output" / "eori_cache.json")))

_LOCK = threading.Lock()
_CACHE = None
# 熔断：连不上的机器（VM/本机到 ec.europa.eu 不通）别每张票都白等 15 秒超时——
# 连续 UNREACHABLE_STRIKES 次"unreachable"就停网络 UNREACHABLE_BACKOFF_HOURS 小时，
# 期间一律直接回 unreachable（中性，不挂旗）；任何一次有效结论都会解除熔断。
UNREACHABLE_STRIKES = 2
UNREACHABLE_BACKOFF_HOURS = 6
_META_KEY = "_meta"


def _breaker_active(cache: dict, now: datetime.datetime) -> bool:
    meta = cache.get(_META_KEY) or {}
    try:
        until = datetime.datetime.fromisoformat(str(meta.get("unreachable_until") or ""))
    except ValueError:
        return False
    return now < until


def _breaker_strikes(cache: dict) -> int:
    return int((cache.get(_META_KEY) or {}).get("strikes") or 0)


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _CACHE = {}
    return _CACHE


def _save():
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(CACHE_PATH, _CACHE)


def _status_of(rec) -> str | None:
    """缓存记录还算不算数：valid/invalid 30 天，unreachable 1 小时，过期/坏记录一律重查。"""
    if not isinstance(rec, dict):
        return None
    st = rec.get("status")
    try:
        t = datetime.datetime.fromisoformat(str(rec.get("checked_at") or ""))
    except ValueError:
        return None
    age = datetime.datetime.now() - t
    if st in ("valid", "invalid"):
        return st if age <= datetime.timedelta(days=VALID_DAYS) else None
    if st == "unreachable":
        return st if age <= datetime.timedelta(hours=UNREACHABLE_HOURS) else None
    return None


def check_one(num: str, timeout: float = 15.0) -> str:
    """查一个号 → "valid" / "invalid" / "unreachable"（页面回不来或认不出结论都不判死）。
    熔断期间不发网络请求，直接回 unreachable。"""
    with _LOCK:
        cache = _load()
        now = datetime.datetime.now()
        if _breaker_active(cache, now):
            return "unreachable"
    url = f"{EORI_URL}?Screen=0&EoriNumb={urllib.parse.quote(num)}&Expand=false&Lang=en"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (JRT-hawb-desk)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace").lower()
    except Exception:
        _note_unreachable()
        return "unreachable"
    if "is not valid" in body:
        _note_ok()
        return "invalid"
    if "valid eori number" in body or "is valid" in body:
        _note_ok()
        return "valid"
    return "unreachable"


def _note_ok():
    """拿到一次有效结论：解除熔断、清零计数。"""
    with _LOCK:
        cache = _load()
        if _META_KEY in cache:
            del cache[_META_KEY]
        _save()


def _note_unreachable():
    """再记一次连不上：连续够数就熔断一段时间（写进缓存文件，重启也记得）。"""
    with _LOCK:
        cache = _load()
        strikes = _breaker_strikes(cache) + 1
        meta = cache.get(_META_KEY) or {}
        if strikes >= UNREACHABLE_STRIKES:
            meta = {"strikes": 0,
                    "unreachable_until": (datetime.datetime.now()
                                          + datetime.timedelta(hours=UNREACHABLE_BACKOFF_HOURS)
                                          ).isoformat(timespec="seconds")}
        else:
            meta = {"strikes": strikes}
        cache[_META_KEY] = meta
        _save()


def numbers(d: dict) -> dict:
    """一票 EORI 列里的 EORI 形态号：{列名: [裸号, ...]}。判形与 validator._eori_cell_ok 同源
    （codes.split_kind_ids 分流 + 本体两位字母开头/含数字），标签连写的摘掉标签再查；
    GB 跳过（见模块头）；重复号只查一次。"""
    out: dict = {}
    for col in EORI_COLS:
        eoris, _t = codes.split_kind_ids(d.get(col, "") or "")
        nums = []
        for v in eoris:
            num = codes.clean_tax(v)
            if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{6,15}", num) and any(c.isdigit() for c in num) \
                    and not num.startswith(SKIP_PREFIX):
                nums.append(num)
        if nums:
            out[col] = list(dict.fromkeys(nums))
    return out


def cached_flags(d: dict) -> list:
    """按缓存出旗：只挂 invalid（旗面列名打头，审核台按列名挂旗）。未查过/查不通一律安静
    ——中性口径，不拦人，也不伪造"查过了"。"""
    flags = []
    with _LOCK:
        cache = {k: dict(v) if isinstance(v, dict) else None for k, v in _load().items()}
    for col, nums in numbers(d).items():
        for num in nums:
            if _status_of(cache.get(num)) == "invalid":
                flags.append(f"{col} EORI 官网核验不通过: {num}（欧盟官网显示无效号码，需人工核票；"
                             "确认号没问题就点红旗确认，改对后旧旗随值消失）")
    return flags


def check_cells(d: dict, force: bool = False, timeout: float = 15.0) -> dict:
    """把一票 EORI 列的号都查一遍（force=True 绕过缓存强制刷新）。返回 {列名: {号: 状态}}，
    状态取值 valid / invalid / unreachable（缓存命中的直接带回，未命中现场查）。"""
    with _LOCK:
        cache = _load()
    res: dict = {}
    todo = []
    for col, nums in numbers(d).items():
        r = {}
        for num in nums:
            st = None if force else _status_of(cache.get(num))
            r[num] = st
            if st is None:
                todo.append(num)
        res[col] = r
    for num in dict.fromkeys(todo):
        st = check_one(num, timeout)
        with _LOCK:
            cache = _load()
            cache[num] = {"status": st, "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
            _save()
        for col in res:
            if num in res[col]:
                res[col][num] = st
    return res


def append_qc_flags(stem: str, flags: list):
    """把核验旗并进这张票已落盘的质检记录（去重）。解析后后台线程与手动重查共用：
    旗在 qc 里，审核台打开票就能看到，不必等到提交门。"""
    if not flags:
        return
    path = config.OUTPUT_QC_DIR / f"{stem}.json"
    try:
        qc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return                      # 没有质检记录（旧票/失败票）就不硬造一份
    old = qc.get("flags") or []
    merged = old + [f for f in flags if f not in old]
    if merged != old:
        qc["flags"] = merged
        qc["needs_review"] = bool(merged)
        store.save_qc(stem, qc)
