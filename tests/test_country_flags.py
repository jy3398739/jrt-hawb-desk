# -*- coding: utf-8 -*-
"""按国别的格式红旗（只挂旗、绝不改值）——与逐格优化共用同一张国别表。

分工是刻意的：`polish` 只回答"这一格能不能由现值唯一推出新值"，推不出来就什么也不做；
这些旗回答的是"这一格看着与目的国的口径不合，请核票"。**两边读同一张表**，
不会出现"规则说补横杠、旗说位数不对"这种自相矛盾（分单与主单同一条铁律，见 §4 留档）。

三条边界，用例逐条钉：
① 只报不改——旗里说的是"核票"，不是"已修正"；
② 表外国家一律闭嘴（阿联酋/卡塔尔本来就没有街道邮编，中国省没有两字母码），
   宁可漏报也不给制单员造噪声；
③ 旗面开头必须是列名，审核台按列名把旗挂到那一格上（少写列名就成了没人能确认的死旗）。
"""
import validator


def _c(**kw):
    """一收货人搭好的分单 L2 那份（其余列留空，让旧判据不干扰本次断言）。"""
    d = {"MAWB_NO": "999-95764373", "HAWB_NO": "JRT001", "TO1": "GRU", "DEST_NAME": "GRU",
         "ORIGIN_NAME": "PVG", "PIECES": 1, "WEIGHT": 10, "CREATE_TIME": "2026-10-20",
         "CONSIGNEE_INFO_COUNTRY": "BR", "CONSIGNEE_INFO_POSTAL": "04546-040"}
    d.update(kw)
    return d


def _hit(warns, col):
    return [w for w in warns if w.startswith(col)]


# ── 邮编位数 ────────────────────────────────────────────────────────────────────
def test_postal_length_flag_only_when_it_clearly_does_not_fit_the_country():
    warns = validator.validate_raw(_c(CONSIGNEE_INFO_POSTAL="045460401"))
    assert _hit(warns, "CONSIGNEE_INFO_POSTAL"), "巴西 CEP 给了 9 位，要出声"
    assert "核票" in _hit(warns, "CONSIGNEE_INFO_POSTAL")[0], "旗要说的是人去核，不是系统已改"
    assert not _hit(validator.validate_raw(_c()), "CONSIGNEE_INFO_POSTAL"), "8 位合度：不该有旗"
    assert not _hit(validator.validate_raw(_c(CONSIGNEE_INFO_POSTAL="SE-114 33",
                                              CONSIGNEE_INFO_COUNTRY="SE")),
                    "CONSIGNEE_INFO_POSTAL"), "带国别前缀的写法先剥前缀再量位数"


def test_postal_flag_stays_quiet_for_countries_we_do_not_have_a_shape_for():
    for cc, val in (("AE", ""), ("QA", "12345"), ("TW", "123456"), ("XX", "1")):
        warns = validator.validate_raw(_c(CONSIGNEE_INFO_COUNTRY=cc, CONSIGNEE_INFO_POSTAL=val))
        assert not _hit(warns, "CONSIGNEE_INFO_POSTAL"), f"{cc} 不在国别表里，闭嘴"


# ── 识别号那一格：一条国别旗都不许挂（真夹具咬过的坑，写成守卫挡回来） ──────────
def test_the_id_cell_takes_no_country_shape_flag_because_it_holds_foreign_numbers():
    """`*_INFO_EORI` 从 v1.12.0 起同时装海关号与各国本土税号，按国别量位数会把对的挡掉。

    2026-10-20 加过一版"号与该国形状不符"，全量回归当场被三张真夹具咬红：意大利收货人给
    中国 USCI、中国发货人那格印成 `OCI/CN/SHP/T/USCI91310106MA1FYJ8Q3H`，两条都是票面照抄的
    正解。`_id_cell_warns` 的注释里本来就写着同一个结论——这条是把那次回退钉住，别再加回来。
    """
    import polish
    for value, cc in (("911201117706402073", "IT"),
                      ("GB123456789012", "BR"),
                      ("OCI/CN/SHP/T/USCI91310106MA1FYJ8Q3H", "CN"),
                      ("00280273000137 / 12345678901", "BR")):
        assert polish.shape_warns("CONSIGNEE_INFO_EORI", value, cc) == [], \
            f"{cc} 主体的 {value!r} 不该被国别旗拦"
        warns = validator.validate_air({"CONSIGNEE_INFO_COUNTRY": cc, "CONSIGNEE_INFO_EORI": value})
        assert not [w for w in warns if "应是" in w or "常见形状" in w], warns


def test_postal_flag_stays_quiet_when_the_cell_holds_more_than_a_code():
    assert not _hit(validator.validate_raw(_c(CONSIGNEE_INFO_COUNTRY="SI",
                                              CONSIGNEE_INFO_POSTAL="1000 LJUBLJANA")),
                    "CONSIGNEE_INFO_POSTAL"), "那一格里混了城市名：量出来的位数不可信，闭嘴"
    assert _hit(validator.validate_raw(_c(CONSIGNEE_INFO_COUNTRY="CA",
                                          CONSIGNEE_INFO_POSTAL="M5V3A")),
                "CONSIGNEE_INFO_POSTAL"), "加拿大邮编本来就含字母，缺一位要报"


# ── HS 位数（ICS2 起 6 位、多数目的国 8-10 位） ─────────────────────────────────
def test_hs_below_six_digits_is_flagged_on_both_chains():
    warns = validator.validate_raw(_c(GOODS_HS_CODE="8903"))
    assert _hit(warns, "GOODS_HS_CODE"), "4 位在 ICS2 都不够，要出声"
    assert not _hit(validator.validate_raw(_c(GOODS_HS_CODE="89031200")), "GOODS_HS_CODE")
    m = validator.validate_master({"MAWB_NO": "999-95764373", "GOODS_INFO_HSCODE": "89031200,"
                                                                                    " 89039900"})
    assert not _hit(m, "GOODS_INFO_HSCODE"), "逗号分隔的两个 8 位号都合"
    m = validator.validate_master({"MAWB_NO": "999-95764373", "GOODS_INFO_HSCODE": "8903,89031200"})
    assert _hit(m, "GOODS_INFO_HSCODE"), "多段里有一段不够也要报出来"


# ── 国家那一格：既不是两字母码也不在码表里 ──────────────────────────────────────
def test_unrecognised_country_is_flagged_because_it_goes_out_untouched():
    assert _hit(validator.validate_raw(_c(CONSIGNEE_INFO_COUNTRY="BRAZIL")),
                "CONSIGNEE_INFO_COUNTRY") == [], "全称能认：分单提交时本来会转 BR"
    warns = validator.validate_raw(_c(CONSIGNEE_INFO_COUNTRY="BRAZILIA"))
    assert _hit(warns, "CONSIGNEE_INFO_COUNTRY"), "认错的国家会被原样发出去，必须报"
    m = validator.validate_master({"MAWB_NO": "999-95764373",
                                   "NOTIFY_INFO_COUNTRY": "BRAZILIA"})
    assert _hit(m, "NOTIFY_INFO_COUNTRY"), "主单三个主体都要判，不是只判收货人"


def test_master_chain_gets_the_same_country_flags_as_hawb():
    fields = {"MAWB_NO": "999-95764373", "SHIPPER_INFO_COUNTRY": "DE",
              "SHIPPER_INFO_POSTAL": "1234", "NOTIFY_INFO_COUNTRY": "BRAZILIA"}
    m = validator.validate_master(fields)
    assert _hit(m, "SHIPPER_INFO_POSTAL"), "德国邮编 5 位，4 位要报"
    assert _hit(m, "NOTIFY_INFO_COUNTRY"), "主单三个主体都要判，不是只判收货人"
    assert not _hit(validator.validate_master({"MAWB_NO": "999-95764373",
                                               "SHIPPER_INFO_COUNTRY": "JP",
                                               "SHIPPER_INFO_POSTAL": "100-0001"}),
                    "SHIPPER_INFO_POSTAL")
