# -*- coding: utf-8 -*-
"""审核台字段面（40 列）唯一的家：键名、中文名、分组、输入类型。

为什么要有这个文件：同一批列此前手写抄了三处——后端 `codes.TARGET_KEYS_OUT`（导出/入库顺序）、
前端 `web/js/desk.js` 的 `FIELDS`（审核台显示顺序与中文标签）、Excel 的 `CN_HEADERS`。
加一列（例如 2026-09 的税号）就得记着改两三处，漏一处就是"接口收得进、界面看不见"，
而 IT 那次改列名（NOTIFYE_INFO_COUNTRY → NOTIFY_INFO_COUNTRY）差点踩的正是这个。

分工（有意保留的不同）：
  EXPORT_ORDER = codes.TARGET_KEYS_OUT      导出/入库的列序（业务系统直读，不能乱动）
  DESK_FIELDS  = 下面的表                   审核台的显示顺序与中文标签（按核对动线分组）
  CN_HEADERS   = export_excel 里那份        导出 Excel 的表头用词与上面两套不同（面向收件人），
                                            只要求键名必须在本表里存在（tests 盯着）
前端那份 `FIELDS` 由 deploy/gen_fields.py 从本文件生成，改完跑一次生成器即可。
"""

GROUP_LABELS = {
    "id": "单号", "route": "航路", "cargo": "货物与日期",
    "ship": "发货人 SHIPPER", "cons": "收货人 CONSIGNEE",
}

# 数字列：int 出现小数要报（件数不该有小数），num 只判是不是数
NUM = {"PIECES": "int", "SLAC": "int", "WEIGHT": "num"}

# 整串/长地址列用文本域（一屏放不下，高度跟着内容走）
LONG = ["SHIPPER_INFO", "CONSIGNEE_INFO", "GOODS_INFO",
        "SHIPPER_INFO_COMP_ADDRESS", "CONSIGNEE_INFO_COMP_ADDRESS"]

# (键名, 审核台中文名, 分组) —— 顺序就是核对时的从上到下，按"先核单号航路、再核货物、再逐方核收发货人"
DESK_FIELDS = [
    ("MAWB_NO", "主单号", "id"), ("HAWB_NO", "分单号", "id"),
    ("ORIGIN_NAME", "起运港", "route"), ("TO1", "航路 1", "route"),
    ("TO2", "航路 2", "route"), ("TO3", "航路 3", "route"),
    ("DEST_NAME", "目的港", "route"), ("CREATE_TIME", "签发日期", "cargo"),
    ("PIECES", "件数", "cargo"), ("WEIGHT", "毛重", "cargo"),
    ("SLAC", "小件数 SLAC", "cargo"), ("GOODS_INFO", "货物描述", "cargo"),
    ("GOODS_HS_CODE", "HS 编码", "cargo"), ("SEND_STATUS", "状态", "cargo"),
    ("SHIPPER_INFO", "发货人整串", "ship"), ("SHIPPER_INFO_COMP_NAME", "公司名称", "ship"),
    ("SHIPPER_INFO_COMP_ADDRESS", "详细地址", "ship"), ("SHIPPER_INFO_CITY", "城市", "ship"),
    ("SHIPPER_INFO_STATE", "州 / 省", "ship"), ("SHIPPER_INFO_POSTAL", "邮编", "ship"),
    ("SHIPPER_INFO_COUNTRY", "国家", "ship"), ("SHIPPER_INFO_TEL", "电话", "ship"),
    ("SHIPPER_INFO_FAX", "传真", "ship"), ("SHIPPER_INFO_EORI", "EORI / 税号", "ship"),
    ("SHIPPER_INFO_AEO", "AEO", "ship"), ("SHIPPER_INFO_EMAIL", "邮箱", "ship"),
    ("CONSIGNEE_INFO", "收货人整串", "cons"), ("CONSIGNEE_INFO_COMP_NAME", "公司名称", "cons"),
    ("CONSIGNEE_INFO_COMP_ADDRESS", "详细地址", "cons"), ("CONSIGNEE_INFO_CITTY", "城市", "cons"),
    ("CONSIGNEE_INFO_STATE", "州 / 省", "cons"), ("CONSIGNEE_INFO_POSTAL", "邮编", "cons"),
    ("CONSIGNEE_INFO_COUNTRY", "国家", "cons"), ("CONSIGNEE_INFO_TEL", "电话", "cons"),
    ("CONSIGNEE_INFO_FAX", "传真", "cons"), ("CONSIGNEE_INFO_EORI", "EORI / 税号", "cons"),
    ("CONSIGNEE_INFO_AEO", "AEO", "cons"), ("CONSIGNEE_INFO_EMAIL", "邮箱", "cons"),
]

HEAD = """/* 由 fieldspec.py 生成，改字段只改那一处，然后跑：python deploy/gen_fields.py
   ——tests/test_fieldspec.py 会盯着有没有忘记重生成，别让这里长出一份手写副本。 */
"""


def desk_keys():
    return [k for k, _lab, _g in DESK_FIELDS]


def render_js():
    """生成 desk.js 里那段常量（显示顺序/中文标签/分组/数字与长文本标记）。"""
    rows = []
    for k, lab, g in DESK_FIELDS:
        rows.append('  ["%s","%s","%s"]' % (k, lab, g))
    chunks = [",\n".join(rows[i:i + 2]) for i in range(0, len(rows), 2)]
    fields = ",\n".join(chunks)
    num = ", ".join('%s:"%s"' % (k, v) for k, v in NUM.items())
    long_ = ",".join('"%s"' % k for k in LONG)
    groups = ",".join('%s:"%s"' % (g, lab) for g, lab in GROUP_LABELS.items())
    return (HEAD + "const FIELDS = [\n" + fields + "\n];\n"
            + "const KEYS = FIELDS.map(f => f[0]);\n"
            + "const GROUPS = {%s};\n" % groups
            + "const NUM = {%s};\n" % num
            + "const LONG = new Set([%s]);" % long_)
