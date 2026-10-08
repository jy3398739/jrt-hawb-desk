# -*- coding: utf-8 -*-
"""解析正确率（需求三）：模型那一版与人最后发出去的那一版，逐列比。

这里算的数是要拿去做决定的——"能不能不经过人、直接把提取结果喂给平台"。
所以分母、归属、以及"哪些票没有留痕所以不算"三件事必须写死在用例里，
而不是算出一个看着合理的数。
"""
import contextlib
import json
import tempfile
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import server
import stats
import store
import web_src

TODAY = date.today().isoformat()


def _model(**over):
    air = {"MAWB_NO": "235-96146363", "HAWB_NO": "S1", "ORIGIN_NAME": "BJS", "DEST_NAME": "LAX",
           "PIECES": 1, "WEIGHT": 170.0, "GOODS_INFO": "STEEL PARTS",
           "SHIPPER_INFO_TEL": "+8613258840319", "SEND_STATUS": "PENDING"}
    air.update(over)
    return air


@contextlib.contextmanager
def _world():
    """只摆这台服务器上的落盘文件，不碰真 output/：stats 读的就是这几个目录。"""
    old = (config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR,
           config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR, config.SUBMIT_LEDGER, config.ARCHIVE_DIR,
           auth.USERS_FILE)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config.STAGED_DIR, config.OUTPUT_AIR_DIR = tmp / "staged", tmp / "air"
        config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR = tmp / "raw", tmp / "qc"
        config.TRANSCRIPT_DIR, config.ARCHIVE_DIR = tmp / "transcript", tmp / "archive"
        config.SUBMIT_LEDGER = tmp / "submitted.json"
        auth.USERS_FILE = tmp / "users.json"
        for p in (config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR,
                  config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR, config.ARCHIVE_DIR):
            p.mkdir()
        try:
            yield tmp
        finally:
            (config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR,
             config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR, config.ARCHIVE_DIR,
             config.SUBMIT_LEDGER, auth.USERS_FILE) = old


def _parsed(stem, model, uploader="马殿齐", role="reviewer", when=TODAY,
            mawb="235-96146363", hawb=None):
    """摆一张"已解析并落盘"的票：output/air 是模型原样，qc 记下谁传的、哪个模型读的。"""
    a = _model(**{"HAWB_NO": hawb or stem, **model})
    (config.OUTPUT_AIR_DIR / f"{stem}.json").write_text(json.dumps(a, ensure_ascii=False),
                                                        encoding="utf-8")
    store.save_qc(stem, {"source_name": stem + ".pdf", "channel": "vlm",
                         "model": "qwen3.8-flash", "model_choice": "qwen38-flash-bailian",
                         "processed_at": when + "T09:00:00", "uploader": uploader,
                         "uploader_role": role, "flags": [], "error": ""})
    (config.ARCHIVE_DIR / stem).mkdir()
    return a


def _stage(stem, final, by, role):
    return store.stage_ticket(stem, final, by, role)


def _send(stem, mawb, hawb, sent, reviewer, stager="", no_review=True):
    return store.mark_submitted(stem, mawb, hawb, reviewer, {"mode": "mock"},
                                air_sent=sent, stager=stager,
                                staged_at=TODAY + "T10:00:00" if stager else "",
                                no_inputter_review=no_review)


def test_per_field_accuracy_and_who_moved_it():
    """三个数分别是什么，必须能各举一个例子对上：
      total = 模型那版和最后发出去的那版之间差几列（真正意义上的"模型读错了几列"）；
      doc   = 这几列里被制单员动过的；
      inp   = 被录入员动的（含暂存之后又改了才发的）。
    `1 - inp/可比列数` 才是"人还剩多少活"，也就是能不能对接平台的数——
    它和 `1 - total/可比列数`（模型本身准不准）不能混成一个。"""
    with _world():
        m = _parsed("A1", {"DEST_NAME": "LAX", "PIECES": 1})
        _stage("A1", {**m, "DEST_NAME": "ORD"}, "马殿齐", "reviewer")
        _send("A1", "235-96146363", "A1", {**m, "DEST_NAME": "ORD", "PIECES": 2},
              "刘明", stager="马殿齐", no_review=False)
        _stage("A1", {**m, "DEST_NAME": "ORD"}, "马殿齐", "reviewer")   # 基线已快照，重复调无副作用

        rows = {r["stem"]: r for r in stats.collect()}
        r = rows["A1"]
        assert r["total"] == 2, f"模型→最终差两列（到达站 + 件数）：{r}"
        assert sorted(r["doc"]) == ["DEST_NAME"], r["doc"]
        assert sorted(r["inp"]) == ["PIECES"], r["inp"]
        assert r["comparable"] == 8, f"可比列数=模型或终值非空的列数（两边都空的不算白送一分）：{r}"

        s = stats.summary(stats.collect())
        assert s["tickets"] == 1 and s["fields_comparable"] == 8, s
        assert s["edits_total"] == 2 and s["edits_doc"] == 1 and s["edits_inp"] == 1, s
        assert round(s["acc_field"], 4) == round(1 - 2 / 8, 4), s
        assert round(s["acc_after_review"], 4) == round(1 - 1 / 8, 4), s
        assert s["clean_rate"] == 0.0, "这张票改过两列，不该算一次通过"


def test_a_direct_send_with_no_stage_trace_counts_every_change_against_the_preparer():
    """没暂存记录时，改动全记到制单员那一档。

    这不是省事，是事实：那批值就是坐在核对台前的人改的，只是没有录入员复核过。
    要是因为"找不到 inputter 事件"就把改动记成 0，等于告诉系统"模型一次读对"——
    正确率会被系统性抬高，而抬高的方向最危险（会答应下本来不该接的对接）。
    """
    with _world():
        m = _parsed("B2", {"GOODS_INFO": "STEEL"})
        _send("B2", "235-96146363", "B2", {**m, "GOODS_INFO": "STEEL PARTS HS 7326",
                                          "DEST_NAME": "AMS"}, "马殿齐")
        r = {x["stem"]: x for x in stats.collect()}["B2"]
        assert r["total"] == 2, r
        assert sorted(r["doc"]) == ["DEST_NAME", "GOODS_INFO"], r["doc"]
        assert r["inp"] == [], r
        assert r["no_inputter_review"] is True, r
        s = stats.summary([r])
        assert s["edits_inp"] == 0 and s["edits_doc"] == 2, s
        assert s["unreviewed"] == 1, "没复核就发的张数要单独报，它是那批数的对照组"


def test_a_ticket_staged_but_never_sent_is_not_counted_as_sent_without_review():
    """这一档的标签是"未经录入员复核就发出"，那它数到的必须真的发出去了。

    只暂存没发的票也带着"还没有 inputter 事件"，一起加进去的话，这个数会随着谁
    把工作停在暂存那一档而涨——而它涨的恰好是拿去决定要不要对接平台的那个数。
    行上的标记照旧留着（那是这张票自己的复核状态），只是不进这个合计。
    """
    with _world():
        a = _parsed("SENT-OK", {"DEST_NAME": "LAX"})
        _stage("SENT-OK", {**a, "DEST_NAME": "PAR"}, "马殿齐", "reviewer")
        _stage("SENT-OK", {**a, "DEST_NAME": "AMS"}, "刘明", "inputter")
        _send("SENT-OK", "235-96146363", "SENT-OK", {**a, "DEST_NAME": "AMS"}, "刘明",
              stager="刘明", no_review=False)
        w = _parsed("WAITING", {"DEST_NAME": "ORD"})
        _stage("WAITING", {**w, "DEST_NAME": "PAR"}, "马殿齐", "reviewer")
        j = _parsed("JUMPED", {"DEST_NAME": "MAD"})
        _send("JUMPED", "235-96146363", "JUMPED", {**j, "DEST_NAME": "AMS"}, "马殿齐")
        rows = {x["stem"]: x for x in stats.collect()}
        assert rows["WAITING"]["state"] == "staged", rows["WAITING"]
        assert rows["WAITING"]["no_inputter_review"] is True, "这张票自己的复核状态还是要标出来"
        s = stats.summary(list(rows.values()))
        assert s["tickets"] == 3, s
        assert s["unreviewed"] == 1, f"只有真发出去又没人复核的那张算：{s['unreviewed']}"


def test_ledger_entries_without_value_trace_are_counted_as_legacy_not_as_zero_edits():
    """本功能上线之前的台账没有 air_sent（只存过字段名与哈希）：算不了就别混进来。

    把它们当"零改动"，修改率会当场变成 0%——这台机器上真有 17 条这样的历史记录，
    上一次就是这么算出"制单员一张都没改过"的。它们要进 legacy 计数，不进正确率的分母。
    """
    with _world():
        _parsed("OLD", {"DEST_NAME": "LAX"})
        store.mark_submitted("OLD", "235-96146363", "OLD", "admin", {"mode": "mock"})
        rows = stats.collect()
        assert [r["stem"] for r in rows] == ["OLD"], rows
        assert rows[0]["legacy"] is True and rows[0]["total"] is None, rows[0]
        s = stats.summary(rows)
        assert s["tickets"] == 0 and s["legacy"] == 1, s
        assert s["acc_field"] is None, "没有可算的票就不许编一个 100% 出来"


def test_both_empty_fields_are_not_comparable_and_send_status_never_is():
    """两边都空的列不算（票面本来就没有这栏，不该白送一分）；
    SEND_STATUS 恒等于模型硬写的 PENDING，算进去等于凭空加一列正确。"""
    with _world():
        m = _parsed("C3", {"TO2": "", "TO3": "", "GOODS_HS_CODE": ""})
        _send("C3", "235-96146363", "C3", dict(m), "马殿齐")
        r = {x["stem"]: x for x in stats.collect()}["C3"]
        assert r["total"] == 0, r
        assert "SEND_STATUS" not in r["fields"], r["fields"]
        assert "TO2" not in r["fields"] and "GOODS_INFO" in r["fields"], r["fields"]
        assert r["comparable"] == len(r["fields"]) == 8, r
        assert stats.summary([r])["clean_rate"] == 1.0, "零改动就该算一次通过"


def test_format_only_edits_do_not_count_and_model_is_a_grouping_axis():
    """170.0 改成 170、单号少了根连字符，不算模型读错（compare 走 store.same_value）。
    按模型分组是本轮新增的能力：qc 里第一次落下用的模型，两条渠道的横评不用再手工跑脚本。"""
    with _world():
        m = _parsed("D4", {"WEIGHT": 170.0, "MAWB_NO": "235-96146363"})
        _send("D4", "235-96146363", "D4", {**m, "WEIGHT": "170", "MAWB_NO": "23596146363"}, "马殿齐")
        rows = stats.collect()
        assert rows[0]["total"] == 0, rows[0]
        s = stats.summary(rows, group_by="model")
        assert [g["model"] for g in s["groups"]] == ["qwen3.8-flash"], s["groups"]
        assert s["groups"][0]["tickets"] == 1, s["groups"]


def test_stats_route_needs_login_and_reports_the_source_of_truth():
    """接口要登录（回的是票面与人名），并且把"这些数从哪算的"说清楚：
    历史遗留几条、时间窗内几张——不然拿着一个没有分母的百分比就会去做决定。"""
    with _world():
        m = _parsed("E5", {"DEST_NAME": "LAX"})
        _send("E5", "235-96146363", "E5", {**m, "DEST_NAME": "ORD"}, "马殿齐")
        store.mark_submitted("OLD9", "235-96146363", "OLD9", "admin", {"mode": "mock"})
        anon = TestClient(server.app)
        assert anon.get("/stats").status_code == 401, "统计含人名与经手记录，不登录不给读"
        login = TestClient(server.app)
        login.post("/login", json={"name": "admin", "password": "admin123"})
        r = login.get("/stats")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["summary"]["tickets"] == 1 and j["summary"]["legacy"] == 1, j["summary"]
        assert j["summary"]["edits_doc"] == 1, j["summary"]
        # rows 里保留历史那条（页面要显示"这张没逐字段留痕"），只是它不进 summary 的分母
        assert [(x["stem"], x["legacy"]) for x in j["rows"]] == [("E5", False), ("OLD9", True)], j["rows"]
        bad = login.get("/stats", params={"group_by": "性别"})
        assert bad.status_code == 400, "乱填分组要拒绝，不能悄悄按不分组的算"
        win = login.get("/stats", params={"from": "2020-01-01", "to": "2020-01-02"}).json()
        assert win["summary"]["tickets"] == 0 and win["summary"]["legacy"] == 0, win


def test_the_exported_excel_carries_the_same_numbers_as_the_api():
    """导出必须与页面同源：同一套算术如果哪里再写一遍，两份百分比会不一致，
    而拿去开会的往往是 Excel 那份。"""
    import io

    import pandas as pd

    with _world():
        m = _parsed("F1", {"DEST_NAME": "LAX", "PIECES": 1})
        _stage("F1", {**m, "DEST_NAME": "ORD"}, "马殿齐", "reviewer")
        _send("F1", "235-96146363", "F1", {**m, "DEST_NAME": "ORD"}, "刘明",
              stager="马殿齐", no_review=False)
        login = TestClient(server.app)
        login.post("/login", json={"name": "admin", "password": "admin123"})
        j = login.get("/stats").json()["summary"]
        r = login.get("/stats/export")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith(
            "application/vnd.openxmlformats"), r.headers.get("content-type")
        assert r.content[:2] == b"PK", "导出的不是 xlsx"
        head = pd.read_excel(io.BytesIO(r.content), sheet_name="口径")
        got = {str(row["指标"]): row["值"] for _, row in head.iterrows()}
        assert got["逐字段准确率"] == j["acc_field"], got
        assert got["复核后准确率（能不能对接看这个）"] == j["acc_after_review"], got
        per = pd.read_excel(io.BytesIO(r.content), sheet_name="每张票")
        assert list(per["票名"]) == ["F1"] and per["改动列数"][0] == 1, per


def test_stats_view_shows_the_server_numbers_and_says_what_is_missing():
    """统计页只许渲染服务器算好的数：同一套算术在前后端各写一遍，迟早给出两个百分比
    （今日台账那页就为这件事专门留了一条用例）。

    还要把"能不能对接"的那个数与"模型本身准不准"分开摆，并说明历史数据没进分母。"""
    page, js = web_src.part("index.html"), web_src.part("js/desk.js")
    assert 'id="viewStats"' in page and 'id="statsCard"' in page, "缺少统计页入口或整页"
    assert '"/stats"' in js and "renderStats" in js, "前端没接 /stats"
    for k in ("acc_field", "acc_after_review", "clean_rate"):
        assert k in js, f"页面上少了 {k} 这个数"
    assert "legacy" in js, "要说明有几张历史票没逐字段留痕、没进分母"
    assert "edits_doc" in js and "edits_inp" in js, "两个角色的改动要分开摆，不能合成一个修改率"
    # 前端不许自己再算总改动：只许读 summary
    i = js.index("function renderStats(")
    body = js[i:i + 2500]
    assert "reduce(" not in body and "edits_total +" not in body, \
        "统计页在自己重算合计：与服务器同一套算术写两遍迟早对不上"
    # 导出走服务器，页面与导出同源
    assert '"/stats/export"' in js, "要能导出同一份数"
