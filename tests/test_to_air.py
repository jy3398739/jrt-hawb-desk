# -*- coding: utf-8 -*-
"""to_air(L3 归一化) 与 validator 的单元用例。
每对 (输入, 期望) 都对应一个真实缺陷回归，注释里标了票号，别当噪声删掉。
源码只用 \\uXXXX 转义写特殊字符：Windows 上直接贴 U+2010 会被编辑器/管道改成别的字符。
"""
import codes
import to_air
import validator
import vlm_extract

# 缺陷1：票面"电话+联系人+CNPJ"挤同一格，整行照抄后剥字母会拼成 26 位怪物号（PEK1642839）
# 缺陷4：Excel 前导单引号强制文本，LibreOffice 把 ' 印进 PDF 文字层，L3 丢 +（AE20260916）
# L1 改走文字层后引入：排版连字符 U+2010 曾被当作号码断点，+86‐022‐60359588 只剩 60359588
PHONE_CASES = [
    ("+559240091129 Carla Os CNPJ: 00280273000137", "+559240091129"),
    ("'8613944484042", "+8613944484042"),
    ("'905428542609", "+905428542609"),
    ("+86-13311099286", "+8613311099286"),          # L3 口径去连字符（32 单基线无一含 -）
    ("TEL:+86-13311099286", "+8613311099286"),
    ("+46 (0)70 31 76 762", "+46703176762"),        # 欧标干线 0 不是号码的一部分
    ("+86‐022‐60359588", "+8602260359588"),  # U+2010 排版连字符
    ("02089205262", "02089205262"),                 # 本地号无国际冠码：原样保留，不硬造 +
    ("无号码纯文字", ""),
    ("", ""),
]

# 缺陷2：TO/DEST 未走 IATA 映射，且 CITY_IATA 漏项（PEK1642839 的 MANAUS）
IATA_CASES = [
    ("MANAUS", "MAO"), ("MUMBAI", "BOM"), ("GUARULHOS", "GRU"),
    ("ERCAN", "ECN"), ("VIRACOPOS", "VCP"), ("LYON", "LYS"),
    ("WARSZAWA", "WAW"),     # 波兰语原名（BJSA26048069），码表只收了英文 WARSAW
    ("FRA", "FRA"), ("BEIJING CAPITAL INTERNATIONAL APT", "BJS"), ("", ""),
]


def test_clean_phone():
    for src, want in PHONE_CASES:
        got = to_air._clean_phone(src)
        assert got == want, f"_clean_phone({src!r}) -> {got!r}，期望 {want!r}"


def test_first_phone():
    assert to_air._first_phone("+8613944484042 / +8613800000000") == "+8613944484042"
    assert to_air._first_phone("010-88889999;010-88880000") == "010-88889999"


def test_iata_place_mapping():
    for src, want in IATA_CASES:
        got = codes.iata_place(src)
        assert got == want, f"_iata_place({src!r}) -> {got!r}，期望 {want!r}"


def test_country_with_parenthesised_iso2():
    """票面把国名和国码写在一格里（'Slovak Republic (SK)'，999-95764815 收货人）：
    L3 必须归成 SK。不归一的话发给公司的 *_INFO_COUNTRY 那一格还是个短语，公司按两字母码校验会拒。"""
    assert to_air.country_to_iso2("Slovak Republic (SK)") == "SK"
    assert to_air.country_to_iso2("SLOVAK REPUBLIC (sk)") == "SK"
    assert to_air.country_full("Slovak Republic (SK)") == "SLOVAK REPUBLIC"
    # 括号里不是两字母国码时不能乱摘（城市/州名缩写等）
    assert to_air.country_to_iso2("Vietnam (VNM)") == "VIETNAM (VNM)"


def test_party_whole_string_is_the_transcribed_text_not_an_assembly():
    """2026-10-14 业主定案：发货人/收货人「整串」这一列只许是票面那一格的照抄串，
    L3 不再从子字段重拼。重拼把三种破坏都干过（对照 L3 夹具）：同一段街道出现两遍
    （2_sch_1 收货人、5e696c 收货人）、邮编省名整段丢掉（5_dhl 收货人的 KA 560058）、
    公司名第二行被挪到地址后面（4_ceva 发货人）——而这一列是制单员核对时读的那一眼。

    子列照常归一：同一条里 *_INFO_COUNTRY 要落成两字母码、*_INFO_TEL 要清成号码本体。"""
    l2d = {k: "" for k in codes.TARGET_KEYS_OUT}
    l2d.update({
        "MAWB_NO": "131-56115007", "HAWB_NO": "BJS261001",
        "SHIPPER_INFO": "KG FRIDMAN AB, GJUTERIGATAN 11-13 SE-652 21 KARLSTAD, SWEDEN, "
                        "TEL: +46 (0)70 31 76 762",
        "SHIPPER_INFO_COMP_NAME": "KG FRIDMAN AB",
        "SHIPPER_INFO_COMP_ADDRESS": "GJUTERIGATAN 11-13 SE-652 21 KARLSTAD, SWEDEN",
        "SHIPPER_INFO_CITY": "KARLSTAD", "SHIPPER_INFO_COUNTRY": "SWEDEN",
        "SHIPPER_INFO_POSTAL": "652 21", "SHIPPER_INFO_TEL": "+46 (0)70 31 76 762"})
    l3 = to_air.to_air(l2d)
    assert l3["SHIPPER_INFO"] == l2d["SHIPPER_INFO"], \
        f"整串被改了：{l3['SHIPPER_INFO']!r}"
    assert l3["SHIPPER_INFO"].count("GJUTERIGATAN") == 1, "重拼会把街道再挂一遍"
    # 同一格的国家/电话照常归一——这次动的只有整串的取值来源
    assert l3["SHIPPER_INFO_COUNTRY"] == "SE", l3["SHIPPER_INFO_COUNTRY"]
    assert l3["SHIPPER_INFO_TEL"] == "+46703176762", l3["SHIPPER_INFO_TEL"]


def test_l3_invents_no_whole_string_when_the_model_left_it_empty():
    """模型没抄整串（只给了子字段）时，L3 要留空，不许由代码拼一份顶上。
    「这格是空的」与「这格是代码造的」在公司那边长得一模一样，但只有前者是能被发现的漏——
    留空会进制单员的空栏确认，造出来的串谁都不会去看第二眼。"""
    l2d = {k: "" for k in codes.TARGET_KEYS_OUT}
    l2d.update({
        "MAWB_NO": "131-56115007", "HAWB_NO": "BJS261002",
        "CONSIGNEE_INFO_COMP_NAME": "BIG METAL LTD",
        "CONSIGNEE_INFO_COMP_ADDRESS": "OCCUPATIONAL ROAD 1, LONDON SE17 3BE GB",
        "CONSIGNEE_INFO_CITTY": "LONDON", "CONSIGNEE_INFO_COUNTRY": "GB",
        "CONSIGNEE_INFO_TEL": "+442077010005"})
    l3 = to_air.to_air(l2d)
    assert l3["CONSIGNEE_INFO"] == "", f"空整串被代码拼出来了：{l3['CONSIGNEE_INFO']!r}"
    assert l3["CONSIGNEE_INFO_TEL"] == "+442077010005", l3["CONSIGNEE_INFO_TEL"]
    assert l3["CONSIGNEE_INFO_COUNTRY"] == "GB", l3["CONSIGNEE_INFO_COUNTRY"]


def test_consignee_city_keeps_the_ticket_name():
    """收货人城市留票面全称（2026-10-15 业主定案「收货人城市要全称，不要三字码」）：
    公司那一格要的是城市名，转成机场码等于让人在核对区把 CHENNAI 手动改回去。
    起运港/目的港/航路那几列仍是三字码，别跟着改；发货人城市这次没动——业主只点了收货人，
    要不要同样留全称等他表态，所以这里把发货人仍转码这件事一并钉住。"""
    l2d = {k: "" for k in codes.TARGET_KEYS_OUT}
    l2d.update({
        "MAWB_NO": "131-56115007", "HAWB_NO": "BJS261003",
        "SHIPPER_INFO_COMP_NAME": "QINGDAO TEXTILE CO LTD",
        "SHIPPER_INFO_COMP_ADDRESS": "NO.91 XINGANG ROAD, QINGDAO, CHINA",
        "SHIPPER_INFO_CITY": "QINGDAO", "SHIPPER_INFO_COUNTRY": "CN",
        "CONSIGNEE_INFO_COMP_NAME": "BIG METAL LTD",
        "CONSIGNEE_INFO_COMP_ADDRESS": "OCCUPATIONAL ROAD 1, LONDON SE17 3BE GB",
        "CONSIGNEE_INFO_CITTY": "LONDON", "CONSIGNEE_INFO_COUNTRY": "GB"})
    l3 = to_air.to_air(l2d)
    assert l3["CONSIGNEE_INFO_CITTY"] == "LONDON", \
        f"收货人城市被转成了三字码：{l3['CONSIGNEE_INFO_CITTY']!r}"
    assert l3["SHIPPER_INFO_CITY"] == "TAO", \
        f"发货人城市现仍转码（有意的不对称）：{l3['SHIPPER_INFO_CITY']!r}"


def test_prompt_asks_for_a_transcribed_party_string():
    """提示词要与 L3 同口径：L3 不再重拼，提示词却还写着「这两个串允许按规定格式拼接」，
    模型就照旧交一份自己拼的串，而这一串会被当成"票面原文"送进公司——比原来更糟。

    断言只看这两件事：这两列各自行里要出现"照抄"、不许再出现"拼接"（"不是拼接"这种否定句除外）；
    铁律 C 不许再给这两列开例外。具体措辞随提示词调，别把字句钉死。"""
    blob = vlm_extract.PROMPT_HEAD
    assert "系统拼接" not in blob, blob[:200]
    for key in ("SHIPPER_INFO", "CONSIGNEE_INFO"):
        i = blob.index(key + " - ")
        line = blob[i:blob.index("\n", i)]
        assert "照抄" in line, f"{key} 那一行没要求照抄：{line}"
        assert "拼接" not in line.replace("不是拼接", ""), f"{key} 还在允许拼接：{line}"
    c = blob[blob.index("C. 不猜不补"):blob.index("D. CREATE_TIME")]
    assert "允许" not in c, f"铁律 C 还在给哪一列开口子：{c}"


def test_accented_city_name_folds():
    """带重音的地名要能和 ASCII 写的码表键对上（_fold_ascii）。"""
    acc = {"A": "\u00c4", "E": "\u00c9", "I": "\u00cd", "O": "\u00d6", "U": "\u00dc"}
    key = accented = None
    for k in codes.CITY_IATA:
        if not k.isascii():
            continue
        for c in k:
            if c in acc:
                key, accented = k, k.replace(c, acc[c], 1)
                break
        if key:
            break
    assert key, "CITY_IATA 里找不到可注入重音的键，测试需换写法"
    assert codes.lookup_city(accented) == codes.lookup_city(key), \
        f"{accented!r} 未折叠回 {key!r} 的码值"


def test_drop_phone_segs_keeps_tax_id():
    """L2 整格照抄是对的，L3 只剥号码本体：同格的 CNPJ/门牌/邮编不能被误删。"""
    s = "Av Xavantes 416, TE +551156446468 CNPJ: 00280273000218, NO.91, 410 507"
    out = to_air._drop_phone_segs(s)
    assert "+551156446468" not in out, f"号码没被剔除: {out!r}"
    for keep in ("CNPJ: 00280273000218", "NO.91", "410 507", "Av Xavantes 416"):
        assert keep in out, f"{keep!r} 被误删: {out!r}"


def test_to_air_end_to_end_on_one_ticket():
    """一个完整 L2 → L3 的收口检查：国家 ISO2、电话、主单号格式、航路三字码同时成立。"""
    l2 = {"MAWB_NO": "020-1902 5661", "HAWB_NO": "TAO-5808 9795",
          "ORIGIN_NAME": "QINGDAO", "TO1": "FRA", "TO2": "MIA", "TO3": "MANAUS",
          "DEST_NAME": "MANAUS", "GOODS_INFO": "STEEL PARTS",
          "PIECES": 3, "WEIGHT": 16.0, "SLAC": 3, "CREATE_TIME": "2026-09-20",
          "SEND_STATUS": "PENDING",
          "SHIPPER_INFO_COMP_NAME": "QINGDAO TEXTILE CO LTD",
          "SHIPPER_INFO_COMP_ADDRESS": "NO.91 XINGANG ROAD, TE +8613944484042, Qingdao, China",
          "SHIPPER_INFO_CITY": "QINGDAO", "SHIPPER_INFO_COUNTRY": "CN",
          "SHIPPER_INFO_STATE": "", "SHIPPER_INFO_POSTAL": "266000",
          "SHIPPER_INFO_TEL": "TE +8613944484042 Wang CNPJ: 00280273000137",
          "SHIPPER_INFO_FAX": "", "SHIPPER_INFO_EORI": "", "SHIPPER_INFO_AEO": "",
          "SHIPPER_INFO_EMAIL": ""}
    for suf in ("COMP_NAME", "COMP_ADDRESS", "CITTY", "COUNTRY", "STATE", "POSTAL",
                "TEL", "FAX", "EORI", "AEO", "EMAIL"):
        l2.setdefault("CONSIGNEE_INFO_" + suf, "")
    l3 = to_air.to_air(l2)
    assert l3["MAWB_NO"] == "020-19025661", l3["MAWB_NO"]
    assert l3["TO3"] == "MAO" and l3["DEST_NAME"] == "MAO", (l3["TO3"], l3["DEST_NAME"])
    assert l3["ORIGIN_NAME"] == "TAO", l3["ORIGIN_NAME"]
    assert l3["SHIPPER_INFO_COUNTRY"] == "CN"
    assert l3["SHIPPER_INFO_TEL"] == "+8613944484042", l3["SHIPPER_INFO_TEL"]
    assert "CNPJ" not in l3["SHIPPER_INFO_TEL"]
    # 电话已单独成字段，L3 街道里不该再留号码，但门牌号要保住（_drop_phone_segs 接进 to_air 了吗）
    assert l3["SHIPPER_INFO_COMP_ADDRESS"] == "NO.91 XINGANG ROAD", l3["SHIPPER_INFO_COMP_ADDRESS"]
    assert l3["SHIPPER_INFO"] == "", \
        "这份 L2 没给整串：出口必须是空的，代码不许拿子字段拼一份顶上（2026-10-14 定案）"
    assert validator.validate_air(l3) == [], validator.validate_air(l3)


def test_validator_division_l2_vs_l3():
    """缺陷3：票面印城市名时 L2 必须照抄，"非三字码"的告警只属于 L3。"""
    l2 = {"TO1": "FRA", "TO2": "MIA", "TO3": "MANAUS", "MAWB_NO": "020-43605590",
          "HAWB_NO": "PEK1642839", "PIECES": 3, "WEIGHT": 16.0, "CREATE_TIME": "2026-09-20"}
    assert validator.validate_raw(l2) == [], validator.validate_raw(l2)
    assert any("MANAUS" in w for w in validator.validate_air(dict(l2, TO3="MANAUS")))
    # 填成承运人码/航班号一定不是票面 To 栏值，L2 就该拦
    lw = validator.validate_raw(dict(l2, TO1="JL", TO2="CA949"))
    assert any("承运人码" in w for w in lw) and any("航班号" in w for w in lw), lw


def test_validator_warns_on_missing_first_routing_hop():
    """航路第一跳留空是静默漏抄：实测 qwen3.8 关思考 10 票里 4 票只填了目的站、TO1 空着，
    正向保真（L2⊆L1）与三字码反向核查都抓不到（后者会把公司名 AIR & SEA 里的 SEA 当成西雅图码误报）。
    判据用结构事实：本票库 18 票无一路出栏 TO1 为空，且 TO1 与目的站同源。"""
    base = {"MAWB_NO": "020-43605590", "HAWB_NO": "PEK1642839", "PIECES": 3, "WEIGHT": 16.0,
            "CREATE_TIME": "2026-09-20", "TO1": "AMS", "TO2": "", "TO3": "", "DEST_NAME": "AMS"}
    assert validator.validate_raw(dict(base)) == [], validator.validate_raw(dict(base))
    w = validator.validate_raw(dict(base, TO1=""))
    assert any("TO1" in x and "漏抄" in x for x in w), w
    # 目的站也空着时无从推断，不凭猜测报（避免与"整栏没填"这种情形混在一起）
    assert not any("TO1" in x for x in validator.validate_raw(dict(base, TO1="", DEST_NAME=""))), \
        validator.validate_raw(dict(base, TO1="", DEST_NAME=""))
    # 多跳票里 TO1 已填即不报，哪怕 TO2/TO3 空（本无中转点的票占多数）
    assert not any("TO1" in x for x in validator.validate_raw(dict(base, TO1="FRA", DEST_NAME="CHI"))), \
        validator.validate_raw(dict(base, TO1="FRA", DEST_NAME="CHI"))


def test_party_parsing_compiles_no_regex_after_the_tables_are_built():
    """parse_party 每票要给收、发货人各扫一遍国家码表（约 200 项）与省州表（约 60 项），
    从前「取最右那一个匹配」是在函数体里现场 `re.compile`——一票几百次 escape+拼串+查缓存，
    批处理时是纯 CPU 峰值（2026-10-16 效率审查 K2）。两张表都是模块级常量，
    正则就该在导入时编一次；城市停用词那条同理。

    数的是编译次数：跑热之后再解析二十票，一次都不许编。"""
    import re
    party = {"name": "SHENZHEN XINGTAIKE ELECTRONIC CO LTD", "tel": "", "fax": "",
             "tax_id": "",
             "address": "ROOM 501 BLOCK B, SHENZHEN, GUANGDONG 518000, CHINA"}
    got = codes.parse_party(party)
    assert (got["city"], got["state"], got["postal"], got["country"]) == \
        ("SHENZHEN", "GUANGDONG", "518000", "CHINA"), f"先确认解析本身没变：{got}"

    calls = []
    real = re.compile

    def counting(pattern, *a, **k):
        calls.append(pattern)
        return real(pattern, *a, **k)

    re.compile = counting
    try:
        for _ in range(20):
            codes.parse_party(party)
    finally:
        re.compile = real
    assert not calls, f"二十票里现场编了 {len(calls)} 次正则，例：{calls[:2]}"


def test_validator_warns_on_missing_awb_numbers():
    """主/分单号分两格印时容易漏抄其中一格，缺哪个都要单独出声。"""
    base = {"MAWB_NO": "020-43605590", "HAWB_NO": "PEK1642839",
            "PIECES": 3, "WEIGHT": 16.0, "CREATE_TIME": "2026-09-20"}
    assert any("MAWB_NO 缺失" in w for w in validator.validate_raw(dict(base, MAWB_NO="")))
    assert any("HAWB_NO 缺失" in w for w in validator.validate_raw(dict(base, HAWB_NO="")))
    w = validator.validate_raw(dict(base, MAWB_NO="", HAWB_NO=""))
    assert any("均为空" in x for x in w) and not any("MAWB_NO 缺失" in x for x in w), w
    # HAWB 栏误填 3 位承运人前缀号（那是主单号）
    assert any("误填主单号" in x for x in validator.validate_raw(dict(base, HAWB_NO="02043605590")))
