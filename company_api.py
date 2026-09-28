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
import json
import re
import urllib.error
import urllib.request

import config
import master_fields as mf
import store

# 读接口的 SHIPPER_INFO/CONSIGNEE_INFO 是 Python repr 的列表串，展示/合并前归一。
_LIST_REPR_RE = re.compile(r"^\[.*\]$", re.S)

# 我方字段名 → 公司 j9 列名。分单表没有 GOODS_HS_CODE（那是主单 AMS_RECORD 的 GOODS_INFO_HSCODE）。
_COLUMN_REMAP = {"ORIGIN_NAME": "ORGIN_NAME", "CONSIGNEE_INFO_CITTY": "CONSIGNEE_INFO_CITY"}


class CompanyNotConfigured(RuntimeError):
    """COMPANY_API_MODE=live 但没接线/缺配置时抛出，避免静默返回假数据。"""


class CompanyApiError(RuntimeError):
    """live 调用失败：HTTP 非 200 或 j9 返回 code!=0，带 j9 的 detail 原文。"""


class CompanyLocked(RuntimeError):
    """SEND_STATUS 闸门拦下：那行已发送/锁定（仅 0/2 可更新），绝不能发 hawb2。"""


def _j9_post(path: str, body: dict, key: str) -> dict:
    """调 j9 接口。path 形如 `/api/v1/j9/hawb`。HTTP 非 200 或 code!=0 都抛 CompanyApiError。"""
    url = config.COMPANY_API_URL + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            pass
        raise CompanyApiError(f"j9 {path} HTTP {e.code}: {detail or e.reason}") from None
    except urllib.error.URLError as e:
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
                   "submitted_at": e.get("submitted_at", "")}
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
    for row in rows:
        stem = store.lookup_stem(mawb, str(row.get("HAWB_NO", "")))
        orders.append({"hawb": str(row.get("HAWB_NO", "")), "mawb": str(row.get("MAWB_NO", "")),
                       "stem": stem, "reviewer": "—", "submitted_at": "—",
                       "send_status": row.get("SEND_STATUS"), "row": row})
    return {"mode": "live", "mawb": mawb, "mawb_order": mawb_order, "hawb_orders": orders,
            "source_available": store.mawb_source_dir(mawb) is not None}


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
    existing = (master[0].get("AMS_RECORD") if master else None) or {}
    if existing and existing.get("SEND_STATUS") not in (0, 2, None):
        raise CompanyLocked(
            f"{mawb} 不允许更新（j9 侧 SEND_STATUS={existing.get('SEND_STATUS')}，仅 0/2 可改，已发送锁定）")
    record = {col: existing.get(col) for col in mf.MASTER_COLS}
    for col in mf.MASTER_COLS:
        if col in ams:
            record[col] = ams[col]
    record["MAWB_NO"] = mf.norm_mawb_hyphen(mawb)
    res = _j9_post("/api/v1/j9/mawb2/", {"AMS_RECORD": record}, config.COMPANY_MAWB_KEY)
    return {"mode": "live", "accepted": bool(res.get("success")), "action": res.get("action"),
            "mawb": mawb, "before": existing}


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
    res = _j9_post("/api/v1/j9/hawb2", {"HAWB_RECORD": record}, config.COMPANY_HAWB_KEY)
    return {"mode": "live", "accepted": bool(res.get("success")),
            "action": res.get("action"), "mawb": mawb, "hawb": hawb}
