# -*- coding: utf-8 -*-
"""公司系统接口适配层（主分单审核台）。

口径（2026-09-26 契约到货）：j9 AMS 录入接口——主单/分单读+写四个接口，全部 POST+JSON，
请求头 X-Api-Key（主单组与分单组两把 key 互不通用）。MODE=live 时真连，mock 保持原行为：
- search_mawb：live 走 `j9/hawb` 按主单号拉分单行 + `j9/mawb/` 拉主单原生资料（含 AMS_RECORD），
  再用本机提交台账把已归档原件的 stem 联结进去（前端 /source 打开原件靠它）。
- submit_master：主单回传走 `j9/mawb2/`，同样先读后写（SEND_STATUS 闸门 + 整表写回补齐未改列）。
- submit_order：live 走"先读后写"——`j9/hawb` 读回那一行（SEND_STATUS 闸门：仅 0/2 可更新），
  用复核后的 air 字段按映射覆盖（ORIGIN_NAME→ORGIN_NAME、CONSIGNEE_INFO_CITTY→…_CITY），
  **整行原样带上未改的列**后 POST `j9/hawb2`。整表写回语义：缺列=写 NULL，绝不能只发我们有的字段。

j9 侧要点（见 IT《AMS录入接口调用说明》）：MAWB_NO 硬性 3位-8位带横杠；SEND_STATUS/CREATE_TIME/
HAWB_ID 服务端忽略；分单表没有 HS 列；限流每把 key 10 次/秒。密钥只进 .env，绝不写死在这里。
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import config
import master_fields as mf
import retry
import store

# 读接口的 SHIPPER_INFO/CONSIGNEE_INFO 是 Python repr 的列表串，展示/合并前归一。
_LIST_REPR_RE = re.compile(r"^\[.*\]$", re.S)

# 我方字段名 → 公司 j9 列名。分单表没有 GOODS_HS_CODE（那是主单 AMS_RECORD 的 GOODS_INFO_HSCODE）。
_COLUMN_REMAP = {"ORIGIN_NAME": "ORGIN_NAME", "CONSIGNEE_INFO_CITTY": "CONSIGNEE_INFO_CITY"}

# 幂等锁的有效期：j9 没有幂等键，重复提交=再来一次整表写回，所以同内容只认这几十秒内的重复点。
GUARD_TTL = 90


class CompanyNotConfigured(RuntimeError):
    """COMPANY_API_MODE=live 但没接线/缺配置时抛出，避免静默返回假数据。"""


class CompanyApiError(RuntimeError):
    """live 调用失败：HTTP 非 200 或 j9 返回 code!=0，带 j9 的 detail 原文。"""


class CompanyPreReadFailed(CompanyApiError):
    """**该有行却读不到**：本机台账证明我们给它录过一次，这次读回空——只能是接口抖动/查询没对上。
    此时继续写会要命：j9 的 hawb2/mawb2 是整表写回，没带的列一律写成 NULL，等于把那一行其余列清空。
    宁可拒绝，让制单员等一次好使的读。"""


class CompanyLocked(RuntimeError):
    """SEND_STATUS 闸门拦下：那行已发送/锁定（仅 0/2 可更新），绝不能发 hawb2。"""


_RATE = {"hits": [], "lock": threading.Lock()}


def _throttle():
    """j9 文档：每把 key 10 次/秒。这里按 8/s 自查——先在自己这边等，比让公司回 429 再让人重点一次好。"""
    with _RATE["lock"]:
        now = time.monotonic()
        hits = [t for t in _RATE["hits"] if now - t < 1.0]
        if len(hits) >= config.J9_RATE_PER_SEC:
            time.sleep(max(0.0, 1.0 - (now - hits[0])))
            now = time.monotonic()
            hits = [t for t in hits if now - t < 1.0]
        hits.append(now)
        _RATE["hits"] = hits


def _j9_post(path: str, body: dict, key: str, timeout: float | None = None) -> dict:
    """调 j9 接口。path 形如 `/api/v1/j9/hawb`；HTTP 非 200 或 code!=0 都抛 CompanyApiError。

    超时读 10s / 写 20s（路径以 2 结尾的是写接口，写要落库）。只有对端抖动
    （429/5xx/连不上）才退避重试；400 这类业务错是我们报文自己的问题（限长、单号格式），
    重试只会再撞一次并把人等更久，直接把公司原话抛出去。"""
    is_write = path.rstrip("/").endswith("2")
    if timeout is None:
        timeout = config.J9_TIMEOUT_WRITE if is_write else config.J9_TIMEOUT_READ
    url = config.COMPANY_API_URL + path
    payload = json.dumps(body).encode("utf-8")
    out = None
    for attempt in range(config.J9_RETRIES):
        _throttle()
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "X-Api-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8")).get("detail", "")
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < config.J9_RETRIES - 1:
                retry.wait(attempt)
                continue
            raise CompanyApiError(f"j9 {path} HTTP {e.code}: {detail or e.reason}") from None
        except urllib.error.URLError as e:
            if attempt < config.J9_RETRIES - 1:
                retry.wait(attempt)
                continue
            raise CompanyApiError(f"j9 {path} 连不上：{e.reason}") from None
    if out.get("code") != 0:
        raise CompanyApiError(f"j9 {path} 返回 code={out.get('code')}: {out.get('message', '')}")
    return out


def _norm_mawb(mawb: str) -> str:
    """j9 硬性要求 3位-横杠-8位。我们台账里都是带横杠形态，防御性补一下。"""
    digits = re.sub(r"\D", "", str(mawb or ""))
    return f"{digits[:3]}-{digits[3:]}" if len(digits) == 11 else str(mawb or "").strip()


def _unrepr(v):
    """读接口的聚合列是 repr 列表串：归一成可读文本（仅展示用，写回时原样带上不改动）。"""
    s = str(v or "").strip()
    if _LIST_REPR_RE.match(s):
        try:
            parsed = json.loads(s.replace("'", '"'))
            return " | ".join(str(x).strip() for x in parsed if str(x).strip())
        except Exception:
            return s
    return re.sub(r"\s+", " ", s)


def search_mawb(mawb: str) -> dict:
    """按主单号检索该主单及其名下分单。"""
    mawb = str(mawb or "").strip()
    if config.COMPANY_API_MODE != "live":
        if config.COMPANY_API_MODE != "mock":
            raise CompanyNotConfigured(
                f"COMPANY_API_MODE={config.COMPANY_API_MODE!r}：只能是 mock 或 live。")
        orders = [{"hawb": e.get("hawb", ""), "mawb": e.get("mawb", ""),
                   "stem": e.get("stem"), "reviewer": e.get("reviewer", ""),
                   "submitted_at": e.get("submitted_at", ""),
                   "submitted_here": True}
                  for e in store.submitted_by_mawb(mawb)]
        return {"mode": "mock", "mawb": mawb, "mawb_order": {}, "hawb_orders": orders,
                "source_available": store.mawb_source_dir(mawb) is not None}
    if not config.COMPANY_API_URL or not config.COMPANY_HAWB_KEY:
        raise CompanyNotConfigured("live 模式缺配置：.env 里要配 COMPANY_API_URL 与 COMPANY_HAWB_KEY/COMPANY_MAWB_KEY")
    nm = store.norm_no(mawb)
    rows = _j9_post("/api/v1/j9/hawb", {"master_no": _norm_mawb(mawb)}, config.COMPANY_HAWB_KEY)["data"]
    try:
        master = _j9_post("/api/v1/j9/mawb/", {"master_no": _norm_mawb(mawb)},
                          config.COMPANY_MAWB_KEY)["data"]
    except CompanyApiError:
        master = []   # 主单资料拿不到不挡分单核对：分单行才是检索的主体
    mawb_order = {}
    if master:
        mo = dict(master[0])
        for k in ("SHIPPER_INFO", "CONSIGNEE_INFO", "NOTIFY_INFO", "GOODS_NAME", "GOODS_DESC"):
            if k in mo:
                mo[k] = _unrepr(mo[k])
        mawb_order = mo
    orders = []
    # j9 的行里没有"谁提交/何时提交"，而本机提交台账有。不 join 的话，刚提交过的票在界面上
    # 显示两栏"—"，看着像从没动过——SEND_STATUS=0 在公司侧就是"已录入待发送"，两件事必须分开看。
    mine = {e.get("key"): e for e in store.ledger().values()}
    for row in rows:
        hawb = str(row.get("HAWB_NO", ""))
        stem = store.lookup_stem(mawb, hawb)
        ent = mine.get(store.number_key(mawb, hawb)) or {}
        orders.append({"hawb": hawb, "mawb": str(row.get("MAWB_NO", "")),
                       "stem": stem, "reviewer": ent.get("reviewer") or "—",
                       "submitted_at": ent.get("submitted_at") or "—",
                       "submitted_here": bool(ent),
                       "send_status": row.get("SEND_STATUS"), "row": row})
    return {"mode": "live", "mawb": mawb, "mawb_order": mawb_order, "hawb_orders": orders,
            "source_available": store.mawb_source_dir(mawb) is not None}


def _fingerprint(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def _guard_file(key: str):
    """锁文件只放哈希：主/分单号里有横杠与空格，别拿它们拼路径。"""
    return config.SUBMIT_GUARD_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")


def _post_guarded(key: str, record: dict, path: str, wrapper: str, api_key: str) -> dict:
    """带去重的写：同一张票、同一份内容在 GUARD_TTL 内重复提交，直接回上一次回执，不再写公司。

    j9 没有幂等键，重复点一次就是第二次整表写回；锁用 O_EXCL 建文件，跨进程也认。
    内容变了说明是真要再改一次（SEND_STATUS 0/2 允许反复改），不拦。"""
    fp, gf = _fingerprint(record), _guard_file(key)
    gf.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(gf), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            prev = json.loads(gf.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        if time.time() - float(prev.get("at") or 0) <= GUARD_TTL and prev.get("fp") == fp:
            if prev.get("receipt"):
                return dict(prev["receipt"], idempotent=True)
            raise CompanyApiError("这张票正在提交中，请等上一次的结果出来再点")
        gf.unlink(missing_ok=True)
        try:
            fd = os.open(str(gf), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise CompanyApiError("这张票正在提交中，请等上一次的结果出来再点")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"key": key, "fp": fp, "at": time.time()}, f, ensure_ascii=False)
    try:
        res = _j9_post(path, {wrapper: record}, api_key)
    except Exception:
        gf.unlink(missing_ok=True)     # 写失败不能留锁，否则下一次真重试会被自己拦掉
        raise
    receipt = {"accepted": bool(res.get("success")), "action": res.get("action")}
    gf.write_text(json.dumps({"key": key, "fp": fp, "at": time.time(), "receipt": receipt},
                             ensure_ascii=False), encoding="utf-8")
    return receipt


def submit_master(payload: dict) -> dict:
    """把一条主单资料回传公司（`POST /api/v1/j9/mawb2/`）。mock 只回带 mode 的回执；live 先读后写。

    先读是为了两件事：① 库里这条记录的 SEND_STATUS 不是 0/2 就说明已发送锁定，直接拒绝且
    **一个写请求都不发**；② mawb2 是整表写回，没给的列会被写成 NULL，所以要把读到的列原样
    带上，再用人工核对后的值覆盖。提交体只有主单那 36 列——服务端维护的 HMY_ID/CREATE_TIME/
    SEND_STATUS 和外层资料块都不发。"""
    mawb = _norm_mawb((payload or {}).get("mawb", ""))
    ams = (payload or {}).get("ams") or {}
    if config.COMPANY_API_MODE != "live":
        if config.COMPANY_API_MODE != "mock":
            raise CompanyNotConfigured(f"COMPANY_API_MODE={config.COMPANY_API_MODE!r}：只能是 mock 或 live。")
        return {"mode": "mock", "accepted": True, "mawb": mawb}
    if not mawb:
        raise CompanyApiError("回传公司需要主单号")
    if not config.COMPANY_API_URL or not config.COMPANY_MAWB_KEY:
        raise CompanyNotConfigured("live 模式缺配置：.env 里要配 COMPANY_API_URL 与 COMPANY_MAWB_KEY")
    master = _j9_post("/api/v1/j9/mawb/", {"master_no": mawb}, config.COMPANY_MAWB_KEY)["data"]
    if not master:
        # 读回空列表分两种：我们从没录过这条主单 = 真·首次录入（外层任务存在、AMS 没填过），
        # 照常发；本机台账说录过 = 这次没读到，继续写会把公司那行其余列抹成 NULL，必须拒。
        if store.master_ledger().get(store.norm_no(mawb)):
            raise CompanyPreReadFailed(
                f"没读到主单 {mawb} 在公司的当前值（本机记录显示已提交过），先不要提交")
        existing = {}
    else:
        existing = master[0].get("AMS_RECORD") or {}
    if existing and existing.get("SEND_STATUS") not in (0, 2, None):
        raise CompanyLocked(
            f"{mawb} 不允许更新（j9 侧 SEND_STATUS={existing.get('SEND_STATUS')}，仅 0/2 可改，已发送锁定）")
    record = {col: existing.get(col) for col in mf.MASTER_COLS}
    for col in mf.MASTER_COLS:
        if col in ams:
            record[col] = ams[col]
    record["MAWB_NO"] = mf.norm_mawb_hyphen(mawb)
    receipt = _post_guarded(store.norm_no(mawb), record, "/api/v1/j9/mawb2/", "AMS_RECORD",
                            config.COMPANY_MAWB_KEY)
    return {"mode": "live", "accepted": receipt["accepted"], "action": receipt["action"],
            "mawb": mawb, "before": existing, "idempotent": receipt.get("idempotent") is True}


def submit_order(payload: dict) -> dict:
    """把一张核对/修改后的分单回传公司。mock 只回带 mode 的回执；live 先读后写。"""
    if config.COMPANY_API_MODE != "live":
        if config.COMPANY_API_MODE != "mock":
            raise CompanyNotConfigured(f"COMPANY_API_MODE={config.COMPANY_API_MODE!r}：只能是 mock 或 live。")
        return {"mode": "mock", "accepted": True,
                "mawb": (payload or {}).get("mawb", ""), "hawb": (payload or {}).get("hawb", "")}
    if not config.COMPANY_API_URL or not config.COMPANY_HAWB_KEY:
        raise CompanyNotConfigured("live 模式缺配置：.env 里要配 COMPANY_API_URL 与 COMPANY_HAWB_KEY")
    mawb = _norm_mawb((payload or {}).get("mawb", ""))
    hawb = str((payload or {}).get("hawb", "") or "").strip()
    if not mawb or not hawb:
        raise CompanyApiError("回传公司需要主单号+分单号")
    rows = _j9_post("/api/v1/j9/hawb", {"master_no": mawb, "house_no": hawb},
                    config.COMPANY_HAWB_KEY)["data"]
    existing = next((r for r in rows if store.norm_no(r.get("HAWB_NO")) == store.norm_no(hawb)), None)
    if existing is None and store.number_index().get(store.number_key(mawb, hawb)):
        # 本机台账证明这张已回传过 = 公司一定有这一行，读回空只能当"没读到"（见 CompanyPreReadFailed）
        raise CompanyPreReadFailed(
            f"没读到分单 {hawb} 在公司的当前值（本机记录显示已提交过），先不要提交")
    if existing is not None and existing.get("SEND_STATUS") not in (0, 2):
        raise CompanyLocked(
            f"{hawb} 不允许更新（j9 侧 SEND_STATUS={existing.get('SEND_STATUS')}，仅 0/2 可改，已发送锁定）")
    record = dict(existing) if existing else {"MAWB_NO": mawb, "HAWB_NO": hawb}
    record["MAWB_NO"], record["HAWB_NO"] = mawb, hawb
    air = (payload or {}).get("air") or {}
    _J9_COLS = {"MAWB_NO", "HAWB_NO", "ORGIN_NAME", "TO1", "TO2", "TO3", "DEST_NAME",
                "GOODS_INFO", "PIECES", "WEIGHT", "SLAC"}
    for k, v in air.items():
        col = _COLUMN_REMAP.get(k, k)
        if col in ("GOODS_HS_CODE", "SEND_STATUS", "CREATE_TIME", "HMY_ID", "HAWB_ID"):
            continue   # 分单表没有的列 / 服务端自己维护的列
        if col in _J9_COLS or col.startswith(("SHIPPER_INFO_", "CONSIGNEE_INFO_")):
            record[col] = v
    receipt = _post_guarded(store.number_key(mawb, hawb), record, "/api/v1/j9/hawb2", "HAWB_RECORD",
                            config.COMPANY_HAWB_KEY)
    return {"mode": "live", "accepted": receipt["accepted"], "action": receipt["action"],
            "mawb": mawb, "hawb": hawb, "before": existing or {},
            "idempotent": receipt.get("idempotent") is True}
