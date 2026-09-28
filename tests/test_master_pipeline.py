# -*- coding: utf-8 -*-
"""主单解析管道 + 回传公司（mawb2）回归：模型与 HTTP 全部打桩，绝不真调、绝不出网。

盯的是四件事：缓存按资料指纹失效（公司改了要重跑、没改不再花钱）、红旗门在服务端重算、
锁行绝不发出写请求、主单台账不污染分单台账。
"""
import json
import threading
from pathlib import Path

import company_api
import config
import master_pipeline as mp
import vlm_extract

AMS_OUT = {"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901", "SLAC": 10}
ORDER = {"MASTER_NO": "176-62400004",
         "SHIPPER_INFO": "METSO (TIANJIN) INVESTMENT CO., LTD. CHINA",
         "GOODS_NAME": "PISTON ROD",
         "AMS_RECORD": dict(AMS_OUT, HMY_ID=12, SEND_STATUS=0, CREATE_TIME="2026-09-20 09:12:00")}


def _use_tmp(tmp: Path):
    """把主单缓存目录与两份台账指到临时目录，返回旧值。"""
    old = (config.MASTER_DIR, config.MASTER_LEDGER, config.SUBMIT_LEDGER)
    config.MASTER_DIR = tmp / "master"
    config.MASTER_LEDGER = tmp / "master_submitted.json"
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    return old


def _restore(old, tmp):
    config.MASTER_DIR, config.MASTER_LEDGER, config.SUBMIT_LEDGER = old
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _stub_extract(reply=None, calls=None):
    """把主单 L2 换成假实现：不发网络，记录被调用了几次。"""
    real = vlm_extract.extract_master
    box = calls if calls is not None else []

    def fake(transcript):
        box.append(1)
        data = {c: None for c in mp.MASTER_COLS}
        data.update(reply if reply is not None else dict(AMS_OUT,
                                                       SHIPPER_INFO_COMP_NAME="METSO (TIANJIN) INVESTMENT CO., LTD.",
                                                       SHIPPER_INFO_COUNTRY="CN"))
        return data

    vlm_extract.extract_master = fake
    return box, (lambda: setattr(vlm_extract, "extract_master", real))


def test_run_master_produces_clean_editable_record():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        rec = mp.run_master("176-62400004", ORDER)
        assert rec["state"] == "done", rec
        assert rec["model"] == config.VLM_MODEL
        assert rec["ams"]["MAWB_NO"] == "176-62400004"
        assert rec["ams"]["HMY_ID"] is None if "HMY_ID" in rec["ams"] else True, "服务端列不进可编辑面"
        assert rec["qc"]["flags"] == [], rec["qc"]
        assert rec["qc"]["fidelity"]["checked"] >= 2, "解析值要能逐字回查到公司资料原文"
        assert len(rec["transcript"]["lines"]) >= 4
        assert Path(rec["path"]).is_file(), "结果落盘，第二次打开不再花钱"
        assert box == [1]
    finally:
        undo()
        _restore(old, tmp)


def test_cache_hit_skips_the_model_and_revalidates_on_new_text():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        first = mp.ensure("176-62400004", ORDER)
        assert first["state"] == "done" and len(box) == 1
        again = mp.ensure("176-62400004", ORDER)
        assert len(box) == 1, "资料没变就该用缓存，不该再调一次模型"
        assert again["text_md5"] == first["text_md5"]
        changed = dict(ORDER, GOODS_NAME="PISTON ROD AND SEALS")
        mp.ensure("176-62400004", changed)
        assert len(box) == 2, "公司资料变了必须重跑，否则核对的是过期值"
    finally:
        undo()
        _restore(old, tmp)


def test_empty_master_text_fails_without_calling_the_model():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        rec = mp.ensure("176-62400004", {})          # mock 模式就是空壳
        assert rec["state"] == "failed" and "空" in rec["error"]
        assert box == [], "没有资料还去调模型，等于让模型凭空编一条主单"
    finally:
        undo()
        _restore(old, tmp)


def test_extract_failure_is_recorded_not_swallowed():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    real = vlm_extract.extract_master

    def boom(transcript):
        raise RuntimeError("模型无响应")

    vlm_extract.extract_master = boom
    try:
        rec = mp.run_master("176-62400004", ORDER)
        assert rec["state"] == "failed" and "模型无响应" in rec["error"], rec
        assert rec["qc"]["needs_review"] is True, "失败的解析更要留痕，别让它在页面上隐身"
    finally:
        vlm_extract.extract_master = real
        _restore(old, tmp)


def test_inflight_master_is_not_parsed_twice():
    """两个请求同时到（前端重进页面、或两个人查同一主单）只该花一次调用。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    gate = threading.Event()
    real = vlm_extract.extract_master

    def slow(transcript):
        gate.wait(3)
        return real(transcript)

    vlm_extract.extract_master = slow
    try:
        t1 = threading.Thread(target=lambda: mp.ensure("176-62400004", ORDER))
        t1.start()
        import time
        for _ in range(100):
            rec = mp.read_master("176-62400004")
            if rec and rec["state"] == "parsing":
                break
            time.sleep(0.02)
        assert rec and rec["state"] == "parsing", "在跑的时候状态要如实报 parsing"
        second = mp.ensure("176-62400004", ORDER)
        assert second["state"] == "parsing"
        gate.set()
        t1.join(5)
        assert len(box) == 1, "第二个请求不该再起一次解析"
        assert mp.read_master("176-62400004")["state"] == "done"
    finally:
        undo()
        _restore(old, tmp)


def _patch_j9(sent, master_rows):
    real = company_api._j9_post

    def fake(path, body, key):
        sent.append({"path": path, "body": body, "key": key})
        if path.endswith("/mawb/"):
            return {"code": 0, "data": master_rows}
        if path.endswith("/mawb2/"):
            return {"code": 0, "success": True, "action": "update", "rows": 1}
        raise AssertionError("未知路径 " + path)

    company_api._j9_post = fake
    return lambda: setattr(company_api, "_j9_post", real)


def _live():
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    return old


def test_submit_master_reads_then_sends_only_the_36_columns():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    sent = []
    undo_cfg = _live()
    restore = _patch_j9(sent, [{"MASTER_NO": "176-62400004",
                               "AMS_RECORD": dict(AMS_OUT, HMY_ID=12, SEND_STATUS=2,
                                                  CONSIGNEE_INFO_COMP_NAME="OLD NAME")}])
    try:
        out = mp.submit_master({"mawb": "176-62400004", "reviewer": "马殿齐",
                                "ams": dict(AMS_OUT, SLAC=12)})
        assert out["accepted"] is True
        assert len(sent) == 2 and sent[0]["path"].endswith("/mawb/") and sent[0]["key"] == "km"
        body = sent[1]["body"]["AMS_RECORD"]
        assert set(body) <= set(mp.MASTER_COLS), "提交体只能有主单那 36 列"
        assert body["SLAC"] == 12, "人工改过的值覆盖库里的旧值"
        assert body["CONSIGNEE_INFO_COMP_NAME"] == "OLD NAME", \
            "整表写回：读到的列这次没给值也要原样带上，否则公司把 NULL 写进去抹掉旧值"
        assert "SEND_STATUS" not in body and "HMY_ID" not in body and "CREATE_TIME" not in body
        led = json.loads((tmp / "master_submitted.json").read_text(encoding="utf-8"))
        assert led["17662400004"]["reviewer"] == "马殿齐" and led["17662400004"]["action"] == "update"
        assert not (tmp / "submitted.json").exists(), "主单提交不写分单台账"
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = undo_cfg
        _restore(old, tmp)


def test_submit_master_refuses_locked_record_without_writing():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    sent = []
    undo_cfg = _live()
    restore = _patch_j9(sent, [{"MASTER_NO": "176-62400004",
                               "AMS_RECORD": dict(AMS_OUT, SEND_STATUS=1)}])
    try:
        try:
            mp.submit_master({"mawb": "176-62400004", "reviewer": "马殿齐", "ams": dict(AMS_OUT)})
            raise AssertionError("SEND_STATUS=1 的主单居然放行了")
        except company_api.CompanyLocked as e:
            assert "SEND_STATUS" in str(e)
        assert len(sent) == 1, "闸门拦下时绝不能发出 mawb2 写请求"
        assert not (tmp / "master_submitted.json").exists()
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = undo_cfg
        _restore(old, tmp)


def test_submit_master_gate_blocks_uncleared_flags():
    """红旗门在服务端重算：前端把红旗藏了也提不出去；逐条「确认无误」留痕后才放行。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    sent = []
    undo_cfg = _live()
    restore = _patch_j9(sent, [{"MASTER_NO": "176-62400004", "AMS_RECORD": None}])
    bad = dict(AMS_OUT, MAWB_NO="176-6240004", SHIPPER_INFO_POSTAL="x" * 60)
    try:
        try:
            mp.submit_master({"mawb": "176-62400004", "reviewer": "马殿齐", "ams": bad})
            raise AssertionError("带红旗的主单居然提了")
        except mp.MasterFlagged as e:
            assert "MAWB_NO" in str(e) and "SHIPPER_INFO_POSTAL" in str(e)
        assert sent == [], "被门拦下时一次请求都不该发出去"
        flags = mp.master_flags(bad)
        out = mp.submit_master({"mawb": "176-62400004", "reviewer": "马殿齐", "ams": bad,
                                "acked_flags": flags})
        assert out["accepted"] is True and len(sent) == 2
        led = json.loads((tmp / "master_submitted.json").read_text(encoding="utf-8"))
        assert len(led["17662400004"]["acked_flags"]) == len(flags), "确认无误要留痕"
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = undo_cfg
        _restore(old, tmp)


def test_submit_master_needs_reviewer_name():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    try:
        try:
            mp.submit_master({"mawb": "176-62400004", "reviewer": "  ", "ams": dict(AMS_OUT)})
            raise AssertionError("没署名也能提")
        except mp.MasterFlagged as e:
            assert "复核人" in str(e) or "署名" in str(e)
    finally:
        _restore(old, tmp)
