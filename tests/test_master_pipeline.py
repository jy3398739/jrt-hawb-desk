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

    def fake(transcript, **_kw):
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


def test_public_exposes_group_labels_and_readonly_meta():
    """分组名由后端给（前端抄一份迟早漂移）；JOB_ID 这类"主单多出来但不参与提交"的列
    以只读 meta 形式进基础信息，让人看得见、也不会被当成可提交列发出去。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        rec = mp.public(mp.run_master("176-62400004", dict(ORDER, JOB_ID=40366281)))
        assert rec["groups"]["base"] == "基础信息" and "notify" not in rec["groups"]
        assert rec["meta"] == [{"col": "JOB_ID", "label": "公司任务号（只读，不提交）",
                                "value": 40366281, "group": "base"}]
        assert "JOB_ID" not in rec["ams"], "只读列绝不进可提交面"
        assert not any(l["text"].startswith("JOB_ID") for l in rec["transcript"]["lines"]), \
            "JOB_ID 不进 L1：模型没有这一列，给了只会诱发它编"
    finally:
        undo()
        _restore(old, tmp)


def test_missed_ams_column_is_named_as_a_red_flag():
    """公司 AMS_RECORD 里明明有值、模型却没取 → 报"漏取"并进提交门。
    不区分"资料里没有"和"有但没提出来"，复核人只能对着一堆空格子猜；而整表写回会把
    这列写成 NULL，等于用一次提交把公司库里已有的值抹掉。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    thin = {c: None for c in mp.MASTER_COLS}
    thin.update({"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901"})   # 故意漏掉发货人那一列
    box, undo = _stub_extract(reply=thin)
    order = dict(ORDER, AMS_RECORD=dict(ORDER["AMS_RECORD"],
                                        SHIPPER_INFO_COMP_NAME="METSO (TIANJIN) INVESTMENT CO., LTD."))
    try:
        rec = mp.run_master("176-62400004", order)
        flags = "\n".join(rec["qc"]["flags"])
        assert "漏取" in flags and "SHIPPER_INFO_COMP_NAME" in flags, rec["qc"]["flags"]
        assert "SHIPPER_INFO_COMP_NAME" in rec["qc"]["missed_cols"]
        assert "NOTIFY_INFO_EMAIL" not in rec["qc"]["missed_cols"], "资料里本来就没有，不该算漏取"
        assert rec["qc"]["needs_review"] is True, "漏取必须卡提交门"
        # 提交门是服务端按当前值重算的，所以漏取也必须出现在 master_flags 里
        again = mp.master_flags({"MAWB_NO": "176-62400004"}, rec["transcript"])
        assert any("漏取" in f and "SHIPPER_INFO_COMP_NAME" in f for f in again), again
    finally:
        undo()
        _restore(old, tmp)


def test_cache_invalidates_when_prompt_or_model_changes():
    """2026-09-29 用户实撞：改了提示词/换了模型，旧缓存还在投喂过期结果（页面显示 5/36，
    公司侧实有 21 列）。所以命中条件除资料指纹外还要看"提示词+列面"指纹和当时用的模型。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    old_prompt, old_model = vlm_extract.MASTER_PROMPT_HEAD, config.MASTER_VLM_MODEL
    try:
        assert mp.ensure("176-62400004", ORDER)["state"] == "done" and len(box) == 1
        assert mp.ensure("176-62400004", ORDER)["state"] == "done" and len(box) == 1, "什么都没改不该重跑"

        vlm_extract.MASTER_PROMPT_HEAD = old_prompt + "\n⑥ 新增一条口径\n"
        mp.ensure("176-62400004", ORDER)
        assert len(box) == 2, "提示词变了必须重解析"

        vlm_extract.MASTER_PROMPT_HEAD = old_prompt
        config.MASTER_VLM_MODEL = ""          # 换渠道（模型 id 变了）同样作废
        mp.ensure("176-62400004", ORDER)
        assert len(box) == 3, "换模型后必须重解析，否则对比不了、也看不到新模型的效果"
    finally:
        vlm_extract.MASTER_PROMPT_HEAD, config.MASTER_VLM_MODEL = old_prompt, old_model
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

    def boom(transcript, **_kw):
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

    def slow(transcript, **_kw):
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
