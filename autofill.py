# -*- coding: utf-8 -*-
"""主单兜底补号（2026-11-14 业主定案，方案 A）。

分单这票的 HS / 发货人与收货人的 EORI、税号格**缺**（空）而主单那头有 → 自动从主单按
同主体补进来：发货人缺的从主单发货人抓、收货人缺的从主单收货人抓，HS 按新口径截前 6 位。
**分单自己有号就绝不碰**（业主原话："分单有号就要填分单的号，只有分单没有号的时候才能
从主单抓"）。

时机与留痕：
- 解析 job 收尾的后台线程里做（长活不占解析返回）；主单不在本机时去公司检索并解析一遍
  （检索即解析），公司接口不通就安静放弃——不挂旗不报错，提交门的主单先行门自会拦。
- 补进了什么记进 output/auto_fill.json（哪格、原值、补入值、来自哪条主单），旗面写进
  质检记录：「XX 自主单 999-… 补入：…」，人工核对确认（软闸门，可确认可改格）。
- 合并只填**还空着**的格：人在核对区补过号、或重检后分单自己解出了号，主单那份不再覆盖；
  人把格子改成了别的值 = 他自己拿的主意，旗随之消失、值按人写的发。
"""
import json
import os
import re
from pathlib import Path

import atomic
import codes
import config

# 可补的格：HS 一列 + 收发货人各两列（识别号两列拆分后的口径；通知人不补——主单有通知人、
# 分单没有这一格）
COLS = ("GOODS_HS_CODE", "SHIPPER_INFO_EORI", "SHIPPER_INFO_TAX",
        "CONSIGNEE_INFO_EORI", "CONSIGNEE_INFO_TAX")
FILE = Path(os.getenv("AUTO_FILL_FILE", str(config.BASE_DIR / "output" / "auto_fill.json")))


def _flag(col: str, mawb: str, val: str) -> str:
    return (f"{col} 自主单 {mawb} 补入：{val}（分单这格原本是空的；核对无误就点红旗确认，"
            "不对就直接改格子——改了值旗会随重算消失）")


def plan(air: dict, master_ams: dict, mawb: str) -> dict:
    """按"分单缺、主单有"逐格配对，返回 {列: {"from","to","src"}}。主单税号列本来就只装
    税号、EORI 列只装海关号（两列拆分），直接整格搬；HS 截前 6 位——注意两条链的 HS 列名
    不是同一个：主单叫 GOODS_INFO_HSCODE、分单叫 GOODS_HS_CODE（"主单是主单，分单是分单"）。"""
    fills: dict = {}
    for col in COLS:
        if col == "GOODS_HS_CODE":
            have = str(air.get(col) or "").strip()
            want = str(master_ams.get("GOODS_INFO_HSCODE") or "").strip()
            if not have and want:
                fills[col] = {"from": "", "to": codes.hs6(want), "src": mawb}
            continue
        if str(air.get(col) or "").strip():
            continue                       # 分单自己有号：绝不碰（业主定案）
        v = str(master_ams.get(col) or "").strip()
        if v:
            fills[col] = {"from": "", "to": v, "src": mawb}
    return fills


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record(stem: str, fills: dict):
    """补号台账按票合并落盘（重复解析同一票以最新一份为准）。"""
    if not fills:
        return
    led = _load()
    led[str(stem)] = dict(fills)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(FILE, led)


def load(stem: str) -> dict:
    return _load().get(str(stem)) or {}


def merge(stem: str, air: dict) -> dict:
    """把台账里的补入值并进这份 air——**只填还空着的格**：人有值就按人的。"""
    out = dict(air or {})
    for col, f in load(stem).items():
        if col in COLS and not str(out.get(col) or "").strip() and f.get("to"):
            out[col] = f["to"]
    return out


def flags_for(stem: str, air: dict) -> list:
    """还没被人拿主意的补入旗：格子仍空（合并还没发生/又被清了）或仍等于补入值时出声；
    人改成了别的值 = 他自己拿的主意，不再催。提交门与 recompute 同一条文案。"""
    out = []
    for col, f in load(stem).items():
        if col not in COLS:
            continue
        cur = str((air or {}).get(col) or "").strip()
        if not cur or cur == str(f.get("to") or "").strip():
            out.append(_flag(col, str(f.get("src") or ""), str(f.get("to") or "")))
    return out


def quick_fill(stem: str, air: dict, read_master):
    """主单已在本机解析过时的快速补号（毫秒级，跟解析同步做，前端第一眼就看到补进来的值）。
    主单不在本机的不走这条路——那是后台线程 + 公司检索 + 解析主单的活（background_fill）。
    只写台账与质检旗，不动盘上的 air（L3 是 L2 的纯函数重算，重检不许被外部值污染）。"""
    mawb = str((air or {}).get("MAWB_NO") or "").strip()
    if not re.fullmatch(r"\d{3}-\d{8}", mawb):
        return
    if not [c for c in COLS if not str((air or {}).get(c) or "").strip()]:
        return                          # 分单五格都有号：没什么可补的
    rec = read_master(mawb)
    if not (rec and rec.get("state") == "done" and rec.get("ams")):
        return
    fills = plan(air or {}, rec["ams"], mawb)
    if not fills:
        return
    record(stem, fills)
    import eori_check                   # append_qc_flags 是通用的"旗并进质检记录"
    eori_check.append_qc_flags(str(stem), [_flag(c, f["src"], f["to"]) for c, f in fills.items()])


def background_fill(stem: str, air: dict, read_master, ensure_master):
    """解析收尾的后台线程：主单不在本机就检索并解析（检索即解析），失败安静放弃。
    read_master/ensure_master 由调用方传（master_pipeline），本模块不倒着 import 它。"""
    mawb = str((air or {}).get("MAWB_NO") or "").strip()
    if not re.fullmatch(r"\d{3}-\d{8}", mawb):
        return
    if not [c for c in COLS if not str((air or {}).get(c) or "").strip()]:
        return                          # 分单五格都有号：没什么可补的
    rec = read_master(mawb)
    if not (rec and rec.get("state") == "done" and rec.get("ams")):
        try:
            rec = ensure_master(mawb)
        except Exception:
            return                      # 公司接口不通/资料缺：安静放弃，提交门自会拦
    ams = (rec or {}).get("ams") or {}
    fills = plan(air or {}, ams, mawb)
    if not fills:
        return
    record(stem, fills)
    import eori_check                   # append_qc_flags 是通用的"旗并进质检记录"
    eori_check.append_qc_flags(str(stem), [_flag(c, f["src"], f["to"]) for c, f in fills.items()])
