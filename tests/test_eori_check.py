# -*- coding: utf-8 -*-
"""EORI 欧盟官网核验（2026-11-14 方案 1C）回归：判形收集、缓存语义、旗口径、查不通不挂旗。
全程不碰真网络：urlopen 换成假的；缓存与质检目录指到 tmp，测完还原。"""
import datetime
import json
import shutil
import tempfile
import urllib.request
from pathlib import Path

import config
import eori_check

D_NOW = datetime.datetime.now()
_ORIG = {}


def _setup(tmp):
    _ORIG.update(cache=eori_check.CACHE_PATH, cache_mem=eori_check._CACHE,
                 qc=config.OUTPUT_QC_DIR)
    eori_check.CACHE_PATH = Path(tmp) / "eori_cache.json"
    eori_check._CACHE = {}
    config.OUTPUT_QC_DIR = Path(tmp)
    return Path(tmp)


def _teardown():
    eori_check.CACHE_PATH = _ORIG["cache"]
    eori_check._CACHE = _ORIG["cache_mem"]
    config.OUTPUT_QC_DIR = _ORIG["qc"]


class _Resp:
    def __init__(self, body):
        self._b = body.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eori"
# 真页面（2026-11-14 实抓）：结论端点 eori_detail.jsp 1:1 干净；表单页没有结论、
# 只有 JS 注释陷阱（3 处 "is not valid"，v1.39.0 打错端点就是栽在这上面）
PAGES = {
    "SE5563646560": FIXTURES.joinpath("valid.html").read_text(encoding="utf-8"),
    "IT03268900267": FIXTURES.joinpath("valid_it.html").read_text(encoding="utf-8"),
    "SE00000000000": FIXTURES.joinpath("invalid.html").read_text(encoding="utf-8"),
}


def _fake_urlopen(pages=None):
    """按查询号回真夹具（pages 可覆盖个别号）；不在表里的号抛异常=网络不通。
    记下每一次被请求的 URL，供"只许打结论端点"那条契约断言。"""
    requested = []

    def fake(req, timeout=None, context=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        requested.append(url)
        num = url.split("EoriNumb=")[-1].split("&")[0]
        body = pages.get(num) if pages else None
        if body is None:
            body = PAGES.get(num)
        if body is None:
            raise OSError("network unreachable")
        return _Resp(body)

    eori_check.urllib.request.urlopen = fake
    return requested


def test_numbers_picks_eori_shaped_only_and_skips_gb_and_tax():
    d = {"SHIPPER_INFO_EORI": "IT03268900267 / USCI911201117706402073",
         "CONSIGNEE_INFO_EORI": "VAT SE5563646560",
         "NOTIFY_INFO_EORI": "GB123456789000",
         "SHIPPER_INFO_TAX": "SE5563646560"}
    got = eori_check.numbers(d)
    assert got == {"SHIPPER_INFO_EORI": ["IT03268900267"],
                   "CONSIGNEE_INFO_EORI": ["SE5563646560"]}, got
    assert "GB123456789000" not in got.get("NOTIFY_INFO_EORI", []), "GB 号不在欧盟系统，查它必回假无效"
    assert "SE5563646560" not in got.get("SHIPPER_INFO_EORI", []), "TAX 列不查"


def test_check_one_hits_the_verdict_endpoint_not_the_form_page():
    """v1.39.0 的错就是打错端点：表单页 eori_validation.jsp 里**没有结论**，唯一像结论的
    "is not valid" 三处全是 JS 注释（"//The day is not valid"）——拿它判无效，可达机器上
    每个号都会被判无效（当晚 SE5563646560 这个瑞典有效号就被判了"无效"）。这条钉死：
    只许打 eori_detail.jsp，真夹具必须判出真结论。"""
    old = eori_check.urllib.request.urlopen
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eori_"))
    try:
        _setup(tmp)
        requested = _fake_urlopen()
        assert eori_check.check_one("SE5563646560") == "valid", "真页面：瑞典有效号要判 valid"
        assert eori_check.check_one("IT03268900267") == "valid"
        assert eori_check.check_one("SE00000000000") == "invalid", "真页面：假号要判 invalid"
        assert requested and all("eori_detail.jsp" in u for u in requested), requested
        assert not any("eori_validation.jsp" in u for u in requested), \
            "表单页是陷阱页（JS 注释里有 is not valid），一个字节都不许再打它"
    finally:
        eori_check.urllib.request.urlopen = old
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_form_page_trap_body_can_not_produce_a_verdict():
    """就算有人把端点改回去，让真表单页进到判词里也要现出原形：判词对表单页正文
    必须**认不出结论**（表单页里 "is not valid" 是 JS 注释不是结论），回 unreachable
    ——宁可中性，不可把有效号判死。"""
    old = eori_check.urllib.request.urlopen
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eori_"))
    trap = FIXTURES.joinpath("form_page_trap.html").read_text(encoding="utf-8")
    try:
        _setup(tmp)
        _fake_urlopen(pages={"SE5563646560": trap})   # 伪造"端点回成表单页"
        assert eori_check.check_one("SE5563646560") == "unreachable", \
            "表单页正文不许判出 valid/invalid——判词只认结论区"
    finally:
        eori_check.urllib.request.urlopen = old
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_check_one_reads_the_page_verdict():
    old = eori_check.urllib.request.urlopen
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eori_"))
    try:
        _setup(tmp)   # check_one 会把结论写进缓存（含熔断计数）：不指开就污染真 output/
        junk = "<html>something else entirely</html>"
        _fake_urlopen({"NL0000000000": junk})
        assert eori_check.check_one("NL0000000000") == "unreachable", "页面认不出结论不算 invalid"
        assert eori_check.check_one("FI0000000000") == "unreachable", "连不上不算 invalid"
    finally:
        eori_check.urllib.request.urlopen = old
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_cached_flags_only_fire_on_invalid():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eori_"))
    try:
        _setup(tmp)
        hour_ago = (D_NOW - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        eori_check._CACHE.update({
            "IT03268900267": {"status": "invalid", "checked_at": hour_ago},
            "SE5563646560": {"status": "valid", "checked_at": hour_ago},
            "FI0000000000": {"status": "unreachable", "checked_at": hour_ago},
            "NL0000000000": {"status": "unreachable",
                             "checked_at": (D_NOW - datetime.timedelta(hours=2)).isoformat(timespec="seconds")},
        })
        d = {"SHIPPER_INFO_EORI": "IT03268900267 / SE5563646560",
             "CONSIGNEE_INFO_EORI": "FI0000000000 / NL0000000000"}
        flags = eori_check.cached_flags(d)
        assert len(flags) == 1 and flags[0].startswith("SHIPPER_INFO_EORI") \
            and "IT03268900267" in flags[0], flags
        assert not eori_check.cached_flags({"SHIPPER_INFO_EORI": "SE5563646560"}), "valid 不挂旗"
        assert not eori_check.cached_flags({"CONSIGNEE_INFO_EORI": "FI0000000000"}), "查不通不挂旗（中性）"
    finally:
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_check_cells_uses_cache_until_forced():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eori_"))
    old = eori_check.urllib.request.urlopen
    try:
        _setup(tmp)
        hour_ago = (D_NOW - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        eori_check._CACHE.update({"IT03268900267": {"status": "valid", "checked_at": hour_ago}})
        # 未覆盖的号回真夹具（SE5563646560 真页=valid）
        _fake_urlopen()
        d = {"SHIPPER_INFO_EORI": "IT03268900267 / SE5563646560"}
        res = eori_check.check_cells(d)
        assert res["SHIPPER_INFO_EORI"] == {"IT03268900267": "valid", "SE5563646560": "valid"}, res
        assert (tmp / "eori_cache.json").exists(), "查过的号要进缓存"

        # force=True：绕过缓存、结果覆盖缓存，invalid 翻案成 valid 后旗就安静
        # （IT 的真夹具正文本来就是 valid——拿真页面做"翻案"，不用编造文本）
        _fake_urlopen(pages={})
        res2 = eori_check.check_cells(d, force=True)
        assert res2["SHIPPER_INFO_EORI"]["IT03268900267"] == "valid"
        assert not eori_check.cached_flags({"SHIPPER_INFO_EORI": "IT03268900267"})

        # 翻回 invalid 后手动重查要立刻挂旗（用假号的真夹具正文）
        _fake_urlopen(pages={"IT03268900267": PAGES["SE00000000000"]})
        eori_check.check_cells({"SHIPPER_INFO_EORI": "IT03268900267"}, force=True)
        assert eori_check.cached_flags({"SHIPPER_INFO_EORI": "IT03268900267"})
    finally:
        eori_check.urllib.request.urlopen = old
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_append_qc_flags_merges_and_survives_missing_record():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eori_"))
    try:
        _setup(tmp)
        stem = "eoriqc1"
        qc = {"flags": ["已有旗"], "needs_review": True}
        (tmp / f"{stem}.json").write_text(json.dumps(qc, ensure_ascii=False), encoding="utf-8")
        eori_check.append_qc_flags(stem, ["SHIPPER_INFO_EORI EORI 官网核验不通过: IT03268900267"])
        got = json.loads((tmp / f"{stem}.json").read_text(encoding="utf-8"))
        assert got["flags"] == ["已有旗", "SHIPPER_INFO_EORI EORI 官网核验不通过: IT03268900267"]
        eori_check.append_qc_flags(stem, ["SHIPPER_INFO_EORI EORI 官网核验不通过: IT03268900267"])
        got2 = json.loads((tmp / f"{stem}.json").read_text(encoding="utf-8"))
        assert len(got2["flags"]) == 2, "重复查不重复挂"
        eori_check.append_qc_flags("没有质检记录的票", ["旗"])
        assert not (tmp / "没有质检记录的票.json").exists(), "不硬造质检记录"
    finally:
        _teardown()
        shutil.rmtree(tmp, ignore_errors=True)
