# -*- coding: utf-8 -*-
"""公司系统接口适配层（主分单审核台）。

口径（2026-10-09 IT 文档改版）：j9 AMS 录入接口六個——主单/分单各三，一读两写（查询/暂存/提交发送），
全部 POST+JSON，请求头 X-Api-Key（主单组与分单组两把 key 互不通用）。MODE=live 时真连，mock 保持原行为：
- search_mawb：live 走 `j9/hawb` 按主单号拉分单行 + `j9/mawb/` 拉主单原生资料（含 AMS_RECORD），
  再用本机提交台账把已归档原件的 stem 联结进去（前端 /source 打开原件靠它）。
- submit_master / submit_order：**提交发送走 `j9/mawb3/` 与 `j9/hawb3`**，先读后写
  （SEND_STATUS 闸门 + 整表写回补齐未改列），落库状态 1=已提交发送，等外围程序取数。
  2026-10-09 之前这两个函数打的是 2——那一档在新文档里是**暂存**（状态 0，外围不取），
  所以我们记着"提交成功"的票在公司侧其实一条都没发出去。

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
import fieldspec
import master_fields as mf
import retry
import store

# 读接口的 SHIPPER_INFO/CONSIGNEE_INFO 是 Python repr 的列表串，展示/合并前归一。
_LIST_REPR_RE = re.compile(r"^\[.*\]$", re.S)

# 我方字段名 → 公司 j9 列名。分单表没有 GOODS_HS_CODE（那是主单 AMS_RECORD 的 GOODS_INFO_HSCODE）。
_COLUMN_REMAP = {"ORIGIN_NAME": "ORGIN_NAME", "CONSIGNEE_INFO_CITTY": "CONSIGNEE_INFO_CITY"}

# 提交体该有哪些列（IT 2026-10-09：全部字段都要带上，没值的空着——缺键与"本来就是空"必须分得开）。
# 服务端自己维护的三列与它没有的两列（HS 编码、税号）不在其内；税号另有去处，见 merge_ids。
_HAWB_SKIP = ("SEND_STATUS", "CREATE_TIME", "GOODS_HS_CODE")
HAWB_SUBMIT_COLS = tuple(_COLUMN_REMAP.get(k, k) for k, _lab, _g in fieldspec.DESK_FIELDS
                         if k not in _HAWB_SKIP and not k.endswith("_TAX_ID"))

# 空数字格必须发 null 而不是 ""：公司那边 `PIECES 必须为整数` 会把整条退回。
_HAWB_NUM = tuple(k for k in ("PIECES", "WEIGHT", "SLAC"))

# 拼串的分隔符必须带空格：CNPJ 自己就印成 07.454.234/0001-10，裸斜杠会把一个号码拆成两段。
_ID_SPLIT = re.compile(r"\s+/\s+")


def merge_ids(*vals):
    """公司列面没有税号格，IT 定案：税号统一进 EORI，同主体多个号拼在一格（" / "）。

    先拆再拼：暂存能反复写，下一次读回来的 EORI 就已经是拼好的那串——不拆的话
    每暂存一次就多挂一段，第三次变成 "A / B / B / B"。"""
    out = []
    for v in vals:
        for part in _ID_SPLIT.split(str(v or "").strip()):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return " / ".join(out)

# 幂等锁的有效期：j9 没有幂等键，重复提交=再来一次整表写回，所以同内容只认这几十秒内的重复点。
GUARD_TTL = 90


class CompanyNotConfigured(RuntimeError):
    """COMPANY_API_MODE=live 但没接线/缺配置时抛出，避免静默返回假数据。"""


class CompanyApiError(RuntimeError):
    """live 调用失败：HTTP 非 200 或 j9 返回 code!=0，带 j9 的 detail 原文。"""


class CompanyPreReadFailed(CompanyApiError):
    """**该有行却读不到**：本机台账证明我们给它录过一次，这次读回空——只能是接口抖动/查询没对上。
    此时继续写会要命：j9 两个写口（2 暂存 / 3 提交发送）都是整表写回，没带的列一律写成 NULL，等于把那一行其余列清空。
    宁可拒绝，让制单员等一次好使的读。"""


class CompanyLocked(RuntimeError):
    """SEND_STATUS 闸门拦下：那一行已经离开暂存档（1 已提交发送 / 2 外围已发送成功），
    公司侧两个写口都会 400「已提交，不能更新」。本地先拦下来，省一次白发与一句看不懂的 400。"""


# 公司侧 SEND_STATUS 字典（2026-10-09 文档）：只有 0 能写。1 是"已提交发送、还没发出去"，
# 2 是"外围程序已成功发送"——从前我们把 2 当"已改待重发"放行，那是去覆盖一条已经发成功的记录。
SEND_STATE = {0: "暂存（还能改）", 1: "已提交发送，等外围程序取数（不能再改）",
              2: "外围已发送成功（锁定，不能再改）"}


def _gate(status, label):
    """闸门只认 0（和"库里根本没这条"）；1/2 一律拒，并把是哪一档说清楚。"""
    if status is None or status == 0:
        return
    raise CompanyLocked(f"{label} 公司侧 SEND_STATUS={status}：{SEND_STATE.get(status, '未知状态')}，"
                        f"只有 0（暂存）允许再写")


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


_PENDING_COLS = ("stem", "mawb", "hawb", "state", "stager", "stager_role", "staged_at",
                 "edited", "reviewed_by_inputter", "has_original")


def local_pending(mawb: str) -> list:
    """本机已解析或已暂存、但还没进提交台账的分单——公司不认识它们，两种模式都要单独回。

    名下分单表读的是公司接口，公司只认提交过的票：只上传/只暂存的票要是不出现在这里，
    录入员查同一张主单就还是"没有分单"（2026-09-29 用户实测过这个，当时的补丁只在浏览器里
    数自己这台机器的票，换个人照样看不见）。名单从 `store.ticket_rows` 现算，
    已经在台账里的不重复列。"""
    return [{k: r.get(k) for k in _PENDING_COLS}
            for r in store.ticket_rows(mawb=mawb) if r["state"] in ("parsed", "staged")]


def search_mawb(mawb: str) -> dict:
    """按主单号检索该主单及其名下分单。"""
    mawb = str(mawb or "").strip()
    if config.COMPANY_API_MODE != "live":
        if config.COMPANY_API_MODE != "mock":
            raise CompanyNotConfigured(
                f"COMPANY_API_MODE={config.COMPANY_API_MODE!r}：只能是 mock 或 live。")
        # stem 一律现查（和 live 分支同一个口径）：台账里那条名字可能已经点不开了。
        orders = [{"hawb": e.get("hawb", ""), "mawb": e.get("mawb", ""),
                   "stem": store.lookup_stem(e.get("mawb", ""), e.get("hawb", "")),
                   "reviewer": e.get("reviewer", ""),
                   "submitted_at": e.get("submitted_at", ""),
                   "submitted_here": True}
                  for e in store.submitted_by_mawb(mawb)]
        return {"mode": "mock", "mawb": mawb, "mawb_order": {}, "hawb_orders": orders,
                "pending": local_pending(mawb),
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
            "pending": local_pending(mawb),
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
    内容变了说明是真要再改一次（公司侧状态 0 允许反复写），不拦。"""
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
    """把一条主单资料**提交发送**给公司（`POST /api/v1/j9/mawb3/`，落库状态 1）。
    mock 只回带 mode 的回执；live 先读后写。

    先读是为了两件事：① 库里这条记录的 SEND_STATUS 不是 0 就说明已提交/已发送、公司会拒改，
    **一个写请求都不发**；② 写口是整表写回，没给的列会被写成 NULL，所以要把读到的列原样
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
    _gate(existing.get("SEND_STATUS") if existing else None, mawb)
    # 先按公司当前值铺满那 36 列（j9 是整表写回：没带上的列会被写成 NULL），再盖掉本次改过的。
    # 列面走 mf.submit_body——"恰好 36 列"这条规则只许有一处。
    record = mf.submit_body(existing)
    for col in mf.MASTER_COLS:
        if col in ams:
            record[col] = ams[col]
    record["MAWB_NO"] = mf.norm_mawb_hyphen(mawb)
    receipt = _post_guarded(store.norm_no(mawb), record, "/api/v1/j9/mawb3/", "AMS_RECORD",
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
    _gate(existing.get("SEND_STATUS") if existing is not None else None, hawb)
    record = dict(existing) if existing else {"MAWB_NO": mawb, "HAWB_NO": hawb}
    record["MAWB_NO"], record["HAWB_NO"] = mawb, hawb
    air = (payload or {}).get("air") or {}
    for k, v in air.items():
        col = _COLUMN_REMAP.get(k, k)
        if col not in HAWB_SUBMIT_COLS:
            continue   # 分单表没有的列（HS 编码、税号）与服务端自己维护的列
        record[col] = v
    # 公司列面没有税号格：税号统一进 EORI，两个号都留、拼在一格（IT 2026-10-09 定案）
    for pfx in ("SHIPPER", "CONSIGNEE"):
        col = pfx + "_INFO_EORI"
        record[col] = merge_ids(record.get(col), air.get(pfx + "_INFO_TAX_ID")) or None
    for col in HAWB_SUBMIT_COLS:
        record.setdefault(col, None)      # 没给值的列也要占位：缺键与"本来就是空"必须分得开
        if col in _HAWB_NUM and record[col] == "":
            record[col] = None            # 空数字格发 null——空串会被公司判「必须为整数」整条退回
    receipt = _post_guarded(store.number_key(mawb, hawb), record, "/api/v1/j9/hawb3", "HAWB_RECORD",
                            config.COMPANY_HAWB_KEY)
    return {"mode": "live", "accepted": receipt["accepted"], "action": receipt["action"],
            "mawb": mawb, "hawb": hawb, "before": existing or {},
            "idempotent": receipt.get("idempotent") is True}
