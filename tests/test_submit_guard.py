# -*- coding: utf-8 -*-
"""P0 第 1 步：预读没读到就不再整表写回 + 提交幂等锁。全程打桩，绝不真调公司接口。

背景（2026-09-29 审计）：j9 的 hawb2/mawb2 是**整表写回**——提交体里没给的列会被写成 NULL。
原实现在"读不到公司当前行"时把 existing 当空表继续发，等于一次提交把那一行其余列清空；
而"读不到"和"公司确实没有这行（首次录入）"当时是同一个分支。
"""
import json
import shutil
import tempfile
from pathlib import Path

import company_api
import config
import store


def _iso(tmp):
    """把两个提交台账与幂等锁目录都指到临时目录，返回旧值供恢复。"""
    old = (config.SUBMIT_LEDGER, config.MASTER_LEDGER, config.SUBMIT_GUARD_DIR,
           config.COMPANY_API_MODE, config.COMPANY_API_URL,
           config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY)
    config.SUBMIT_LEDGER, config.MASTER_LEDGER = tmp / "submitted.json", tmp / "master_submitted.json"
    config.SUBMIT_GUARD_DIR = tmp / "submit_guard"
    config.COMPANY_API_MODE, config.COMPANY_API_URL = "live", "http://j9.test:18080"
    config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = "km", "kh"
    return old


def _restore(old):
    (config.SUBMIT_LEDGER, config.MASTER_LEDGER, config.SUBMIT_GUARD_DIR,
     config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY,
     config.COMPANY_HAWB_KEY) = old


HOUSE_ROW = {
    "HAWB_ID": 448, "MAWB_NO": "020-18134281", "HAWB_NO": "NOAIRWAW050582",
    "ORGIN_NAME": "BEIJING", "DEST_NAME": "WARSAW", "TO1": "WAW", "PIECES": 12,
    "WEIGHT": 170.0, "SEND_STATUS": 0,
    "SHIPPER_INFO": "['DONGGUAN OREN']", "CONSIGNEE_INFO": "['BKT ELEKTRONIK']",
    "SHIPPER_INFO_TEL": "18575297929", "CONSIGNEE_INFO_EORI": "PL554289446200000",
}


def _stub(sent, rows):
    """按调用顺序回放：读接口回 rows，写接口记下来并回成功。返回恢复函数。"""
    real = company_api._j9_post

    def fake(path, body, key):
        sent.append({"path": path, "body": body})
        if path.endswith("/hawb"):
            return {"code": 0, "data": rows()}
        if path.endswith("/hawb2"):
            return {"code": 0, "success": True, "action": "insert", "rows": 1}
        if path.endswith("/mawb/"):
            return {"code": 0, "data": rows()}
        if path.endswith("/mawb2/"):
            return {"code": 0, "success": True, "action": "update", "rows": 1}
        raise AssertionError("未知路径 " + path)

    company_api._j9_post = fake

    def undo():
        company_api._j9_post = real
    return undo


def _payload():
    return {"mawb": "020-18134281", "hawb": "NOAIRWAW050582", "stem": "T1",
            "air": {"MAWB_NO": "020-18134281", "HAWB_NO": "NOAIRWAW050582",
                    "ORIGIN_NAME": "BJS", "DEST_NAME": "WAW"}}


def test_house_submit_refuses_when_the_row_should_already_exist():
    """本机台账里这张票提交过 = 公司一定有这一行；这时读回空只能当"没读到"，
    绝不能继续写——整表写回会把那一行其余列全抹成 NULL。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_p0_"))
    old = _iso(tmp)
    sent = []
    undo = _stub(sent, lambda: [])
    try:
        store.mark_submitted("T1", "020-18134281", "NOAIRWAW050582", "马殿齐", {"mode": "live"})
        sent.clear()
        try:
            company_api.submit_order(_payload())
            raise AssertionError("预读没读到却继续写了公司")
        except company_api.CompanyPreReadFailed as e:
            assert "没读到" in str(e) or "先不要提交" in str(e)
        assert not [s for s in sent if s["path"].endswith("/hawb2")], "一个写请求都不许发"
    finally:
        undo()
        _restore(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_house_submit_still_inserts_a_brand_new_row():
    """反过来：本机从没提交过、公司也读不到 = 真·首次录入，必须放行，
    否则第一张票永远提不上去（G26090906 就是这条路径）。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_p0_"))
    old = _iso(tmp)
    sent = []
    undo = _stub(sent, lambda: [])
    try:
        out = company_api.submit_order(_payload())
        assert out["accepted"] is True and out["action"] == "insert"
        assert any(s["path"].endswith("/hawb2") for s in sent)
    finally:
        undo()
        _restore(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_house_submit_merges_the_read_row_when_it_exists():
    """读到了就要把公司现有列带上（这条锁住"改法没改坏原有语义"）。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_p0_"))
    old = _iso(tmp)
    sent = []
    undo = _stub(sent, lambda: [dict(HOUSE_ROW)])
    try:
        company_api.submit_order(_payload())
        rec = [s for s in sent if s["path"].endswith("/hawb2")][0]["body"]["HAWB_RECORD"]
        assert rec["CONSIGNEE_INFO_EORI"] == "PL554289446200000", "读到没改的列要原样带回"
        assert rec["ORGIN_NAME"] == "BJS", "人工值覆盖读到旧值"
    finally:
        undo()
        _restore(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_double_click_submits_to_the_company_only_once():
    """同一个票、同一份内容连点两次：第二次必须直接回第一次的回执，不再发写请求。
    （j9 没有幂等键，重复提交就是两次整表写回。）"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_p0_"))
    old = _iso(tmp)
    sent = []
    undo = _stub(sent, lambda: [dict(HOUSE_ROW)])
    try:
        first = company_api.submit_order(_payload())
        n_writes = len([s for s in sent if s["path"].endswith("/hawb2")])
        second = company_api.submit_order(_payload())
        assert n_writes == 1
        assert len([s for s in sent if s["path"].endswith("/hawb2")]) == 1, "第二次不该再写公司"
        assert second.get("idempotent") is True and second["action"] == first["action"]
    finally:
        undo()
        _restore(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_real_change_is_not_swallowed_by_the_idempotency_lock():
    """改了值再提交是正常业务（SEND_STATUS 0/2 可反复改），幂等锁不能把它当重复点吞掉。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_p0_"))
    old = _iso(tmp)
    sent = []
    undo = _stub(sent, lambda: [dict(HOUSE_ROW)])
    try:
        company_api.submit_order(_payload())
        changed = _payload()
        changed["air"]["DEST_NAME"] = "MXP"
        out = company_api.submit_order(changed)
        assert out.get("idempotent") is not True
        writes = [s for s in sent if s["path"].endswith("/hawb2")]
        assert len(writes) == 2 and writes[-1]["body"]["HAWB_RECORD"]["DEST_NAME"] == "MXP"
    finally:
        undo()
        _restore(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_master_submit_refuses_when_the_record_should_exist():
    """主单侧同一条规矩：本机提交过 → 读回空就是没读到，不许写。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_p0_"))
    old = _iso(tmp)
    sent = []
    undo = _stub(sent, lambda: [])
    try:
        store.mark_master_submitted("176-62400004", "马殿齐", {"mode": "live"})
        sent.clear()
        try:
            company_api.submit_master({"mawb": "176-62400004", "ams": {"SLAC": 10}})
            raise AssertionError("预读没读到却继续写了公司主单")
        except company_api.CompanyPreReadFailed:
            pass
        assert not [s for s in sent if s["path"].endswith("/mawb2/")], "一个写请求都不许发"
    finally:
        undo()
        _restore(old)
        shutil.rmtree(tmp, ignore_errors=True)
