# -*- coding: utf-8 -*-
"""税号（USCI/统一社会信用代码、CNPJ、VAT NO、TAX ID）专项回归。

历史：37 字段时代票面没有税号落点，模型按规则既不放 TEL 也不放 EORI，号就直接丢了
（DRAFT - HAWB No_ TSN10359645 发货人印着 USCI: 911201117706402073 是那张起点票），
所以当年加了 `*_INFO_TAX_ID` 两列，并配了"漏号必须反向查"的红旗。

2026-10-10 业主定案：**这两列删掉，税号一律填进同主体的 `*_INFO_EORI`**（公司 AMS 列面
本来就没有税号格，v1.8.0 起提交前已经是"两个号用 ' / ' 拼进 EORI 一格"）。
这一档改动之后，EORI 那一格会同时装欧码海关号与本土税号，所以下面钉死三件事：
① 号不丢（正向照抄 + 反向漏号红旗都要指向 EORI）；
② 不重复（同格去重、反复暂存不越拼越长）；
③ 老记录不静默掉号（air 里还留着 TAX_ID 的历史票，提交时照样并进去）。
"""
import json
import re
from pathlib import Path

import codes
import company_api
import fieldspec
import to_air
import web_src
import validator
from fidelity import find_missing_tax, verify_fidelity

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity"
USCI = "911201117706402073"


def vlm_fields():
    import vlm_extract
    return vlm_extract.FIELDS


def desk_keys():
    html = web_src.desk()
    body = re.search(r"const FIELDS = \[(.*?)\n\];", html, re.S).group(1)
    return re.findall(r'\["([A-Z][A-Z0-9_]+)"', body)


def l2(**over):
    """一张干净的 L2 骨架（所有列都存在但为空），只覆盖传进来的那几格。"""
    d = {k: "" for k in codes.TARGET_KEYS_OUT}
    d.update(over)
    return d


def test_field_contract_is_40_with_tax_columns():
    """字段清单只有一个真值源：后端契约、prompt 要抽的字段、前端表格行三处必须同集合。
    2026-11-14 IT 加回税号列（新名 `*_INFO_TAX`，36→39 主单 / 38→40 分单）：EORI 只放海关号，
    USCI/CNPJ/VAT 这类税号放同主体 `_TAX`；旧名 `*_INFO_TAX_ID` 这一家三口仍不得复活。"""
    assert len(codes.TARGET_KEYS_OUT) == 40 and len(set(codes.TARGET_KEYS_OUT)) == 40
    assert set(codes.TARGET_KEYS_OUT) == set(vlm_fields()), \
        "prompt 的字段清单与后端契约不一致，模型抽出来的键会对不上列"
    assert set(codes.TARGET_KEYS_OUT) == set(desk_keys()), \
        "审核台字段行与后端契约不一致，制单员看不到就是没审核"
    assert not [k for k in codes.TARGET_KEYS_OUT if k.endswith("_TAX_ID")], "旧税号列没删干净"
    assert not [k for k in fieldspec.desk_keys() if k.endswith("_TAX_ID")], "fieldspec 里还剩旧税号列"
    for pfx in ("SHIPPER_INFO_", "CONSIGNEE_INFO_"):
        assert pfx + "TAX" in codes.TARGET_KEYS_OUT, "新税号列缺失"
    assert "GOODS_HS_CODE" in codes.TARGET_KEYS_OUT, "HS 码这列不在这次改动里"


def test_prompt_sends_tax_into_the_eori_cell():
    """提示词必须说清"税号填进 EORI 那一格"，否则模型照旧无处安放、干脆丢掉。"""
    import vlm_extract
    blob = vlm_extract.PROMPT_HEAD
    assert "TAX_ID" not in blob, "提示词还在要求一个已经删掉的字段"
    assert "EORI" in blob and "USCI" in blob and "CNPJ" in blob, "税号标签表还在，但要落到 EORI 上"


def test_l3_folds_a_tax_pulled_out_of_the_address_into_tax():
    """模型把 USCI/CNPJ 留在地址里：L3 摘干净地址，号进同主体的 TAX 格（2026-11-14 两列拆分，
    EORI 只放海关号）。发出格式不变：标签词跟着号一起走，连写在前面（`USCI9112…`）。"""
    l2d = l2(MAWB_NO="999-30825351", HAWB_NO="TSN10359645",
             SHIPPER_INFO_COMP_NAME="PARKER HANNIFIN HYDRAULICS (TIANJIN) CO LTD",
             SHIPPER_INFO_COMP_ADDRESS="NO 21 HONGYUAN ROAD, TIANJIN 300385 CN, "
                                       "TE +862258388999 USCI: " + USCI,
             SHIPPER_INFO_CITY="TIANJIN", SHIPPER_INFO_COUNTRY="CN",
             SHIPPER_INFO_POSTAL="300385", SHIPPER_INFO_TEL="+862258388999 USCI: " + USCI)
    l3 = to_air.to_air(l2d)
    assert l3["SHIPPER_INFO_TEL"] == "862258388999", l3["SHIPPER_INFO_TEL"]
    assert l3["SHIPPER_INFO_TAX"] == "USCI" + USCI, "税号要落在 TAX 格，不是消失"
    assert l3["SHIPPER_INFO_EORI"] == "", "EORI 格只认海关号，不放税号"
    assert l3["SHIPPER_INFO_CITY"] == "BEIJING", "发货人城市无条件固定（2026-10-27 定案）"
    assert USCI not in l3["SHIPPER_INFO_COMP_ADDRESS"], l3["SHIPPER_INFO_COMP_ADDRESS"]
    # 这份 L2 没给整串，出口就得是空的——以前这里是"重拼后的串里不许再挂着没归位的号"，
    # 现在整串不拼了，号在照抄串里出现本来就是票面印的，该管的只剩地址那一格。
    assert l3["SHIPPER_INFO"] == "", l3["SHIPPER_INFO"]


def test_l3_keeps_the_eori_cell_clean_when_the_same_number_is_in_the_address():
    """欧票常见一格两个号：海关 EORI + 本土 VAT/USCI。两个都要留——2026-11-14 起两列拆开：
    EORI 号留 EORI 格，税号进 TAX 格。同一格内同一个号不许写两遍（地址里再出现只清列）。"""
    l2d = l2(HAWB_NO="BJS00032109",
             CONSIGNEE_INFO_COMP_NAME="CALZONI HYDRAULICS ITALY SRL",
             CONSIGNEE_INFO_COMP_ADDRESS="VIA CASTELDEBOLE 10, 40069 IT",
             CONSIGNEE_INFO_CITTY="ZOLA PREDOSA", CONSIGNEE_INFO_COUNTRY="IT",
             CONSIGNEE_INFO_EORI="IT03268900267",
             CONSIGNEE_INFO_TEL="+390422470376")
    l3 = to_air.to_air(l2d)
    assert l3["CONSIGNEE_INFO_EORI"] == "IT03268900267", "票面没第二个号就别硬拼"
    l2d["CONSIGNEE_INFO_COMP_ADDRESS"] = "VIA CASTELDEBOLE 10, VAT IT03268900267, 40069 IT"
    l3 = to_air.to_air(l2d)
    assert l3["CONSIGNEE_INFO_EORI"] == "IT03268900267", "同一个号不该在同一格里写两遍"
    assert l3["CONSIGNEE_INFO_TAX"] == "", "VAT 标签但号码本体是海关号：归 EORI 不归 TAX"


def test_the_label_stays_out_of_the_comparators_and_in_front_of_the_number():
    """2026-10-23 业主定案：发出去的那一格是「票面标签词 + 连写号码」，中间不留分隔符，
    号码本体的点/斜杠/横线也去掉。

    但 `clean_tax` 保持原义（摘掉标签只留号码）——保真回查（`fidelity.find_missing_tax`）和主单
    那条反向红旗都拿它把标签与号码分开比对，改它等于同时把两处判据搅浑。所以留标签是新函数
    `id_cell`，只用在三处产出发出值的地方；这一条用例钉的就是"两个函数不许并成一个"。"""
    assert codes.clean_tax("VAT#769638661") == "769638661", "比对用的那一半不许跟着口径变"
    assert codes.clean_tax("TAX ID 123456789") == "123456789"
    assert codes.clean_tax("PL7010468168") == "PL7010468168", "两位国家码开头的是号码本身"
    assert codes.clean_tax("00.280.273/0001-37") == "00.280.273/0001-37"
    for src, want in (
        ("CNPJ: 00.280.273/0001-37", "CNPJ00280273000137"),
        ("USCI 911201117706402073", "USCI911201117706402073"),
        ("统一社会信用代码：911201165751476360", "USCI911201165751476360"),
        ("VAT NO. 769638661", "VATNO769638661"),
        ("TAX ID 123456789", "TAXID123456789"),
        ("GST IN 07AAACP1234C1Z5", "GSTIN07AAACP1234C1Z5"),
        ("VAT#769638661", "VAT769638661"),
        ("SE5563646560", "SE5563646560"),
        ("IT 03268900267", "IT03268900267"),
        ("00280273000137", "00280273000137"),
        ("VAT NO", ""),
        ("", ""),
    ):
        got = codes.id_cell(src)
        assert got == want, f"id_cell({src!r}) -> {got!r}，期望 {want!r}"


def test_l3_files_the_label_the_model_copied_into_the_cell():
    """票面把 'VAT#769638661' 整格印出来，模型连着标签抄进 `_EORI`：标签词留着，冒号井号去掉。
    2026-11-14 两列拆分：号码本体不带两位国家码（9 位纯数字 VAT）→ 落 TAX 格，EORI 留空。"""
    l2d = l2(HAWB_NO="TAO-7268550",
             CONSIGNEE_INFO_COMP_NAME="BIG METAL LTD",
             CONSIGNEE_INFO_COMP_ADDRESS="OCCUPATIONAL ROAD 1, LONDON SE17 3BE GB",
             CONSIGNEE_INFO_CITTY="LONDON", CONSIGNEE_INFO_COUNTRY="GB",
             CONSIGNEE_INFO_EORI="VAT#769638661")
    out = to_air.to_air(l2d)
    assert out["CONSIGNEE_INFO_EORI"] == ""
    assert out["CONSIGNEE_INFO_TAX"] == "VAT769638661"


def test_the_same_number_in_two_label_spellings_is_still_one_number():
    """税号格里模型写 'VAT#769638661'，页底 customs 区那格印 'VAT NO. 769638661'：同一个号、
    两种标签写法。去重必须按**号码本体**比——按整串比会拼成 'VAT769638661 / VATNO769638661'，
    公司那边看着像两个号。这一条是变异测试逼出来的：发出值带标签之后，老的去重比法就漏了。"""
    l2d = l2(HAWB_NO="TAO-7268551",
             CONSIGNEE_INFO_COMP_NAME="BIG METAL LTD",
             CONSIGNEE_INFO_COMP_ADDRESS="OCCUPATIONAL ROAD 1, VAT NO. 769638661, LONDON SE17 3BE GB",
             CONSIGNEE_INFO_CITTY="LONDON", CONSIGNEE_INFO_COUNTRY="GB",
             CONSIGNEE_INFO_TAX="VAT#769638661")
    air = to_air.to_air(l2d)
    assert air["CONSIGNEE_INFO_TAX"] == "VAT769638661", air["CONSIGNEE_INFO_TAX"]
    assert air["CONSIGNEE_INFO_EORI"] == ""


def _desk_checks(k, l2, l3):
    """把 desk.js 里那份**手抄**的即时提示判据真跑一遍（不是 grep 字符串）。
    它只用在核对区那一格下面那行字上，注释里写着"与后端 validator 同口径"——
    后端改口径而这一份没跟着改，界面上就会出现反着走的提示，而这只有浏览器能看见。"""
    import json
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which("node"):
        raise AssertionError("机器上没有 node：前端判据断言跑不了，别当成通过")
    logic_js = str(Path(__file__).resolve().parent.parent / "web" / "js" / "logic.js")
    src = ("const L = require(%s);\nconst NUM = {};\nconst toNum = () => null;\n" % json.dumps(logic_js))
    src += web_src.fn(web_src.part("js/desk.js"), "function checks") + "\n"
    src += "console.log(JSON.stringify(checks(%s, %s, %s)));" % (
        json.dumps(k), json.dumps(l2), json.dumps(l3))
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, "node 跑 desk.js 的 checks 失败：%s" % (r.stderr.strip() or r.stdout.strip())
    return [w for w, _kind in json.loads(r.stdout.strip())]


def test_the_desk_inline_hints_follow_the_new_caliber():
    """四格提示都要跟着口径走：起运港城市全名、电话不再强制 +、识别号带标签，
    各自都不许再挨一条反着走的提示；而"没归一出来"这类老毛病还得照样出声。"""
    assert not [w for w in _desk_checks("ORIGIN_NAME", "BEIJING CAPITAL INTERNATIONAL APT", "BEIJING")], \
        "北京票下面还挂着三字码提示"
    assert [w for w in _desk_checks("DEST_NAME", "WARSAW", "WARSAW")], "到达港没转成三字码还得报"
    assert not [w for w in _desk_checks("SHIPPER_INFO_CITY", "QINGDAO", "QINGDAO")], \
        "发货人城市留票面拼音是定案，不该被提示"
    assert not [w for w in _desk_checks("CONSIGNEE_INFO_TEL", "'8613944484042", "8613944484042")], \
        "票面没有 + 就不该再要求 +"
    assert not [w for w in _desk_checks("CONSIGNEE_INFO_EORI", "CNPJ: 00280273000137",
                                       "CNPJ00280273000137")], "带标签是新口径"
    assert [w for w in _desk_checks("CONSIGNEE_INFO_EORI", "", "VAT123")], "本体过短还得报"
    assert [w for w in _desk_checks("CONSIGNEE_INFO_EORI", "", "VAT1234567")], \
        "位数要按去掉标签词的本体数：七个数字的号不该因为前面挂了 VAT 就够格"
    assert [w for w in _desk_checks("CONSIGNEE_INFO_TEL", "", "abc")], "不像电话的还得报"


def test_a_label_in_the_id_cell_is_no_longer_a_red_flag():
    """标签就是要发的内容：CNPJ 连标签落在 TAX 格不该挨旗（「混进了标签」那条 v1.17.2 就撤了）。
    2026-11-14 两列拆分后这号住 TAX 列——落 EORI 格才是错位（另一条用例管）。"""
    air = l2(CONSIGNEE_INFO_TAX="CNPJ00280273000137", CONSIGNEE_INFO_COUNTRY="BR")
    assert not [w for w in validator.validate_air(air) if "CONSIGNEE_INFO_TAX" in w], \
        validator.validate_air(air)


def test_the_digit_count_checks_ignore_the_label_letters():
    """位数与「过短」都按号码本体判：USCI 那四个字母不是位数的组成部分，
    不然 `USCI911201117706402073` 会被报成 22 位，而电话吞了税号那条也会跟着失灵。
    2026-11-14 两列拆分：这些判据住在 TAX 列（EORI 列只放海关号，不再管位数）。"""
    ok = l2(SHIPPER_INFO_TAX="USCI" + USCI, SHIPPER_INFO_COUNTRY="CN")
    assert not [w for w in validator.validate_air(ok) if "应为 18 位" in w], validator.validate_air(ok)
    cut = l2(SHIPPER_INFO_TAX="USCI91120111770640207", SHIPPER_INFO_COUNTRY="CN")
    assert any("应为 18 位" in w for w in validator.validate_air(cut)), "17 位还得报"
    tiny = l2(SHIPPER_INFO_TAX="VAT12345", SHIPPER_INFO_COUNTRY="GB")
    assert any("过短" in w for w in validator.validate_air(tiny)), "本体过短还得报"
    dup = l2(CONSIGNEE_INFO_TEL="+559240091129", CONSIGNEE_INFO_COUNTRY="BR",
             CONSIGNEE_INFO_TAX="CNPJ559240091129")
    assert any("与电话同值" in w for w in validator.validate_air(dup)), "带标签也要认出那是电话"


def test_l3_falls_back_to_the_whole_string_for_a_lone_tax_number():
    """票面把 USCI 只印在整串那一格（地址、电话都干净，BJS00032108 发货人即如此）时，
    L3 要从整串里把号认出来——2026-11-14 起归进 TAX 格（EORI 只放海关号）；
    整串本身一个字不动：那号码就是票面那一格印的。"""
    l2d = l2(MAWB_NO="999-30825351", HAWB_NO="BJS00032108",
             SHIPPER_INFO="BEIJING SHUNYI CO LTD, ROOM 101 BEIJING CN, "
                          "TEL: +861065766886 USCI:91110105562078969J",
             SHIPPER_INFO_COMP_NAME="BEIJING SHUNYI CO LTD",
             SHIPPER_INFO_COMP_ADDRESS="ROOM 101 BEIJING 101300 CN",
             SHIPPER_INFO_CITY="BEIJING", SHIPPER_INFO_COUNTRY="CN",
             SHIPPER_INFO_TEL="+861065766886")
    l3 = to_air.to_air(l2d)
    assert l3["SHIPPER_INFO_TAX"] == "USCI91110105562078969J", l3["SHIPPER_INFO_TAX"]
    assert l3["SHIPPER_INFO_EORI"] == ""
    assert l3["SHIPPER_INFO_TEL"] == "861065766886", l3["SHIPPER_INFO_TEL"]
    assert l3["SHIPPER_INFO"] == codes.sym_to_space(l2d["SHIPPER_INFO"]), \
        f"整串发出值 = L2 照抄串去符号（2026-10-27 定案）：{l3['SHIPPER_INFO']!r}"
    assert "," in l2d["SHIPPER_INFO"], "L2 照抄层一个字不许动"


def test_l3_moves_a_legacy_tax_column_into_tax_and_keeps_the_eori():
    """归档里的老 L2 还带着已删除的 `*_INFO_TAX_ID`（39 列那几年解析出来的票）：
    `--recheck` 重算 L3 时必须把号保住——2026-11-14 起按形态归位：EORI 形态留 EORI 格，
    裸数字税号进 TAX 格。不并就等于把当年核对过的号静默丢掉，重跑模型又是一次额度。"""
    l2d = l2(HAWB_NO="OLD1001",
             SHIPPER_INFO_EORI="SE5563646560", SHIPPER_INFO_TAX_ID=USCI,
             CONSIGNEE_INFO_TAX_ID="00280273000218")
    l3 = to_air.to_air(l2d)
    assert l3["SHIPPER_INFO_EORI"] == "SE5563646560", l3["SHIPPER_INFO_EORI"]
    assert l3["SHIPPER_INFO_TAX"] == USCI, l3["SHIPPER_INFO_TAX"]
    assert l3["CONSIGNEE_INFO_TAX"] == "00280273000218", l3["CONSIGNEE_INFO_TAX"]


def test_merge_ids_is_idempotent_and_splits_on_spaced_slash_only():
    """暂存会反复写同一格：先拆再拼，反复喂同一串不能越拼越长。
    分隔符必须两侧带空格——巴西 CNPJ 自己就含斜杠（07.454.234/0001-10），裸斜杠一切号码就碎了。"""
    one = codes.merge_ids("IT13055460961", "9111010880211232X4")
    assert one == "IT13055460961 / 9111010880211232X4"
    assert codes.merge_ids(one, "9111010880211232X4") == one, "再拼一次必须原样，不能变成 A / B / B"
    cnpj = "07.454.234/0001-10"
    assert codes.merge_ids(cnpj) == cnpj, "CNPJ 自己的斜杠不许被当成隔符拆开"
    assert codes.merge_ids("", None, "  ") == ""


def test_missing_tax_flag_points_at_the_tax_column():
    """反向漏号红旗（票面有、字段里没有）还得响，而且要说清现在该填哪一列。
    2026-11-14 两列拆分：USCI/VAT 这类税号指 *_INFO_TAX；号落在任何一格都算接住。"""
    c = json.loads((FIX / "draft_hawb_no_tsn10359645.json").read_text(encoding="utf-8"))
    assert find_missing_tax(c["raw"], c["transcript"]) == [("USCI", USCI)]
    fid = verify_fidelity(c["raw"], c["transcript"])
    hits = [x for x in fid["failed"] if x["field"] == "TAX_ID"]
    assert len(hits) == 1 and USCI in hits[0]["reason"], fid["failed"]
    assert "SHIPPER_INFO_TAX" in hits[0]["reason"] and "CONSIGNEE_INFO_TAX" in hits[0]["reason"], \
        "红旗得指向今天真正那一列，否则复核的人按图去找一个已经不存在的格子"
    assert find_missing_tax(dict(c["raw"], SHIPPER_INFO_TAX=USCI), c["transcript"]) == [], \
        "号落进 TAX 就该闭嘴"
    assert find_missing_tax(dict(c["raw"], SHIPPER_INFO_EORI=USCI), c["transcript"]) == [], \
        "号落在任何一格都算接住（找得到看的是全部字段值）"


def test_missing_tax_still_catches_eu_eori_labels():
    """欧票把海关号印成 `EORI IT03268900267`：号已被任何字段收走（含 EORI 格、或还黏在地址行）
    则闭嘴，不给人添重复旗。"""
    tr = {"lines": [{"text": t} for t in [
        "OZONE S.R.L", "VIALE DELLE INDUSTRIE 10", "EORI IT03268900267", "TEL 390422470376"]],
        "full_text": ""}
    assert find_missing_tax({}, tr) == [("EORI", "IT03268900267")]
    assert find_missing_tax({"CONSIGNEE_INFO_EORI": "IT03268900267"}, tr) == []
    fid = verify_fidelity({}, tr)
    eorihits = [x for x in fid["failed"] if x["field"] == "TAX_ID"]
    assert eorihits and "_INFO_EORI" in eorihits[0]["reason"], \
        "EORI 标签的漏号要指向 EORI 列（不是 TAX）：需核票填同主体的 SHIPPER_INFO_EORI…"
    tr3 = {"lines": [{"text": "FORMERVANGEN 5 EORI NO DK28490704"}], "full_text": ""}
    glued = {"CONSIGNEE_INFO_COMP_ADDRESS": "FORMERVANGEN 5 EORI NO DK28490704"}
    assert find_missing_tax(glued, tr3) == [], "号还黏在我方地址行里=已捕获，不重复报"


def test_tax_check_needs_a_label_and_a_real_number():
    """只认带标签的号：裸 18 位数字可能是货值/账号，抓它做红旗就是误报。"""
    tr = {"lines": [{"text": t} for t in [
        "TIANJIN 300385 CN", "TE +862258388999 USCI: " + USCI, "ACCOUNT 640958392012345678",
        "TAX ID: 12345"]], "full_text": ""}
    assert find_missing_tax({}, tr) == [("USCI", USCI)], "带标签的真税号必须被抓到"
    assert find_missing_tax({"X": ""}, {"lines": [{"text": "ACCOUNT 640958392012345678"}]}) == [], \
        "无标签裸数字不该当税号报出来"


def test_fidelity_checks_each_number_in_a_joined_eori_cell():
    """一格两个号时票面常常分两处印（收货人名下 IT 海关号、页底 customs 区 VAT）。
    正向保真必须逐段回查——拿拼好的整串去找原文，每张双号票都会挂一条假红旗，
    而假红旗看多了人会连真红旗一起无视。"""
    tr = {"lines": [{"text": "CALZONI HYDRAULICS ITALY SRL"},
                    {"text": "EORI IT03268900267 VIA CASTELDEBOLE 10"},
                    {"text": "VAT NO 12345678901"},
                    {"text": "TEL +390422470376"}], "full_text": ""}
    raw = {"CONSIGNEE_INFO_EORI": "IT03268900267 / 12345678901"}
    fid = verify_fidelity(raw, tr)
    assert [x for x in fid["failed"] if x["field"] == "CONSIGNEE_INFO_EORI"] == [], fid["failed"]
    bad = verify_fidelity({"CONSIGNEE_INFO_EORI": "IT03268900267 / 99999999999"}, tr)
    assert [x for x in bad["failed"] if x["field"] == "CONSIGNEE_INFO_EORI"], "编出来的号要照样抓出来"


def test_eori_column_holds_only_eori_shaped_numbers():
    """2026-11-14 两列拆分：EORI 格只收两位国家字母开头的海关号——裸数字税号、带税号标签的
    号落在这一格都是错位，要报并指路 _INFO_TAX。L3 已按形态自动分流（classify_id），
    这条拦的是模型没跟上、人工改错格、老暂存票重验。判形按摘掉标签后的号码本体。"""
    def w(air):
        return [x for x in validator.validate_air(air) if "_INFO_EORI" in x]
    ok = {"SHIPPER_INFO_EORI": "IT13055460961 / SE5563646560",
          "CONSIGNEE_INFO_EORI": "EORIIT03268900267"}
    assert not w(ok), "两个 EORI 拼一格、带 EORI 标签的连写都算合法：" + str(w(ok))
    assert w(dict(ok, SHIPPER_INFO_EORI=USCI)), "纯数字 USCI 落 EORI 格要报错位"
    assert w(dict(ok, SHIPPER_INFO_EORI="00.280.273/0001-37")), "CNPJ 落 EORI 格要报错位"
    assert w(dict(ok, SHIPPER_INFO_EORI="VAT769638661")), "VAT 号落 EORI 格要报错位（该去 TAX）"
    assert w(dict(ok, SHIPPER_INFO_EORI="12345")), "短到不像号的要报"
    assert w(dict(ok, SHIPPER_INFO_EORI="SEE INVOICE")), "散文要报"
    assert all("_INFO_TAX" in x for x in w(dict(ok, SHIPPER_INFO_EORI=USCI))), \
        "错位旗要教人往哪儿挪"


def test_validator_judges_cn18_on_the_tax_column():
    """2026-11-14 两列拆分：18 位 USCI 判据跟着号码搬进 _TAX 列（EORI 列只放海关号，
    不再管位数）。截断的 USCI 落在 TAX 格要报，落在 EORI 格则由形态检查报错位。"""
    def w(air):
        return [x for x in validator.validate_air(air) if "_TAX" in x]
    ok = {"SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_TAX": USCI}
    assert not w(ok), "18 位 USCI 住在 TAX 格是正座：" + str(w(ok))
    assert w(dict(ok, SHIPPER_INFO_TAX="911201117706402")), "被截断的 USCI 在 TAX 格要报 18 位"
    assert not w({"SHIPPER_INFO_COUNTRY": "SA", "SHIPPER_INFO_TAX": "300057757900003"}), \
        "CN 18 位判据只对 CN 判，别国税号长短不一"


def test_hs_keeps_first_six_digits_and_phone_keeps_alnum_only():
    """2026-11-14 业主两条口径落到分单链：HS 每段只留前 6 位（模型连抄/带标签抄，
    分段符五花八门——逗号、空格、标签都认）；电话/传真只留字母数字、多号只取第一个
    （与主单同一个 codes.phone_cell，统一的是引擎）。"""
    assert codes.hs6("HS CODE:8526109 HS CODE:8412210") == "852610,841221"
    assert codes.hs6("84129090,85171200") == "841290,851712"
    assert codes.hs6("852610") == "852610"
    assert codes.hs6("852610,841221") == "852610,841221", "已经 6 位的重复喂原样"
    assert codes.hs6("无") == ""
    l2d = l2(MAWB_NO="999-30825351", HAWB_NO="HS6TEST",
             GOODS_HS_CODE="HS CODE:8526109 HS CODE:8412210",
             SHIPPER_INFO_COMP_NAME="TEST CO", SHIPPER_INFO_COMP_ADDRESS="ROOM 1 BEIJING CN",
             SHIPPER_INFO_TEL="+86 10 64391234 / 13800138000")
    l3 = to_air.to_air(l2d)
    assert l3["GOODS_HS_CODE"] == "852610,841221", l3["GOODS_HS_CODE"]
    assert l3["SHIPPER_INFO_TEL"] == "861064391234", l3["SHIPPER_INFO_TEL"]


def test_legacy_air_records_lose_nothing_on_submit():
    """历史 air 里还留着 `*_INFO_TAX_ID`（老票、老暂存）：提交时按形态归位——
    2026-11-14 两列拆分后海关号进 EORI、裸数字税号进 TAX（公司列面已有税号格）。
    不归位就等于把已经核对过的号静默丢掉。"""
    air = {"MAWB_NO": "999-90273120", "HAWB_NO": "OLD001",
           "SHIPPER_INFO_EORI": "IT13055460961", "SHIPPER_INFO_TAX_ID": USCI,
           "CONSIGNEE_INFO_TAX_ID": "00.280.273/0001-10", "CONSIGNEE_INFO_EORI": ""}
    rec = company_api.order_record("999-90273120", "OLD001", air, None)
    assert rec.get("SHIPPER_INFO_EORI") == "IT13055460961", rec.get("SHIPPER_INFO_EORI")
    assert rec.get("SHIPPER_INFO_TAX") == USCI, rec.get("SHIPPER_INFO_TAX")
    assert rec.get("CONSIGNEE_INFO_TAX") == "00280273000110", rec.get("CONSIGNEE_INFO_TAX")
    assert not rec.get("CONSIGNEE_INFO_EORI"), "裸数字 CNPJ 不进 EORI 格"
    assert not [k for k in rec if k.endswith("_TAX_ID")], "税号老列不该出现在提交体里"


def test_desk_js_moves_its_tax_logic_onto_the_eori_cell():
    """审核台里认字段的代码都得跟着搬：即时提示（checks）要指到识别号那一格。
    留一处还在认 `_TAX_ID`，制单员就会看到一条改了格子也不消的提示。
    （从前还有第三处——"混进标签"这类提示红旗的自动消除 isTaxLabelFlag/taxFlagCleared，
    v1.17.2 删了：它把旗从界面上拿掉却不写进 acked_flags，服务端那道门只认 acked_flags，
    结果是条点不到的死旗。红旗一律由人确认，见 test_review_flow 第五节。）"""
    js = web_src.part("js/desk.js")
    assert "_TAX_ID" not in js, "前端还在引用一列已经不存在的字段"
    assert 'k.endsWith("_EORI")' in js, "即时提示要搬到识别号那一格"
    assert "taxFlagCleared" not in js, "前端不许再自己判某条红旗不算数（服务端只认人确认过的）"


def test_schema_no_longer_creates_the_tax_columns():
    """新库不再建这两列；老库**不 DROP**（列与历史值留着，只是不再写）——删列不可逆，
    而留着既不碍事也保得住"当年确实解析出过这个号"的证据。"""
    sql = (Path(validator.__file__).parent / "schema.sql").read_text(encoding="utf-8")
    ddl = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert not re.search(r"^\s+SHIPPER_INFO_TAX_ID\s+VARCHAR", ddl, re.M), "建表语句还在造已删的列"
    assert not re.search(r"^\s+CONSIGNEE_INFO_TAX_ID\s+VARCHAR", ddl, re.M)
    assert "TAX_ID" in sql, "注释里要留下这段历史与老库怎么办，否则下一个人以为从没这两列"
