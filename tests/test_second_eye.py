# -*- coding: utf-8 -*-
"""第二只眼（2026-11-14 定案）：主链解析成功后，后台线程用同模型强制开思考重读一遍，
号码类冲突挂「互核」旗（穿过现有红旗管道，不拦暂存与提交），件数类标注"两边都可能错"；
整票空返回重试一次；已离开暂存档的票前端不挂现值旗。全程旁路：只写 qc，值一个字不动。
本仓库回归不引 pytest：打桩一律手工换属性、finally/with 还原。"""
import json
import tempfile
import threading
import time
from pathlib import Path

import config
import fieldspec
import second_eye


class _Patch:
    """极简打桩：with 块里临时换属性，退出逐个还原（顺序倒着还，防同名覆盖）。"""

    def __init__(self):
        self._undo = []

    def __call__(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        return False


def test_surface_and_count_cols_exist_in_field_face():
    """列名写死在第二只眼里，列面一改名它就悄悄烂掉：守一道列面契约，改名时这里先红。"""
    face = set(fieldspec.desk_keys())
    bad = [c for c in second_eye.SURFACE_COLS + second_eye.COUNT_COLS if c not in face]
    assert not bad, f"列面里没有这些列：{bad}（列面改名了就同步改第二只眼的清单）"


def test_conflicts_surfaces_number_and_count_cols_only():
    """号码类（主分单号/HS/税号/EORI/航路/日期/起讫）与件数类才顶人；地址/货描等整串差异
    只进留痕不挂旗——实测互核冲突率 11%，一半是格式类，全顶上去人就被淹死。"""
    main = {"MAWB_NO": "235-96146385", "GOODS_HS_CODE": "", "PIECES": 1,
            "CONSIGNEE_INFO_COMP_ADDRESS": "同左", "GOODS_INFO": "A"}
    second = {"MAWB_NO": "", "GOODS_HS_CODE": "854370", "PIECES": 2,
              "CONSIGNEE_INFO_COMP_ADDRESS": "同左 ", "GOODS_INFO": "A,B"}
    got = second_eye.conflicts(main, second)
    assert {c["col"]: c["kind"] for c in got} == {
        "MAWB_NO": "num", "GOODS_HS_CODE": "num", "PIECES": "count"}, got


def test_conflicts_compare_stripped_values():
    assert second_eye.conflicts({"MAWB_NO": " X "}, {"MAWB_NO": "X"}) == []


def test_flag_text_number_and_empty_variants():
    """旗文案就是界面上人工"已核"要对上的字符串，必须逐字稳定。"""
    assert second_eye.flag_text(
        {"col": "MAWB_NO", "main": "880-51074785", "second": "", "kind": "num"}
    ) == "互核 MAWB_NO：主链 '880-51074785'，第二只眼为空"
    assert second_eye.flag_text(
        {"col": "GOODS_HS_CODE", "main": "", "second": "854370", "kind": "num"}
    ) == "互核 GOODS_HS_CODE：主链为空，第二只眼 '854370'"
    assert second_eye.flag_text(
        {"col": "PIECES", "main": "1", "second": "2", "kind": "count"}
    ) == "互核 PIECES：主链 '1'，第二只眼 '2'（两边都可能错，以票面为准）"


def test_record_keeps_full_trace_but_only_surfaces_numbers():
    main = {"MAWB_NO": "A", "GOODS_INFO": "x"}
    second = {"MAWB_NO": "", "GOODS_INFO": "y"}
    rec = second_eye.record(main, second, model="m", thinking=True)
    assert rec["status"] == "ok" and rec["model"] == "m" and rec["thinking"] is True
    assert rec["all_diffs"] == ["GOODS_INFO", "MAWB_NO"], "全量差异要留痕，不止顶人的那几条"
    assert [c["col"] for c in rec["surfaced"]] == ["MAWB_NO"]
    assert rec["at"]


def test_extra_body_forces_thinking_regardless_of_global_switch():
    """第二只眼的思考档写死"开"，不吃全局开关：管理员把分单思考关了，第二只眼照样开。
    渠道没声明思考参数时一个字都不加（与 config.extra_body_for 口径一致），第二只眼降级照跑。"""
    qwen = config.MODEL_PRESETS["qwen38-flash-bailian"]
    assert second_eye.extra_body_for_preset(qwen) == {"enable_thinking": True}
    assert second_eye.extra_body_for_preset({}) == {}, "没声明思考参数的渠道一个字都不加"


def test_run_one_retries_blank_result_then_records_ok():
    """整票空返回是真实发生过的坑（DeepSeek 通道 1/10）：空了重试一次，再空就只留痕不挂旗。"""
    calls = []

    def fake_extract(src, transcript, urls):
        calls.append(1)
        return {} if len(calls) == 1 else {"MAWB_NO": "232-19509626"}

    with _Patch() as p, tempfile.TemporaryDirectory() as td:
        # run_one 末尾会落 qc：目录打桩是给变异验证的——写行为的变异不许碰真 output/
        p(second_eye, "_extract", fake_extract)
        p(config, "OUTPUT_QC_DIR", Path(td))
        rec = second_eye.run_one("s", {"MAWB_NO": "232-19509626"}, {"lines": []},
                                 ["u"], Path("a.pdf"))
    assert len(calls) == 2, "空返回必须重试一次"
    assert rec["status"] == "ok" and rec["surfaced"] == []


def test_run_one_records_empty_after_retry():
    calls = []

    def always_blank(*a):
        calls.append(1)
        return {}

    with _Patch() as p, tempfile.TemporaryDirectory() as td:
        p(second_eye, "_extract", always_blank)
        p(config, "OUTPUT_QC_DIR", Path(td))
        rec = second_eye.run_one("s", {"MAWB_NO": "X"}, None, [], None)
    assert len(calls) == 2 and rec["status"] == "empty" and rec["surfaced"] == []


def test_run_one_records_error_without_raising():
    def boom(*a):
        raise RuntimeError("net down")

    with _Patch() as p, tempfile.TemporaryDirectory() as td:
        p(second_eye, "_extract", boom)
        p(config, "OUTPUT_QC_DIR", Path(td))
        rec = second_eye.run_one("s", {"MAWB_NO": "X"}, None, [], None)
    assert rec["status"] == "error" and "net down" in rec["err"]


def test_run_one_passes_transcript_and_urls_to_extract():
    seen = {}

    def fake_extract(src, transcript, urls):
        seen.update(tr=transcript, urls=urls)
        return {"MAWB_NO": "X"}

    with _Patch() as p, tempfile.TemporaryDirectory() as td:
        p(second_eye, "_extract", fake_extract)
        p(config, "OUTPUT_QC_DIR", Path(td))
        second_eye.run_one("s", {"MAWB_NO": "X"}, {"lines": [{"i": 0, "text": "T"}]},
                           ["data:u"], Path("a.pdf"))
    assert seen["tr"] == {"lines": [{"i": 0, "text": "T"}]} and seen["urls"] == ["data:u"]


def test_append_record_writes_flags_and_keeps_trace():
    rec = {"status": "ok", "at": "T", "model": "m", "thinking": True,
           "surfaced": [{"col": "MAWB_NO", "main": "A", "second": "", "kind": "num"}],
           "all_diffs": ["MAWB_NO", "GOODS_INFO"]}
    with _Patch() as p, tempfile.TemporaryDirectory() as td:
        p(config, "OUTPUT_QC_DIR", Path(td))
        (Path(td) / "s.json").write_text(json.dumps({"flags": ["已有旗"], "needs_review": True},
                                                    ensure_ascii=False), encoding="utf-8")
        second_eye.append_record("s", rec)
        got = json.loads((Path(td) / "s.json").read_text(encoding="utf-8"))
    assert got["second_eye"] == rec
    assert "互核 MAWB_NO：主链 'A'，第二只眼为空" in got["flags"]
    assert got["needs_review"] is True


def test_append_record_adds_no_flags_when_nothing_surfaced():
    rec = {"status": "ok", "at": "T", "model": "m", "thinking": True, "surfaced": [],
           "all_diffs": ["GOODS_INFO"]}
    with _Patch() as p, tempfile.TemporaryDirectory() as td:
        p(config, "OUTPUT_QC_DIR", Path(td))
        (Path(td) / "s.json").write_text(json.dumps({"flags": [], "needs_review": False},
                                                    ensure_ascii=False), encoding="utf-8")
        second_eye.append_record("s", rec)
        got = json.loads((Path(td) / "s.json").read_text(encoding="utf-8"))
    assert got["flags"] == [] and got["needs_review"] is False, "整串类差异只留痕，不算需复核"
    assert got["second_eye"]["status"] == "ok"


def test_append_record_skips_when_no_qc():
    with _Patch() as p, tempfile.TemporaryDirectory() as td:
        p(config, "OUTPUT_QC_DIR", Path(td))
        second_eye.append_record("ghost", {"status": "empty", "surfaced": [], "all_diffs": []})
        assert not (Path(td) / "ghost.json").exists(), "没有质检记录（旧票/失败票）就不硬造一份"


def test_maybe_start_runs_once_in_background():
    ev = threading.Event()
    seen = []

    def fake_run(*a):
        seen.append(a)
        ev.set()

    with _Patch() as p:
        p(second_eye, "run_one", fake_run)
        args = ("s", {"A": "1"}, {"lines": []}, ["u"], Path("x.pdf"))
        second_eye.maybe_start(*args)
    assert ev.wait(5), "线程没跑起来"
    assert seen[0] == args


def test_maybe_start_disabled_does_nothing():
    seen = []
    with _Patch() as p:
        p(config, "SECOND_EYE", "0")
        p(second_eye, "run_one", lambda *a: seen.append(a))
        second_eye.maybe_start("s", {}, None, [], None)
        time.sleep(0.2)
    assert not seen, "开关关了就不该有线程"


def test_second_eye_enabled_follows_config():
    with _Patch() as p:
        p(config, "SECOND_EYE", "1")
        assert second_eye.enabled() is True
        p(config, "SECOND_EYE", "0")
        assert second_eye.enabled() is False


def test_extract_one_forwards_extra_body():
    import vlm_extract
    seen = {}

    def fake_chat(prompt, urls, want, label, **kw):
        seen.update(kw)
        return {c: "" for c in want}

    with _Patch() as p:
        p(vlm_extract, "_chat_json", fake_chat)
        vlm_extract.extract_one(Path("a.pdf"), transcript={"lines": [{"i": 0, "text": "X"}]},
                                urls=["u"], extra_body={"enable_thinking": True})
    assert seen.get("extra_body") == {"enable_thinking": True}, \
        "第二只眼要强制开思考，管道得能把参数送下去"


def test_process_file_captures_urls_in_bag():
    import pipeline
    with _Patch() as p:
        p(pipeline, "render_pages", lambda src: ["data:u1", "data:u2"])
        p(pipeline.vlm_extract, "extract_one",
          lambda *a, **k: {c: "" for c in fieldspec.desk_keys()})
        bag = {}
        pipeline.process_file(Path("a.pdf"), reuse={"lines": [{"i": 0, "text": "X"}]}, bag=bag)
    assert bag["urls"] == ["data:u1", "data:u2"], "第二只眼要复用同一份页图，不许再渲染一遍"


def test_maybe_start_reports_whether_started():
    """状态行的"互核中…"靠解析响应里的标记：maybe_start 得说实话——起了线程 True、
    开关关了 False。"""
    ev = threading.Event()

    def fake_run(*a):
        ev.set()

    with _Patch() as p:
        p(second_eye, "run_one", fake_run)
        p(config, "SECOND_EYE", "1")   # 回归入口钉 0：这条用例自己把开关拨回来
        assert second_eye.maybe_start("s", {}, None, [], None) is True
        assert ev.wait(5)
    with _Patch() as p:
        p(config, "SECOND_EYE", "0")
        assert second_eye.maybe_start("s", {}, None, [], None) is False


def test_handle_file_marks_se_pending_from_maybe_start():
    """解析结果带 se_pending：起了线程=True（前端亮"互核中…"），没起=False（不亮）。"""
    import jobs

    seen_ms = []
    real_ms, real_store, real_pf = (jobs.second_eye.maybe_start, jobs.store,
                                    jobs.process_file)
    class _S:
        def archive_original(self, pth):
            return {"md5": ""}
        def reusable_transcript(self, stem, prov):
            return None
        def save_transcript(self, stem, tr, prov=None):
            pass
        def save_result(self, stem, raw, air):
            pass
        def save_qc(self, stem, qc):
            pass
        def save_preview(self, stem, src):
            pass

    def fake_ms(*a):
        seen_ms.append(a)
        return len(seen_ms) == 1   # 桩照实说：第二次（开关已关）返回 False

    def fake_pf(path, do_transcript=True, reuse=None, bag=None):
        bag["transcript"] = {"lines": []}
        bag["urls"] = ["u"]
        return {"raw": {}, "air": {}, "channel": "vlm", "transcript": {"lines": []},
                "src": None, "model": "m", "model_choice": "c", "timings": {}}

    jobs.second_eye.maybe_start, jobs.store, jobs.process_file = fake_ms, _S(), fake_pf
    try:
        r = jobs.handle_file(Path("某分单 TAO1234567.pdf"), save=True)
        assert r["error"] == "", r["error"]
        assert r["se_pending"] is True and len(seen_ms) == 1, (r.get("se_pending"), seen_ms)

        config.SECOND_EYE = "0"               # maybe_start 会自己看开关——关了就该 False
        r2 = jobs.handle_file(Path("某分单 TAO1234567.pdf"), save=True)
        assert r2["se_pending"] is False
    finally:
        jobs.second_eye.maybe_start, jobs.store, jobs.process_file = real_ms, real_store, real_pf
        config.SECOND_EYE = "1"


def test_jobs_source_carries_the_marker_through_to_the_job_api():
    """/job 的 result 是整个 r 原样透传（server.job_result 不挑字段），所以只要 jobs 写进
    r["se_pending"]，前端就能拿到——钉住这一行，防止以后有人"顺手清理"掉。"""
    src = (Path(__file__).resolve().parent.parent / "jobs.py").read_text(encoding="utf-8")
    assert 'r["se_pending"]' in src, "jobs 得把第二只眼是否在跑写进解析结果"


def test_set_second_eye_validates_and_persists():
    """运行时总开关（旁路定案）：1/0 两档、写回 .env（重启不丢）、本进程立即生效；
    没有"跟随预设"那档——互核要么跑要么不跑。"""
    calls = []
    with _Patch() as p:
        p(config, "persist_model_choice", lambda v, k: calls.append((v, k)))
        assert config.set_second_eye("0") == "0"
        assert config.SECOND_EYE == "0" and calls == [("0", "SECOND_EYE")]
        assert config.set_second_eye("1") == "1"
        try:
            config.set_second_eye("跟随预设")
            assert False, "第二只眼没有跟随预设档"
        except ValueError:
            pass


def test_second_eye_default_is_on():
    assert config.SECOND_EYE == "1", "默认开（灰度一周）——回归 runner 不设这个 env"


def test_server_carries_the_switch_routes():
    """管理路由（仅管理员）+ /models 把开关状态报给前端（免登录下拉）。"""
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    assert '@app.post("/second_eye")' in src
    seg = src.split('/second_eye')[1][:300]
    assert "require_admin" in seg, "拨互核开关是管理员动作"
    assert '"second_eye": config.SECOND_EYE' in src, "/models 要把开关状态报出去"
