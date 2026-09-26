# -*- coding: utf-8 -*-
"""V2 地基回归：web_bridge 填表计划（纯逻辑 dry-run）——不碰浏览器、不发请求。
判据来自 2026-09-26 CCSP 八票对格（docs/V2-ccsp-hawb-mapping.md）：主单拆分、HS 6 位口径、
电话去符号、城市码→机场码、平台库补值列进人工清单。"""
import json
from pathlib import Path

import web_bridge

AIR = {
    "MAWB_NO": "999-95764373", "HAWB_NO": "VCE4373",
    "ORIGIN_NAME": "BJS", "DEST_NAME": "VCE", "TO1": "VCE",
    "PIECES": 5, "SLAC": 5, "WEIGHT": 257.0,
    "GOODS_INFO": "Inflatable boat 135*60*41/5 VOL: 1.66 CBM", "GOODS_HS_CODE": "89031200",
    "SHIPPER_INFO_COMP_NAME": "QINGDAO HAIHAO YACHT CO.,LTD",
    "SHIPPER_INFO_COMP_ADDRESS": "ROOM 1201, ENTRANCE 1, BUILDING 145",
    "SHIPPER_INFO_CITY": "TAO", "SHIPPER_INFO_COUNTRY": "CN",
    "SHIPPER_INFO_STATE": "SHANDONG PROVINCE", "SHIPPER_INFO_POSTAL": "",
    "SHIPPER_INFO_TEL": "", "SHIPPER_INFO_FAX": "", "SHIPPER_INFO_TAX_ID": "91370214MA949PRM6F",
    "CONSIGNEE_INFO_COMP_NAME": "OZONE S.R.L",
    "CONSIGNEE_INFO_COMP_ADDRESS": "Viale delle Industrie 10",
    "CONSIGNEE_INFO_CITTY": "VENICE", "CONSIGNEE_INFO_COUNTRY": "IT",
    "CONSIGNEE_INFO_POSTAL": "31032", "CONSIGNEE_INFO_TEL": "+390422470376",
    "CONSIGNEE_INFO_TAX_ID": "",
}


def test_site_profile_loads_and_has_recon_facts():
    s = web_bridge.load_site()
    assert s["login_url"].startswith("http"), "站点档案要带登录入口"
    assert "editHAWBForm" in " ".join(s["notes"]), "详情表单名是侦察实据，丢了就等于要重新扒页面"
    assert s["field_map"]["SHIPPER_INFO"]["prefix"] == "hmr.hawb.shipper."
    assert "hmr.hawb.currency" in s["platform_only"], "平台独有控件要列出来给业务拍板"


def test_mawb_split_and_hs6_and_tel():
    assert web_bridge.split_mawb("999-95764373") == ("999", "95764373")
    assert web_bridge.split_mawb("99995764373") == ("999", "95764373")
    assert web_bridge.split_mawb("12-345") == ("", ""), "拆不开要给空串，让 needs_human 接手"
    assert web_bridge.hs6("89031200") == "890312", "平台口径 6 位：8 位中国申报码截前 6 位"
    assert web_bridge.hs6("901839,901831") == "901839,901831"
    assert web_bridge.norm_tel("+39 0422-470376") == "390422470376"


def test_city_to_airport_uses_table_and_passes_unknowns():
    table = web_bridge.load_site()["city_to_airport"]
    assert web_bridge.city_to_airport("BJS", table) == ("PEK", False)
    assert web_bridge.city_to_airport("YMQ", table) == ("YUL", False)
    assert web_bridge.city_to_airport("XXX", table) == ("XXX", False), \
        "未收录的码原样放行且不报警：多数本来就是机场码（VCE/MUC），逐条报警会淹没人工清单"


def test_build_fill_plan_maps_real_ticket():
    plan = web_bridge.build_fill_plan(AIR)
    v = plan["values"]
    assert v["hmr.mawb.awbPre"] == "999" and v["hmr.mawb.awbNo"] == "95764373"
    assert v["hmr.hawb.originAirport"] == "PEK", "BJS 是城市组码，平台要 PEK（8/8 张对格实证）"
    assert v["hmr.hawb.destAirport"] == "VCE"
    assert v["hmr.hawb.hsCode"] == "890312"
    assert v["hmr.hawb.grossWt"] == "257", "257.0 的浮点尾巴要去掉"
    assert v["hmr.hawb.consignee.tel"] == "390422470376", "去 + 号：平台样本无 +"
    assert v["hmr.hawb.shipper.customsCode"] == "91370214MA949PRM6F"


def test_build_fill_plan_lists_human_items_never_invents():
    plan = web_bridge.build_fill_plan(AIR)
    joined = "\n".join(plan["needs_human"])
    assert "SHIPPER_INFO_TEL 为空" in joined, "平台 tel 常从平台库带出，我方空=人工核对项，不是缺陷"
    assert "CONSIGNEE_INFO_TAX_ID 为空" in joined, "欧票 VAT 要人工核对（八票里五张漏过）"
    assert plan["platform_only"], "平台独有控件要原样透出给业务"
    assert not any(k.startswith("hmr.hawb.currency") for k in plan["values"]), \
        "platform_only 控件绝不能被自动填值"


def test_city_prefers_l2_original_over_l3_iata_code():
    """对格实证：我方 L3 的 city 是三字码（TAO），平台 city 栏要城市名（人工填 BEIJING 这类）。
    传了 raw（L2 原文口径）就用原文；只有 L3 时保留三字码但必须进人工清单。"""
    plan = web_bridge.build_fill_plan(AIR, raw={"SHIPPER_INFO_CITY": "QINGDAO",
                                                "CONSIGNEE_INFO_CITTY": "VENICE"})
    assert plan["values"]["hmr.hawb.shipper.city"] == "QINGDAO"
    assert plan["values"]["hmr.hawb.consignee.city"] == "VENICE"
    assert not any("city" in n for n in plan["needs_human"]), "原文口径可用时不再烦人工"
    plan2 = web_bridge.build_fill_plan(AIR)
    assert plan2["values"]["hmr.hawb.shipper.city"] == "TAO"
    assert any("SHIPPER_INFO_CITY" in n and "三字码" in n for n in plan2["needs_human"])


def test_dry_run_never_touches_network():
    src = Path(web_bridge.__file__).read_text(encoding="utf-8")
    assert "requests" not in src and "urllib" not in src and "OpenAI" not in src and \
        "playwright" not in src.lower(), "web_bridge 当前只许纯逻辑；接浏览器是下一个里程碑，要单独立项过审"
