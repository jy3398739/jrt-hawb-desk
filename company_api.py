# -*- coding: utf-8 -*-
"""公司系统接口适配层（主分单审核台）。

口径（2026-09-23 定案）：主单/分单的业务数据 JSON 以公司系统为唯一源，录入员按主单号取回
"主单 + 该主单下全部分单"。但公司接口契约待 IT 给，这里**只做 mock、绝不外发任何请求**：
- search_mawb：按主单号返回本机"已提交集"里该主单下的分单（store.submitted_by_mawb）。
  这是我们真实拥有、且不依赖公司 schema 的那部分数据（哪些分单挂在这个主单号下、原件在
  本机 ARCHIVE 的哪个 stem）；主单本身的业务字段留空壳，等真接口再填。

真接口到位后：把 config.COMPANY_API_MODE 设为 live、配好 COMPANY_API_URL 与密钥环境变量，
在本文件里补上真正的 HTTP 调用（读密钥、POST/GET 出去、把返回转成同样的返回结构），上层
/company/mawb 与各角色守卫都不用动。search_mawb 与 submit_order 的返回结构就是这里的对外契约。
"""
import config
import store


class CompanyNotConfigured(RuntimeError):
    """COMPANY_API_MODE=live 但还没接线/缺配置时抛出，避免在没契约的情况下静默返回假数据。"""


def search_mawb(mawb: str) -> dict:
    """按主单号检索该主单及其名下分单。mock：主单业务字段是空壳，分单来自本机提交台账。"""
    mawb = str(mawb or "").strip()
    if config.COMPANY_API_MODE != "mock":
        raise CompanyNotConfigured(
            f"COMPANY_API_MODE={config.COMPANY_API_MODE!r}：公司接口契约待 IT，暂不能真连；"
            "请保持 mock，或在 company_api.py 补上真实现与配置。")
    orders = [{"hawb": e.get("hawb", ""), "mawb": e.get("mawb", ""),
               "stem": e.get("stem"), "reviewer": e.get("reviewer", ""),
               "submitted_at": e.get("submitted_at", "")}
              for e in store.submitted_by_mawb(mawb)]
    # 主单原件：公司接口待 IT，本机按归一化主单号认 output/mawb_source/<主单号>/（人工放或日后缓存）
    return {"mode": "mock", "mawb": mawb, "mawb_order": {}, "hawb_orders": orders,
            "source_available": store.mawb_source_dir(mawb) is not None}


def submit_order(payload: dict) -> dict:
    """把一张核对/修改后的单子回传公司。mock：只回一个带 mode 的回执，不外发。
    与 server._company_submit 同源——提交那一路真接口到位后也收敛到这里。"""
    if config.COMPANY_API_MODE != "mock":
        raise CompanyNotConfigured(
            f"COMPANY_API_MODE={config.COMPANY_API_MODE!r}：公司接口契约待 IT，暂不能真连。")
    return {"mode": "mock", "accepted": True,
            "mawb": (payload or {}).get("mawb", ""), "hawb": (payload or {}).get("hawb", "")}
