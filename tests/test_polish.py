# -*- coding: utf-8 -*-
"""逐格「优化」规则表（polish）的用例。

业主 2026-10-20 定的形态：核对区那一眼先看到票面原值，觉得值本身没错，再点「优化」
把这一格换成目的国规则写法。所以这个模块只准回答一个问题——
**这一格能不能由现值唯一推出新值**；推不出来就返回 None（宁可不动）。

两条硬界线由用例钉住，别让后人松口：
① 只碰子列，`*_INFO` 整串与公司名/地址/城市一律不进（照抄口径见 [[feedback-transcribed-fields-not-rebuilt]]，
   收发货人城市都留票面全称：收货人侧 2026-10-15 定案，发货人侧 2026-10-23 跟上，to_air 不再转三字码）；
② 要猜的不做：HS 补零、韩国 6 位邮编补横杠（新制 5 位、旧制 6 位都见过，横杠位置推不出来）、
   德国邦名（没有两字母码）——这些只挂旗，旗在 validator 那边。
"""
import polish

BR = {"CONSIGNEE_INFO_COMP_NAME": "ACME LTDA",
      "CONSIGNEE_INFO_COMP_ADDRESS": "RUA X 100, SAO PAULO, 04546040, BRAZIL",
      "CONSIGNEE_INFO": "ACME LTDA, RUA X 100, SAO PAULO, BRAZIL",
      "CONSIGNEE_INFO_COUNTRY": "BRAZIL",
      "CONSIGNEE_INFO_POSTAL": "04546040",
      "CONSIGNEE_INFO_STATE": "SAO PAULO",
      "CONSIGNEE_INFO_CITTY": "SAO PAULO"}


def _one(field, value, country):
    """取新值；不该动就是 None。规则号与依据另测。"""
    got = polish.one(field, value, country)
    return None if got is None else got[0]


# ── 邮编：位数与国别完全吻合才补分隔符 ──────────────────────────────────────────
def test_postal_puts_the_dash_where_that_country_prints_it():
    assert _one("CONSIGNEE_INFO_POSTAL", "04546040", "BR") == "04546-040", \
        "巴西 CEP 官方写法是 5+横杠+3（业主问的就是这条）"
    assert _one("CONSIGNEE_INFO_POSTAL", "1000001", "JP") == "100-0001", \
        "日本是横杠在第 3 位后，与巴西反着来"
    assert _one("CONSIGNEE_INFO_POSTAL", "1000050", "PT") == "1000-050"
    assert _one("CONSIGNEE_INFO_POSTAL", "00123", "PL") == "00-123", \
        "波兰是 2 位+横杠+3 位，横杠在头两位之后"
    assert _one("CONSIGNEE_INFO_POSTAL", "00001", "PL") == "00-001"
    assert _one("CONSIGNEE_INFO_POSTAL", "902101234", "US") == "90210-1234", \
        "ZIP+4 的横杠在第 5 位后"


def test_postal_puts_the_space_where_that_country_prints_it():
    assert _one("CONSIGNEE_INFO_POSTAL", "11433", "SE") == "114 33"
    assert _one("CONSIGNEE_INFO_POSTAL", "10432", "GR") == "104 32"
    assert _one("CONSIGNEE_INFO_POSTAL", "12000", "CZ") == "120 00"
    assert _one("CONSIGNEE_INFO_POSTAL", "1234AB", "NL") == "1234 AB"
    assert _one("CONSIGNEE_INFO_POSTAL", "M5V3A4", "CA") == "M5V 3A4"
    assert _one("CONSIGNEE_INFO_POSTAL", "SW1A1AA", "GB") == "SW1A 1AA"
    assert _one("CONSIGNEE_INFO_POSTAL", "D6WY455", "IE") == "D6W Y455"


def test_postal_case_and_stray_space_are_cleaned_even_without_a_separator():
    assert _one("CONSIGNEE_INFO_POSTAL", "1234 ab", "NL") == "1234 AB"
    assert _one("CONSIGNEE_INFO_POSTAL", "m5v 3a4", "CA") == "M5V 3A4"
    assert _one("CONSIGNEE_INFO_POSTAL", " 04546-040 ", "BR") == "04546-040"


# ── 不该动的不许动：位数不合、国别无格式、带别的信息 ────────────────────────────
def test_postal_does_nothing_when_the_shape_does_not_match_the_country():
    for value, cc in (("0454604", "BR"), ("10000001", "JP"), ("045460", "BR"),
                      ("1234", "NL"), ("M5V3A", "CA")):
        assert _one("CONSIGNEE_INFO_POSTAL", value, cc) is None, \
            f"{cc} 的 {value!r} 位数对不上本国格式：动它就是编数据"


def test_postal_leaves_alone_formats_we_do_not_own():
    # 德国/意大利/西班牙本身就是 5 位连续数字，没有分隔符要补
    for cc, val in (("DE", "12345"), ("IT", "00100"), ("ES", "28001"), ("CN", "200000")):
        assert _one("CONSIGNEE_INFO_POSTAL", val, cc) is None, f"{cc} 不该被改"
    # 韩国新制 5 位、旧制 6 位带横杠都还在票面上出现：横杠位置推不出来 → 只挂旗
    assert _one("CONSIGNEE_INFO_POSTAL", "123456", "KR") is None
    # 带国别前缀的写法（瑞典 SE-114 33 那种）里有信息，剥前缀属于改内容不是改格式
    assert _one("CONSIGNEE_INFO_POSTAL", "SE-114 33", "SE") is None
    # 已经是规范写法 → 幂等，不许报第二次
    assert _one("CONSIGNEE_INFO_POSTAL", "04546-040", "BR") is None
    assert _one("CONSIGNEE_INFO_POSTAL", "M5V 3A4", "CA") is None


def test_postal_needs_a_country_before_it_can_guess_anything():
    assert _one("CONSIGNEE_INFO_POSTAL", "04546040", "") is None, \
        "认不出国家就不知道该怎么印，不动"
    assert _one("CONSIGNEE_INFO_POSTAL", "04546040", "随便一句话") is None


# ── 州 / 省：只放有官方两字母码、且全称不会撞别的国家的 ─────────────────────────
def test_state_full_name_becomes_the_official_short_code():
    assert _one("CONSIGNEE_INFO_STATE", "NEW YORK", "US") == "NY"
    assert _one("CONSIGNEE_INFO_STATE", "District of Columbia", "US") == "DC"
    assert _one("CONSIGNEE_INFO_STATE", "BRITISH COLUMBIA", "CA") == "BC"
    assert _one("CONSIGNEE_INFO_STATE", "SAO PAULO", "BR") == "SP"
    assert _one("CONSIGNEE_INFO_STATE", "MINAS GERAIS", "BR") == "MG"
    assert _one("CONSIGNEE_INFO_STATE", "NEW SOUTH WALES", "AU") == "NSW"


def test_state_is_accent_insensitive_and_stays_put_when_unlisted():
    assert _one("CONSIGNEE_INFO_STATE", "SÃO PAULO", "BR") == "SP", \
        "重音只是写法差别，查表要先把它摘掉"
    assert _one("CONSIGNEE_INFO_STATE", "BAVARIA", "DE") is None, \
        "德国邦没有两字母码，硬造一个就是编"
    assert _one("CONSIGNEE_INFO_STATE", "LOMBARDIA", "IT") is None
    assert _one("CONSIGNEE_INFO_STATE", "NUEVO LEON", "MX") is None, \
        "墨西哥 32 个联邦实体的两位 clave 没拿到可信全表（JL/JAL、MS/MOS、CL 与 DF 几种写法都在流传），" \
        "宁可这一格不动：表少几格只是不改，表错一格是静默把州改错"
    assert _one("CONSIGNEE_INFO_STATE", "SP", "BR") is None, "已经是码，幂等"
    assert _one("CONSIGNEE_INFO_STATE", "ON", "CA") is None
    assert _one("CONSIGNEE_INFO_STATE", "SAO PAULO", "US") is None, \
        "美国没有 Sao Paulo 这个州：拿错国家不许乱映射"


# ── 识别号那一格：不再往回补标点（2026-10-23 业主定案）──────────────────────────
def test_the_id_cell_is_never_punctuated_back():
    """从前「优化」会把裸 CNPJ 补成 00.280.273/0001-37；业主定案这一格发「标签词+连写号码」，
    补点补斜杠正好反着走，人一按就把要发的样子改坏，所以整条规则撤掉。"""
    for value in ("00280273000137", "12345678901", "CNPJ00280273000137",
                  "00.280.273/0001-37", "00280273000137 / 12345678901"):
        assert _one("CONSIGNEE_INFO_EORI", value, "BR") is None, \
            f"识别号格不该再有点「优化」补标点：{value!r}"


# ── 不可见字符与非 ASCII 连字符（U+2010 那一族坑） ──────────────────────────────
def test_invisible_chars_and_fancy_hyphens_are_normalised_to_ascii():
    assert _one("CONSIGNEE_INFO_POSTAL", "04546\u2010040", "BR") == "04546-040", \
        "票面排版的 U+2010 不是 ASCII 横杠，公司那边按字符串比会当两个值"
    assert _one("CONSIGNEE_INFO_TEL", "+55\u00a0113333\u200b4444", "BR") == "+55 1133334444", \
        "硬空格换一个普通空格、零宽字符直接删"
    for field in ("CONSIGNEE_INFO_STATE", "CONSIGNEE_INFO_EORI", "CONSIGNEE_INFO_AEO"):
        assert polish.one(field, "A\u200bB\u00a0C", "BR") is not None, \
            f"{field} 里的零宽/硬空格要清掉"
    assert _one("CONSIGNEE_INFO_STATE", "SAO PAULO", "BR") == "SP", "清完字符还能接着查表"


# ── 整票扫描：给出「将要发出去的值」，并且只给白名单里的格 ──────────────────────
def test_preview_shows_every_actionable_cell_of_a_party():
    rows = polish.preview(BR)
    by = {r["field"]: r for r in rows}
    assert by["CONSIGNEE_INFO_POSTAL"]["to"] == "04546-040"
    assert by["CONSIGNEE_INFO_STATE"]["to"] == "SP"
    assert by["CONSIGNEE_INFO_COUNTRY"]["to"] == "BR", "L3 本来就会转 alpha-2，从前核对区看不见"
    assert {r["rule"] for r in rows} >= {"POSTAL", "STATE_ABBR"}, "规则号要带出去才好留痕"
    assert rows and all(r["why"] for r in rows), "每条都得有话可写：依据要显示在人眼前"


def test_preview_never_touches_the_verbatim_block_or_the_name_and_address_cells():
    fields = dict(BR)
    fields["CONSIGNEE_INFO_TEL"] = "+55(11)3333-4444"
    fields["CONSIGNEE_INFO_COMP_NAME"] = "ACME  LTDA"
    fields["CONSIGNEE_INFO_EMAIL"] = "X@Y.COM "
    fields["CONSIGNEE_INFO_AEO"] = " A1 "
    seen = {r["field"] for r in polish.preview(fields)}
    for k in ("CONSIGNEE_INFO", "CONSIGNEE_INFO_COMP_NAME", "CONSIGNEE_INFO_COMP_ADDRESS",
              "CONSIGNEE_INFO_CITTY", "CONSIGNEE_INFO_CITY", "GOODS_INFO", "SHIPPER_INFO"):
        assert k not in seen, f"{k} 不是可优化格：整串要照抄、城市留全称是定案"
    for k in seen:
        assert k.split("_INFO_")[-1] in polish.ACTIONABLE_SUFFIXES, f"{k} 越出了白名单"


def test_preview_is_idempotent_and_empty_cells_are_not_optimised():
    fields = dict(BR, CONSIGNEE_INFO_FAX="", CONSIGNEE_INFO_EORI="", CONSIGNEE_INFO_TEL="")
    once = polish.preview(fields)
    assert once, "这张样票该有可优化的格子"
    after = dict(fields)
    for r in once:
        after[r["field"]] = r["to"]
    assert polish.preview(after) == [], "点完一轮优化，第二轮必须一格不报（否则计数永远清不掉）"
    assert all(r["field"] not in ("CONSIGNEE_INFO_FAX", "CONSIGNEE_INFO_EORI") for r in once), \
        "空格子没有可推出的新值，别报成「可优化」"


def test_master_chain_preview_follows_clean_ams_not_to_air():
    """主单那 36 列不过 to_air：国家只转大写、不替公司转两字母码，电话不重排。

    这条是把两条链的口径差别钉住——分单已经在发 BR，主单还按 IT 给的现值发原文大写；
    界面要是把两家用同一条规则显示，人就会看到"点了优化但发出去的不是那个值"。
    """
    fields = {"MAWB_NO": "999-95764373",
              "SHIPPER_INFO_COMP_NAME": "ACME LTDA",
              "SHIPPER_INFO_COUNTRY": "brazil",
              "SHIPPER_INFO_POSTAL": "04546040",
              "SHIPPER_INFO_STATE": "SAO PAULO",
              "SHIPPER_INFO_TEL": "+55 (11) 3333-4444",
              "SHIPPER_INFO_EORI": "00280273000137",
              "NOTIFY_INFO_COUNTRY": "JAPAN",
              "NOTIFY_INFO_POSTAL": "1000001"}
    by = {r["field"]: r for r in polish.preview(fields, chain="master")}
    assert by["SHIPPER_INFO_COUNTRY"]["to"] == "BRAZIL", "主单只转大写，alpha-2 那一步等 IT 定口径"
    assert by["SHIPPER_INFO_POSTAL"]["to"] == "04546-040"
    assert by["SHIPPER_INFO_STATE"]["to"] == "SP"
    assert by["NOTIFY_INFO_POSTAL"]["to"] == "100-0001", "通知人按通知人自己的国家，不跟着发货人"
    assert "SHIPPER_INFO_TEL" not in by, "主单口径不重排电话：没有这一步就不许给按钮"


def test_hawb_and_master_disagree_on_the_same_value_on_purpose():
    fields = {"SHIPPER_INFO_COMP_NAME": "ACME LTDA", "SHIPPER_INFO_COUNTRY": "BRAZIL"}
    hawb = {r["field"]: r["to"] for r in polish.preview(dict(fields), chain="hawb")}
    master = {r["field"]: r["to"] for r in polish.preview(dict(fields), chain="master")}
    assert hawb.get("SHIPPER_INFO_COUNTRY") != master.get("SHIPPER_INFO_COUNTRY"), \
        "两条链同源就该在这里看得出不同：分单发 BR，主单发 BRAZIL"


# ── 提交归一那一档：说清"号是从地址里提进来的"与"号本来在这格里、只是被摘了标签" ──
def test_an_id_pulled_out_of_the_address_is_named_a_pull_not_a_clean():
    """这一格是空的、号印在地址里：那是「提进来」。说成"摘掉标签词"会把人引到错的一眼上。

    真票上这一档出现得最多（本地 44 份解析结果里 24 格都是它），所以话必须说对。
    """
    rows = {r["field"]: r for r in polish.preview(
        {"CONSIGNEE_INFO_COMP_NAME": "ACME",
         "CONSIGNEE_INFO_COMP_ADDRESS": "VAT NO 38-4046995, NAIROBI, KENYA",
         "CONSIGNEE_INFO_COUNTRY": "KENYA", "CONSIGNEE_INFO_EORI": ""})}
    assert rows["CONSIGNEE_INFO_EORI"]["rule"] == "AIR_PULL"
    assert "空" in rows["CONSIGNEE_INFO_EORI"]["why"], rows["CONSIGNEE_INFO_EORI"]["why"]
    rows = {r["field"]: r for r in polish.preview(
        {"CONSIGNEE_INFO_COMP_NAME": "ACME",
         "CONSIGNEE_INFO_COMP_ADDRESS": "OLE OLE, NAIROBI",
         "CONSIGNEE_INFO_COUNTRY": "KE", "CONSIGNEE_INFO_EORI": "VAT 38-4046995"})}
    assert rows["CONSIGNEE_INFO_EORI"]["rule"] == "AIR_EORI", "号本来在这格里：那才是清洗那一档"


def test_preview_takes_the_country_from_the_same_party_only():
    fields = dict(BR)
    fields["SHIPPER_INFO_POSTAL"] = "04546040"        # 发货人在上海，这串不是巴西 CEP
    fields["SHIPPER_INFO_COUNTRY"] = "CHINA"
    fields["SHIPPER_INFO_STATE"] = "GUANGDONG"
    seen = {r["field"] for r in polish.preview(fields)}
    assert "CONSIGNEE_INFO_POSTAL" in seen and "SHIPPER_INFO_POSTAL" not in seen, \
        "发货人的邮编不许按收货人国家改：串主体就是把两家的值互换了"
    assert "SHIPPER_INFO_STATE" not in seen, "中国省没有两字母码，不动"
