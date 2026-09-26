# -*- coding: utf-8 -*-
"""V2 地基：CCSP 平台填表计划（dry-run）。

口径（2026-09-26 定案）：V2 = 把核对完的 L3 JSON 落到 CCSP「国际分单录入」网页。
本模块只做**纯逻辑**——从我方字段算出"将要往哪些控件填什么值"，产出可人工核对的
填表计划；**不起浏览器、不发任何请求、不写平台**。真开浏览器是下一个里程碑，
且提交动作永远由人点（.env WEB_SUBMIT=on + 每票人工确认 + 零红旗白名单）。

站点事实（选择器/URL/映射）全部放 sites/ccsp.json，选择器漂移只改配置不改代码。
"""
import json
import re
from pathlib import Path

SITE_FILE = Path(__file__).with_name("sites") / "ccsp.json"


def load_site() -> dict:
    return json.loads(SITE_FILE.read_text(encoding="utf-8"))


def split_mawb(mawb) -> tuple[str, str]:
    """`999-95764373` / `999 95764373` / `99995764373` → (前缀, 8位序号)。拆不开返回空串。"""
    digits = re.sub(r"\D", "", str(mawb or ""))
    if len(digits) == 11:
        return digits[:3], digits[3:]
    return "", ""


def hs6(hs) -> str:
    """GOODS_HS_CODE → 平台 6 位国际口径（`89031200`→`890312`；多码逗号分隔逐个取前 6 位）。"""
    runs = re.findall(r"\d{6,10}", str(hs or ""))
    return ",".join(r[:6] for r in runs)


def norm_tel(tel) -> str:
    """平台样本 `861069479536`：去 + 与一切分隔符。不擅自加国家前缀——票面没印 86 的
    中国号补 86 是业务判断，进 needs_human 不进自动值。"""
    return re.sub(r"\D", "", str(tel or ""))


def city_to_airport(code, table: dict) -> tuple[str, bool]:
    """城市组码 → 主机场码（BJS→PEK、MIL→MXP、YMQ→YUL，8/8 张对格实证平台人工一律机场码）。
    返回 (值, needs_human)。表里没有的码**原样放行**且不报警——未收录的大多是本身就是
    机场的码（VCE/MUC/CPH…），逐条报警只会把人工清单淹掉；表随新证据增补。"""
    c = str(code or "").strip().upper()
    if c in table:
        return table[c], False
    return c, False


# 子字段名 → 平台控件名。注意契约里的历史 typo：收货人城市是 CITTY（双 T），发货人是 CITY。
_SUB = {
    "COMP_NAME": "name", "COMP_ADDRESS": "address", "CITY": "city", "CITTY": "city",
    "STATE": "state", "COUNTRY": "country", "POSTAL": "zipCode", "TEL": "tel",
    "FAX": "fax", "TAX_ID": "customsCode", "AEO": "AEOCode",
}
# 平台库常补、我方常空的控件：为空时进人工核对清单（不算提取缺陷）。
_HUMAN_EMPTY = ("CITY", "CITTY", "POSTAL", "TEL", "TAX_ID")


def _sub_value(air: dict, sub_key: str, transform: str, site: dict, target: str):
    """取我方一个子字段的值并做口径转换，返回 (值, 需人工提示或 None)。"""
    v = str(air.get(sub_key, "") or "").strip()
    if not v:
        # sub_key 传进来的是完整字段名（SHIPPER_INFO_TEL），用后缀比对裸子键
        if sub_key.endswith(_HUMAN_EMPTY):
            return "", f"{sub_key} 为空：平台 {target} 可能要从平台收发货人库带出，人工核对"
        return "", None
    if transform == "plain":
        return v, None
    if transform == "weight":
        return (v.rstrip("0").rstrip(".") if "." in v else v), None
    if transform == "hs6":
        return hs6(v), None
    if transform == "tel":
        return norm_tel(v), None
    if transform == "city_to_airport":
        out, human = city_to_airport(v, site.get("city_to_airport", {}))
        if human:
            return out, f"{sub_key}={v!r} 不在机场码映射表里，平台要机场码：需人工确认 {target}"
        return out, None
    return v, None


def build_fill_plan(air: dict, site: dict | None = None, raw: dict | None = None) -> dict:
    """我方 L3（航空口径）→ CCSP 填表计划。raw 传 L2 原文口径时，城市栏优先用票面原文
    （对格实证：我方 L3 的 city 已三字码化 TAO，平台要城市名 QINGDAO）。返回：
    values        {控件名: 将要填的值}——只含非空且口径转换成功的
    needs_human   [提示串]——空值核对清单 / 机场码表缺项 / 该人工确认的口径
    platform_only [控件名]——平台要、我方契约没有的（默认留空等业务定）
    """
    site = site or load_site()
    values: dict[str, str] = {}
    needs: list[str] = []
    raw = raw or {}

    for field, spec in site["field_map"].items():
        if field == "MAWB_NO":
            pre, no = split_mawb(air.get("MAWB_NO"))
            if pre:
                for control, transform in spec["targets"].items():
                    values[control] = pre if transform == "split_mawb_pre" else no
            else:
                needs.append(f"MAWB_NO 拆不开（要 3 位前缀+8 位序号）：{air.get('MAWB_NO')!r}")
            continue
        if "targets" in spec:
            for target, transform in spec["targets"].items():
                v, msg = _sub_value(air, field, transform, site, target)
                if v:
                    values[target] = v
                if msg:
                    needs.append(msg)
            continue
        prefix, subs = spec["prefix"], spec["sub"]
        for sub_key, sub_name in subs.items():
            full_key = field + "_" + sub_key
            v, msg = _sub_value(air, full_key, "plain", site, prefix + sub_name)
            if sub_key in ("CITY", "CITTY"):
                raw_v = str(raw.get(full_key, "") or "").strip()
                if raw_v:
                    v = raw_v   # 票面原文城市名优先于 L3 三字码；"为空"提示随之作废
                    msg = None
                elif v:
                    msg = (f"{full_key}={v!r} 是我方 L3 的三字码口径，平台 city 栏要城市名"
                           f"（对格实证平台填 BEIJING 这类）：需人工确认 {prefix + sub_name}")
            if sub_key in ("TEL", "FAX"):
                v = norm_tel(v)
            if v:
                values[prefix + sub_name] = v
            if msg:
                needs.append(msg)

    return {"values": values, "needs_human": needs, "platform_only": list(site["platform_only"])}


def format_plan(plan: dict, air: dict) -> str:
    """dry-run 报告（人看的）：填什么、留什么、核对什么。"""
    lines = [f"HAWB {air.get('HAWB_NO','')} @ 主单 {air.get('MAWB_NO','')}",
             f"将填 {len(plan['values'])} 格："]
    for k in sorted(plan["values"]):
        lines.append(f"  {k} = {plan['values'][k]!r}")
    lines.append(f"需人工核对 {len(plan['needs_human'])} 项：")
    for n in plan["needs_human"]:
        lines.append(f"  - {n}")
    lines.append(f"平台独有控件（本版留空）{len(plan['platform_only'])} 个：" +
                 "、".join(x.rsplit('.', 1)[-1] for x in plan["platform_only"]))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    air = d.get("air", d)
    plan = build_fill_plan(air, raw=d.get("raw") or {})
    print(format_plan(plan, air))
