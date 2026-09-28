# -*- coding: utf-8 -*-
"""主单（MAWB）字段面：公司 `AMS_RECORD` 的 36 列，也就是 `POST /api/v1/j9/mawb2/` 的提交体。

2026-09-28 用户定案「主单是主单，分单是分单」：这里一律用**公司列名原样**，不套我方分单
40 字段契约——通知人国家那列公司写成 `NOTIFYE_INFO_COUNTRY`（多个 E）是库表原样，收货人城市
是单 T 的 `CONSIGNEE_INFO_CITY`（双 T `CITTY` 只在分单表）。主单表没有货名列、没有税号列、
没有件重/航路/签发日期，所以外层 7 字段（含 GOODS_NAME/GOODS_DESC）只能当只读参考。
"""
import re

# 三组当事人各 11 列，写法一致（通知人的国家列名例外）
_PARTY_SUFFIXES = (("COMP_NAME", "公司名"), ("COMP_ADDRESS", "地址"), ("CITY", "城市"),
                   ("COUNTRY", "国家"), ("STATE", "州省"), ("POSTAL", "邮编"),
                   ("TEL", "电话"), ("FAX", "传真"), ("EORI", "EORI"),
                   ("AEO", "AEO"), ("EMAIL", "邮箱"))


def _party(prefix: str, who: str, group: str, country_key: str | None = None) -> list:
    out = []
    for suf, lab in _PARTY_SUFFIXES:
        key = country_key if (suf == "COUNTRY" and country_key) else prefix + suf
        out.append((key, f"{who} · {lab}", group))
    return out


# 分组照分单那套给人看的排版（2026-09-28 用户定案）：基础信息在最前、收货人最后。
# 通知人是主单比 分单 多出来的一块，不单列一段，并进基础信息；列名与中文标签照旧。
MASTER_FIELDS = (
    [("MAWB_NO", "主单号", "base"), ("GOODS_INFO_HSCODE", "海关编码 HS", "base"),
     ("SLAC", "SLAC 计数", "base")]
    + _party("NOTIFY_INFO_", "通知人", "base", country_key="NOTIFYE_INFO_COUNTRY")
    + _party("SHIPPER_INFO_", "发货人", "shipper")
    + _party("CONSIGNEE_INFO_", "收货人", "consignee")
)

MASTER_GROUP_LABELS = {"base": "基础信息", "shipper": "发货人 SHIPPER", "consignee": "收货人 CONSIGNEE"}

# 主单比 分单 多出来、但**不参与提交**的列：只在基础信息里只读展示，不进 L1、不进提交体
MASTER_META_COLS = [("JOB_ID", "公司任务号（只读，不提交）", "base")]

MASTER_COLS = [c for c, _l, _g in MASTER_FIELDS]

# 读接口给的外层资料块：主单表没有对应列，只供人工对着看，绝不进提交体
OUTER_REF_COLS = ("JOB_ID", "MASTER_NO", "SHIPPER_INFO", "CONSIGNEE_INFO",
                  "NOTIFY_INFO", "GOODS_NAME", "GOODS_DESC")
# 服务端维护：提交时不发，读了也只当元信息
AMS_SERVER_COLS = ("HMY_ID", "CREATE_TIME", "SEND_STATUS")
# 公司文档写死的限长 50 列：超长直接 400，不写库
MASTER_LIMIT50 = ("SHIPPER_INFO_COUNTRY", "SHIPPER_INFO_STATE", "SHIPPER_INFO_POSTAL",
                  "CONSIGNEE_INFO_CITY", "CONSIGNEE_INFO_COUNTRY", "CONSIGNEE_INFO_STATE",
                  "NOTIFY_INFO_CITY", "NOTIFYE_INFO_COUNTRY", "NOTIFY_INFO_STATE")

_WS = re.compile(r"\s+")


def _txt(v) -> str:
    return _WS.sub(" ", str(v if v is not None else "")).strip()


def norm_mawb_hyphen(v) -> str:
    """凑成公司硬性要求的 3位-横杠-8位；位数不对的原样留着，交给红旗报，绝不硬凑成 11 位。"""
    digits = re.sub(r"\D", "", str(v or ""))
    if len(digits) == 11:
        return digits[:3] + "-" + digits[3:]
    return _txt(v)


def build_transcript(mawb_order: dict) -> dict:
    """公司主单资料 → L1 逐字文本（与分单同一套 {lines, full_text} 形状，保真回查才能复用）。

    已录入的 `AMS_RECORD` 列排在前面、拼接资料块排在后面：模型先看到结构化真值，
    才不会被"地址和电话黏在一行"的文本带偏。资料一律原样出行——切黏行是模型+清洗的活，
    在这里切等于让保真回查自己查自己。"""
    mo = mawb_order or {}
    lines: list[str] = []

    def add(label: str, value):
        """值为空（含 None/纯空白）就不出行——`f"{k}: {mo.get(k)}"` 会把 None 印成文本，
        那会让"公司压根没给这条主单"看起来像"给了但都是空"，白调一次模型。"""
        text = _txt(value)
        if text:
            lines.append(f"{label}: {text}")

    add("MASTER_NO", mo.get("MASTER_NO"))
    ams = mo.get("AMS_RECORD") if isinstance(mo.get("AMS_RECORD"), dict) else {}
    for k, v in ams.items():
        if k in AMS_SERVER_COLS:
            continue
        add(k, v)
    for k in OUTER_REF_COLS[2:]:
        add(k, mo.get(k))
    return {"lines": [{"i": i + 1, "text": t} for i, t in enumerate(lines)],
            "full_text": "\n".join(lines)}


def _int_or_none(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _hs_only(v) -> str:
    """HS 列只留号码：模型常把「HS CODE:8526109 HS CODE:8412210」整串抄进来（逐字抄的，
    保真回查还查不出来），公司要的是号码本身，多个用逗号连。一个数字都没有就原样留着，
    交给红旗报——硬猜成空等于把问题藏起来。"""
    runs = re.findall(r"\d{4,}", str(v or ""))
    return ",".join(runs) if runs else _txt(v)


def clean_ams(ams: dict) -> dict:
    """L2 原文口径 → 可提交口径：只动格式不动内容（与分单 to_air 同一条铁律）。

    只清洗调用方**给到**的列（不在主单 36 列里的键一律丢掉）：mawb2 是整表写回，
    "这次没碰"必须是缺键（由 company_api 用库里的现值补齐），"人工清空"才是显式 null。
    一视同仁地补齐 36 列会把前者误判成后者，等于用一次提交把公司库里没动的列抹成 NULL。"""
    src = ams or {}
    out: dict = {}
    for col in (c for c in MASTER_COLS if c in src):
        v = src.get(col)
        if v is None or (isinstance(v, str) and not v.strip()):
            out[col] = None
        elif col == "MAWB_NO":
            out[col] = norm_mawb_hyphen(v)
        elif col == "SLAC":
            out[col] = _int_or_none(v)
        elif col == "GOODS_INFO_HSCODE":
            out[col] = _hs_only(v)
        elif col.endswith(("_EORI", "_AEO")):
            out[col] = _txt(v).replace(" ", "").upper()
        elif col.endswith("_COUNTRY"):
            out[col] = _txt(v).upper()
        else:
            out[col] = _txt(v)
    return out


def submit_body(ams: dict) -> dict:
    """提交体 = 恰好那 36 列，多一列都没有（服务端三列不发，外层资料块不发）。"""
    src = ams or {}
    return {col: src.get(col) for col in MASTER_COLS}
