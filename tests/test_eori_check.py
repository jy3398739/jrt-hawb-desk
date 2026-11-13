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


def _fake_urlopen(pages):
    """pages: {号: 正文}；不在表里的号抛异常=网络不通。"""
    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        num = url.split("EoriNumb=")[-1].split("&")[0]
        if num not in pages:
            raise OSError("network unreachable")
        return _Resp(pages[num])
    eori_check.urllib.request.urlopen = fake
    return fake


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


def test_check_one_reads_the_page_verdict():
    old = eori_check.urllib.request.urlopen
    tmp = Path(tempfile.mkdtemp(prefix="hawb_eori_"))
    ok = "The EORI number IT03268900267 is a Valid EORI number. Details follow."
    try:
        _setup(tmp)   # check_one 会把结论写进缓存（含熔断计数）：不指开就污染真 output/
        _fake_urlopen({"IT03268900267": ok, "SE0000000000": "is not valid. Please check.",
                       "NL0000000000": "<html>something else entirely</html>"})
        assert eori_check.check_one("IT03268900267") == "valid"
        assert eori_check.check_one("SE0000000000") == "invalid"
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
        _fake_urlopen({"SE5563646560": "Valid EORI number here."})
        d = {"SHIPPER_INFO_EORI": "IT03268900267 / SE5563646560"}
        res = eori_check.check_cells(d)
        assert res["SHIPPER_INFO_EORI"] == {"IT03268900267": "valid", "SE5563646560": "valid"}, res
        assert (tmp / "eori_cache.json").exists(), "查过的号要进缓存"

        # force=True：绕过缓存、结果覆盖缓存，invalid 翻案成 valid 后旗就安静
        _fake_urlopen({"IT03268900267": "is a Valid EORI number."})
        res2 = eori_check.check_cells(d, force=True)
        assert res2["SHIPPER_INFO_EORI"]["IT03268900267"] == "valid"
        assert not eori_check.cached_flags({"SHIPPER_INFO_EORI": "IT03268900267"})

        # 翻回 invalid 后手动重查要立刻挂旗
        _fake_urlopen({"IT03268900267": "is not valid."})
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
