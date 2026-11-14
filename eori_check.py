# -*- coding: utf-8 -*-
"""EORI 欧盟官网核验（2026-11-14 业主定案，方案 1C）。

口径三条，都是业主逐条定的：
① 解析完自动后台查 + 核对区 EORI 格有「查官网」手动重查按钮；
② 查出 invalid 挂红旗（列名打头，自动挂到那一列），走人工确认软闸门——确认过就能交；
③ 查不通（超时/页面认不出）只算"这台机器到不了欧盟"，中性提示，**不挂旗**。

事实底账（2026-11-14 公司那台 Qoder 升级回报 + 本机真页对照实锤，**修正 v1.39.0 的两处错**）：
- **结论不在表单页里**：`eori_validation.jsp?Screen=0` 只是表单页——查询号在页里出现 24 处全是
  JS 胶水/语言链接/输入框回填，唯一像结论的 "is not valid" 三处全在 **JS 注释**里（"//The day is
  not valid"）。真结论是页面再发的 AJAX：**`eori_detail.jsp?Lang=en&EoriNumb=<号>`**，那份干净得
  1:1——合法号恰 1 处 "is valid"（并带回持有人资料）、假号恰 1 处 "is not valid"。v1.39.0 打的是
  表单页还拿 "is not valid" 判无效 ⇒ **可达机器上每个号都会被判无效**（本机隔离台验收当晚两次
  "无效"全是假阳性：SE5563646560 其实是瑞典 K G Fridman AB 的有效号）。三份真页面留成夹具
  `tests/fixtures/eori/`（valid/valid_it/invalid + 表单陷阱页），回归拿真的跑。
- 证书：默认上下文（Windows 根库不全的机器只有 20+ 条）可能 `CERTIFICATE_VERIFY_FAILED`。
  回退链：默认 → `EORI_CAFILE` 环境变量 → Git for Windows 自带的 CA bundle
  （公司那台实测可用）。**每一档都是真验证，绝不关校验**；全失败仍回 unreachable（中性档）。
- 单次 1-2 秒（detail 端点比表单页快）→ 仍后台 + 缓存；GB 前缀的号是英国 HMRC 发的（脱欧后
  不在欧盟 EOS 里），官网查它必回 invalid——真号会吃假旗，所以跳过不查。

缓存 output/eori_cache.json：valid/invalid 留 30 天，unreachable 只留 1 小时
（网络恢复自动重查）；手动重查绕过缓存强制刷新并覆盖缓存。
"""
import datetime
import json
import os
import re
import ssl
import threading
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

import atomic
import codes
import config
import store

DETAIL_URL = "https://ec.europa.eu/taxation_customs/dds2/eos/eori_detail.jsp"
_UA = {"User-Agent": "Mozilla/5.0 (JRT-hawb-desk)"}
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


def _ca_candidates() -> list:
    """CA 来源候选链：默认上下文先来；失败后试 `EORI_CAFILE` 环境变量指的文件、再试
    Git for Windows 自带的 CA bundle（Windows 根库不全的机器靠它，公司那台实测可用）。
    刻意不引第三方包：锁测试要求代码 import 的包必须在锁里，certifi 没装就不许假装有。"""
    out = [None]
    for p in (os.getenv("EORI_CAFILE", ""),
              r"C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt",
              r"C:\Program Files\Git\usr\etc\ssl\certs\ca-bundle.crt",
              r"C:\Program Files (x86)\Git\mingw64\etc\ssl\certs\ca-bundle.crt"):
        if p and p not in out and os.path.isfile(p):
            out.append(p)
    return out


def _fetch(url: str, timeout: float) -> str:
    """按 CA 候选链逐个真验证地试：默认上下文先来；**只有**证书链失败才换下一个来源
    （超时/DNS 这类换了也没用，直接抛）。链尾也不关校验——修不好就老实回中性档。"""
    last: Exception | None = None
    for ca in _ca_candidates():
        try:
            ctx = ssl.create_default_context(cafile=ca)
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            if isinstance(reason, ssl.SSLCertVerificationError) \
                    or "CERTIFICATE_VERIFY_FAILED" in str(reason or e):
                last = e
                continue          # 证书链失败：换下一个 CA 来源重试
            raise
    raise last if last else RuntimeError("no CA source could verify")


def check_one(num: str, timeout: float = 15.0) -> str:
    """查一个号 → "valid" / "invalid" / "unreachable"（页面回不来或认不出结论都不判死）。
    打的是**结论端点** `eori_detail.jsp`（表单页 eori_validation.jsp 里没有结论，
    只有 JS 注释——v1.39.0 拿它数子串把有效号全判成无效，见模块头）。判据在 detail
    端点是 1:1 干净的：恰一处 "is valid" 或恰一处 "is not valid"。
    熔断期间不发网络请求，直接回 unreachable。"""
    with _LOCK:
        cache = _load()
        now = datetime.datetime.now()
        if _breaker_active(cache, now):
            return "unreachable"
    url = f"{DETAIL_URL}?Lang=en&EoriNumb={urllib.parse.quote(num)}"
    try:
        body = _fetch(url, timeout).lower()
    except Exception:
        _note_unreachable()
        return "unreachable"
    # 判据=**完整结论句**（detail 端点的原句 "This EORI number is (not) valid."），
    # 不拿裸子串——表单页的 JS 注释里就有 "The day is not valid"，裸子串会把陷阱页判死。
    if "this eori number is not valid" in body:
        _note_ok()
        return "invalid"
    if "this eori number is valid" in body:
        _note_ok()
        return "valid"
    return "unreachable"    # 页面回来了但认不出结论：当查不到，不判死


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
