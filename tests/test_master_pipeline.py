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
import j9_fake
import master_fields as mf
import master_pipeline as mp
import store
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
    以只读 meta 形式进基础信息，让人看得见、也不会被当成可提交列发出去。
    通知人 2026-10-15 起自成一档排在最后（业主定案 发货人 → 收货人 → 通知人）。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        rec = mp.public(mp.run_master("176-62400004", dict(ORDER, JOB_ID=40366281)))
        assert rec["groups"]["base"] == "基础信息" and rec["groups"]["notify"] == "通知人 NOTIFY"
        assert list(rec["groups"]) == ["base", "shipper", "consignee", "notify"], rec["groups"]
        assert rec["meta"] == [{"col": "JOB_ID", "label": "公司任务号（只读，不提交）",
                                "value": 40366281, "group": "base"}]
        assert "JOB_ID" not in rec["ams"], "只读列绝不进可提交面"
        assert not any(l["text"].startswith("JOB_ID") for l in rec["transcript"]["lines"]), \
            "JOB_ID 不进 L1：模型没有这一列，给了只会诱发它编"
    finally:
        undo()
        _restore(old, tmp)


def test_stage_master_keeps_the_confirmed_flags_on_the_record():
    """人确认过哪些红旗必须落在服务器那条主单记录上：只存在浏览器草稿里的话，刷新一次或
    换台机器再检索同一张主单，确认过的旗又红一遍——分单侧同一个病（2026-10-15 现场）。
    再一次暂存没带上那条，说的是他把确认撤回了，那份留痕要跟着收回去，不许留在顶上。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        mp.run_master("176-62400004", ORDER)
        mp.stage_master("176-62400004", dict(AMS_OUT, SLAC=12), "宛平", "reviewer",
                        acked_flags=["主单号与公司资料不一致"])
        rec = mp.read_master("176-62400004")
        assert rec["acked_flags"] == ["主单号与公司资料不一致"], rec.get("acked_flags")
        assert mp.public(rec)["acked_flags"] == ["主单号与公司资料不一致"], "前端读的就是 public 那份"
        mp.stage_master("176-62400004", dict(AMS_OUT, SLAC=12), "宛平", "reviewer", acked_flags=[])
        assert mp.read_master("176-62400004")["acked_flags"] == [], "撤回的确认不能还挂在顶上"
    finally:
        undo()
        _restore(old, tmp)


def test_submit_master_leaves_the_confirmed_flags_on_the_parsed_record():
    """提交之后重开这张主单，确认过的红旗不该再红一遍：除了台账，解析记录顶上也要留着这一档
    （没解析记录时不新建文件，提交的账不该顺手造出一份缓存来）。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    undo_cfg = _live()
    sent = []
    restore = _patch_j9(sent, [{"MASTER_NO": "176-62400004", "AMS_RECORD": None}])
    try:
        mp.run_master("176-62400004", ORDER)
        bad = dict(AMS_OUT, SHIPPER_INFO_POSTAL="x" * 60)
        flags = mp.master_flags(bad)
        assert flags, "这一版样本没红旗的话，这条断言就是空的"
        mp.submit_master({"mawb": "176-62400004", "reviewer": "马殿齐", "ams": bad,
                          "acked_flags": flags})
        assert mp.read_master("176-62400004")["acked_flags"] == flags
    finally:
        restore()
        config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_MAWB_KEY, config.COMPANY_HAWB_KEY = undo_cfg
        undo()
        _restore(old, tmp)


def test_second_parse_marks_the_columns_we_sent_back():
    """暂存或提交之后再检索同一张主单，L1 会长出我们写回公司的那些列——解析记录要带着
    「哪几行是回填的」下发到前端，不然人看到的"公司资料"里混着自己的输出（2026-10-15 现场：
    074-51188292 第一次 4 行、暂存后第二次 15 行）。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        first = {"MASTER_NO": "074-51188292",
                 "AMS_RECORD": {"MASTER_NO": "074-51188292", "SHIPPER_INFO": "DSV AIR & SEA CO., LTD.",
                                "GOODS_NAME": "QUARTZ GLASS"}}
        mp.run_master("074-51188292", first)
        assert mp.read_master("074-51188292")["self_filled"] == [], "第一次没有可比对象，一格都不该标"
        # 我们把核对结果写回公司（提交台账里那份 ams_sent 就是发出去的值）
        sent = {"MASTER_NO": "074-51188292", "MAWB_NO": "074-51188292", "GOODS_INFO_HSCODE": "702000",
                "SHIPPER_INFO": "DSV AIR & SEA CO., LTD.", "GOODS_NAME": "QUARTZ GLASS",
                "SHIPPER_INFO_COMP_NAME": "DSV AIR & SEA CO., LTD."}
        store.mark_master_submitted("074-51188292", "宛平", {"mode": "live"}, ams_sent=sent)
        second = {"MASTER_NO": "074-51188292",
                  "AMS_RECORD": dict(sent, SHIPPER_INFO_COMP_ADDRESS="25A,HISENSE TOWER")}
        rec = mp.run_master("074-51188292", second)
        assert sorted(rec["self_filled"]) == ["GOODS_INFO_HSCODE", "MAWB_NO", "SHIPPER_INFO_COMP_NAME"], \
            rec["self_filled"]
        assert "SHIPPER_INFO_COMP_ADDRESS" not in rec["self_filled"], \
            "这一格本机从没发过（公司自己新录的），不该被说成回填"
        assert "SHIPPER_INFO" not in rec["self_filled"] and "GOODS_NAME" not in rec["self_filled"], \
            "上次 L1 里就有这两行 = 公司原本录的，值相同也不该改口说是回填"
        assert mp.public(rec)["self_filled"] == rec["self_filled"], "前端读的是 public 那份"
    finally:
        undo()
        _restore(old, tmp)


def test_a_staged_only_master_is_also_compared_against_what_we_sent():
    """用户撞上的正是这一路：只点了「暂存到公司」，没提交。暂存走 `mawb2` **不写提交台账**，
    那份发出去的值只在这条主单缓存的 `ams_final` 里——只看台账就会把这一整路漏掉。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        first = {"MASTER_NO": "074-51188292",
                 "AMS_RECORD": {"MASTER_NO": "074-51188292", "GOODS_NAME": "QUARTZ GLASS"}}
        mp.run_master("074-51188292", first)
        mp.stage_master("074-51188292", {"MAWB_NO": "074-51188292", "SLAC": 12,
                                         "GOODS_INFO_HSCODE": "841290901"}, "宛平", "reviewer")
        second = {"MASTER_NO": "074-51188292",
                  "AMS_RECORD": {"MASTER_NO": "074-51188292", "GOODS_NAME": "QUARTZ GLASS",
                                 "MAWB_NO": "074-51188292", "SLAC": 12, "GOODS_INFO_HSCODE": "841290901"}}
        rec = mp.run_master("074-51188292", second)
        assert sorted(rec["self_filled"]) == ["GOODS_INFO_HSCODE", "MAWB_NO", "SLAC"], rec["self_filled"]
        assert not (tmp / "master_submitted.json").exists(), "这条路径压根没提交，判据不该依赖台账"
    finally:
        undo()
        _restore(old, tmp)


SENT_BACK = {"MASTER_NO": "074-51188292", "MAWB_NO": "074-51188292", "GOODS_INFO_HSCODE": "702000",
             "SHIPPER_INFO": "DSV AIR & SEA CO., LTD.", "GOODS_NAME": "QUARTZ GLASS",
             "SHIPPER_INFO_COMP_NAME": "DSV AIR & SEA CO., LTD."}
FIRST_READ = {"MASTER_NO": "074-51188292",
              "AMS_RECORD": {"MASTER_NO": "074-51188292", "SHIPPER_INFO": "DSV AIR & SEA CO., LTD.",
                             "GOODS_NAME": "QUARTZ GLASS"}}


def _labels(tr: dict) -> list:
    return [x["text"].split(":", 1)[0] for x in tr["lines"]]


def test_the_model_input_is_pinned_to_what_we_first_read():
    """IT 不接受「混着自己的输出、只是标一下」：主单解析几次都好，喂给模型的必须是公司录的那几行。
    所以比较对象不能是「上一次读到的那批行」（暂存之后那批本身就是脏的，一行也剔不掉），
    而是**第一次读到时就钉下的那份基线**，逐次继承（2026-10-16 定案）。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        one = mp.run_master("074-51188292", FIRST_READ)
        assert sorted(set(_labels(one["transcript"]))) == ["GOODS_NAME", "MASTER_NO", "SHIPPER_INFO"], one["transcript"]
        assert sorted(one["baseline"]) == ["GOODS_NAME", "MASTER_NO", "SHIPPER_INFO"], "第一次读到的就是原件基线"
        store.mark_master_submitted("074-51188292", "宛平", {"mode": "live"}, ams_sent=dict(SENT_BACK))
        two = mp.run_master("074-51188292", {"MASTER_NO": "074-51188292", "AMS_RECORD": dict(SENT_BACK)})
        assert sorted(set(_labels(two["transcript"]))) == ["GOODS_NAME", "MASTER_NO", "SHIPPER_INFO"], \
            "写回公司的那几列又读回来了：剔完要和第一次一样，模型不许抄自己"
        assert two["text_md5"] == one["text_md5"], "指纹按剔后算——写回多少列都不该再把缓存踢一次"
        assert sorted(x["label"] for x in two["excluded_self"]) == \
            ["GOODS_INFO_HSCODE", "MAWB_NO", "SHIPPER_INFO_COMP_NAME"], two["excluded_self"]
        for x in two["excluded_self"]:
            assert x["value"] == SENT_BACK[x["label"]], "被剔的那几行要原样记账，回头查得着"
        assert [l["i"] for l in two["transcript"]["lines"]] == list(range(1, len(two["transcript"]["lines"]) + 1)), \
            "剔完不重排行号，前端说的第几行与点字段名的定位就会指到别的行上"
        assert sorted(two["baseline"]) == sorted(one["baseline"]), "基线要逐次继承，不能被这次读到的脏行顶掉"
        assert mp.public(two)["excluded_self"] == two["excluded_self"], "前端读的是 public 那份"
    finally:
        undo()
        _restore(old, tmp)


def test_a_line_the_company_recorded_itself_still_reaches_the_model():
    """剔的是「本机写回的」，不是「新出现的」。公司 IT 后来自己往那一行录了值（本机从没发过那一格），
    那仍是原始资料——把它一起剔掉就等于把公司真值藏起来，模型这次取不到、还报一条假漏取。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        one = mp.run_master("074-51188292", FIRST_READ)
        store.mark_master_submitted("074-51188292", "宛平", {"mode": "live"}, ams_sent=dict(SENT_BACK))
        two = mp.run_master("074-51188292", {"MASTER_NO": "074-51188292",
                                             "AMS_RECORD": dict(SENT_BACK,
                                                                SHIPPER_INFO_COMP_ADDRESS="25A,HISENSE TOWER")})
        assert "SHIPPER_INFO_COMP_ADDRESS" in _labels(two["transcript"]), two["transcript"]
        assert "SHIPPER_INFO_COMP_ADDRESS" not in [x["label"] for x in two["excluded_self"]]
        assert sorted(x["label"] for x in two["excluded_self"]) == \
            ["GOODS_INFO_HSCODE", "MAWB_NO", "SHIPPER_INFO_COMP_NAME"], two["excluded_self"]
        assert two["text_md5"] != one["text_md5"], "公司真的多录了一行，指纹要变、要重解析，不然新值进不了模型"
    finally:
        undo()
        _restore(old, tmp)


def test_a_master_written_back_before_this_upgrade_only_says_it_cannot_tell():
    """升级前解析的老票没有当场钉下的基线。两种情况分开处置，都不许假装分得清：
    ① 还有上一次解析的记录 ⇒ 退到它读到的那批行当对照（比这些行晚出现、又与本机发过的相同 ⇒ 剔），
       但这份"证据不是当场钉的"要如实带着（`baseline_dirty`），重解析一次也不许洗掉；
    ② 连上一次记录都没有（缓存被清过）⇒ 一行都不剔，只报分不清几行。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        prev_read = {"MASTER_NO": "074-51188292",
                     "AMS_RECORD": {"MASTER_NO": "074-51188292", "SHIPPER_INFO": "DSV AIR & SEA CO., LTD.",
                                    "GOODS_NAME": "QUARTZ GLASS", "MAWB_NO": "074-51188292"}}
        mp._write({"mawb": "074-51188292", "state": "done", "text_md5": mp._md5("x"),
                   "face_md5": mp._face_md5(), "model": config.master_model_bundle()["model"],
                   "transcript": mf.build_transcript(prev_read), "ams": {}, "ams_raw": {},
                   "baseline": [], "qc": {"flags": [], "needs_review": False}, "error": ""})
        store.mark_master_submitted("074-51188292", "宛平", {"mode": "live"}, ams_sent=dict(SENT_BACK))
        rec = mp.run_master("074-51188292", {"MASTER_NO": "074-51188292", "AMS_RECORD": dict(SENT_BACK)})
        assert rec["baseline_dirty"] is True, "基线是从上一次解析那份补的，界面要看得见这句话"
        assert sorted(x["label"] for x in rec["excluded_self"]) == \
            ["GOODS_INFO_HSCODE", "SHIPPER_INFO_COMP_NAME"], rec["excluded_self"]
        assert sorted(rec["baseline"]) == ["GOODS_NAME", "MASTER_NO", "MAWB_NO", "SHIPPER_INFO"], rec["baseline"]
        again = mp.run_master("074-51188292", {"MASTER_NO": "074-51188292", "AMS_RECORD": dict(SENT_BACK)})
        assert again["baseline_dirty"] is True, "重解析一次就把污点忘掉＝界面在撒谎"
        assert sorted(x["label"] for x in again["excluded_self"]) == \
            sorted(x["label"] for x in rec["excluded_self"]), "同一份基线要剔同一批行，别看第几次解析"
        # ②：缓存清干净，只剩台账那份发过的值
        mp.path_for("074-51188292").unlink()
        fresh = mp.run_master("074-51188292", {"MASTER_NO": "074-51188292", "AMS_RECORD": dict(SENT_BACK)})
        assert fresh["excluded_self"] == [], "连可比对象都没有，一行都不许剔"
        assert {x["label"] for x in fresh["unknown_self"]} == \
            {"MASTER_NO", "MAWB_NO", "GOODS_INFO_HSCODE", "SHIPPER_INFO", "GOODS_NAME",
             "SHIPPER_INFO_COMP_NAME"}, fresh["unknown_self"]
        assert fresh["baseline_dirty"] is False, "这一张是从这次读回当场钉的基线，谈不上补证"
    finally:
        undo()
        _restore(old, tmp)


def test_searching_a_pinned_master_twice_costs_no_new_parse():
    """认了「第一次要重跑」这一刀之后必须钉住的另一半：剔完的输入与首次一致 ⇒ 指纹一致 ⇒
    以后再怎么写回都不再花钱。走 ensure（路由那条真链路），不是 run_master。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    try:
        first = mp.ensure("074-51188292", FIRST_READ)
        assert first["state"] == "done" and len(box) == 1, box
        mp.stage_master("074-51188292", {"MAWB_NO": "074-51188292", "GOODS_INFO_HSCODE": "702000",
                                         "SHIPPER_INFO_COMP_NAME": "DSV AIR & SEA CO., LTD."},
                        "宛平", "reviewer", acked_flags=[])
        again = mp.ensure("074-51188292", {"MASTER_NO": "074-51188292", "AMS_RECORD": dict(SENT_BACK)})
        assert again["state"] == "done" and len(box) == 1, "暂存过再检索，模型一次都不该再调"
        assert sorted(set(_labels(again["transcript"]))) == ["GOODS_NAME", "MASTER_NO", "SHIPPER_INFO"], again
        assert again["baseline_dirty"] is False, again["baseline_dirty"]
        # 强制重解析要走 ensure 写占位那一下（`state=parsing` 是把上一份整个盖掉的），
        # 基线/发过的值没带过去的话，这一次重跑就把刚钉的原件洗没了
        forced = mp.ensure("074-51188292", {"MASTER_NO": "074-51188292", "AMS_RECORD": dict(SENT_BACK)},
                           force=True)
        assert len(box) == 2, box
        assert sorted(x["label"] for x in forced["excluded_self"]) == \
            ["GOODS_INFO_HSCODE", "MAWB_NO", "SHIPPER_INFO_COMP_NAME"], forced["excluded_self"]
        assert forced["baseline_dirty"] is False, "占位那次 _write 不许把刚钉的基线洗掉"

    finally:
        undo()
        _restore(old, tmp)


def test_the_parsing_placeholder_still_carries_the_baseline():
    """重解析先把 `state=parsing` 那一份写下去——它是**整份盖掉**上一份记录的，而轮询读到的就是它。
    基线、本机发过的那份账不带着走，占位这一档就没有可比对象：等真值写回来时，
    这一次读到的行已经被当成"公司一直都有"了（v1.27.0 的标注在这条真链路上从来不出，就是这个洞）。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    box, undo = _stub_extract()
    seen = {}
    try:
        mp.ensure("074-51188292", FIRST_READ)
        store.mark_master_submitted("074-51188292", "宛平", {"mode": "live"}, ams_sent=dict(SENT_BACK))
        stub = vlm_extract.extract_master

        def peek(transcript, **kw):
            seen.update(mp.read_master("074-51188292") or {})
            return stub(transcript, **kw)

        vlm_extract.extract_master = peek
        try:
            mp.ensure("074-51188292", {"MASTER_NO": "074-51188292", "AMS_RECORD": dict(SENT_BACK)},
                      force=True)
        finally:
            vlm_extract.extract_master = stub
        assert seen.get("state") == "parsing", seen.get("state")
        assert sorted(seen.get("baseline") or []) == ["GOODS_NAME", "MASTER_NO", "SHIPPER_INFO"], \
            "占位记录没带基线：轮询那一眼就没有可比对象"
        assert "MAWB_NO" in (seen.get("sent_back") or {}), "本机发过的那份账也要跟着占位记录走"
    finally:
        undo()
        _restore(old, tmp)


def test_a_submitted_master_gets_flags_recomputed_on_the_company_values():
    """与分单同一条（用户 2026-10-16 问的是同一件事，两条链都得改）：交出去的主单第二次检索，
    红旗按**公司那一版终值**现算，不再回放解析时那批对着模型值算出来的旗。
    判据仍走 `master_flags` 那一处，只是喂给它台账里那份 `ams_sent`。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hawb_mp_"))
    old = _use_tmp(tmp)
    bad = {c: None for c in mp.MASTER_COLS}
    bad.update({"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901", "SLAC": 10,
                "SHIPPER_INFO_COMP_NAME": "METSO (TIANJIN) INVESTMENT CO., LTD.",
                "SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_EORI": "NOTANID"})
    box, undo = _stub_extract(reply=bad)
    try:
        rec = mp.run_master("176-62400004", ORDER)
        assert any("SHIPPER_INFO_EORI" in f for f in rec["qc"]["flags"]), rec["qc"]["flags"]
        assert mp.public(rec)["flags_now"] is None, "还没发出去的票没有「公司现值」，不许凭空造一份"
        fixed = {c: None for c in mp.MASTER_COLS}
        fixed.update({"MAWB_NO": "176-62400004", "GOODS_INFO_HSCODE": "841290901", "SLAC": 10,
                      "SHIPPER_INFO_COMP_NAME": "METSO (TIANJIN) INVESTMENT CO., LTD.",
                      "SHIPPER_INFO_COUNTRY": "CN"})
        store.mark_master_submitted("176-62400004", "宛平", {"mode": "live"}, ams_sent=fixed)
        out = mp.public(mp.read_master("176-62400004"))
        assert out["flags_now"] == [], out["flags_now"]
        assert out["flags_now_at"], out
        assert out["qc"]["flags"] == rec["qc"]["flags"], "落盘那份不改：解析质量分还是按解析时算"
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
        if j9_fake.is_write(path):
            return j9_fake.write_ok(path)
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
                               "AMS_RECORD": dict(AMS_OUT, HMY_ID=12, SEND_STATUS=0,
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
    undo_cfg = _live()
    for st in (1, 2):
        sent = []
        restore = _patch_j9(sent, [{"MASTER_NO": "176-62400004",
                                    "AMS_RECORD": dict(AMS_OUT, SEND_STATUS=st)}])
        try:
            try:
                mp.submit_master({"mawb": "176-62400004", "reviewer": "马殿齐", "ams": dict(AMS_OUT)})
                raise AssertionError(f"SEND_STATUS={st} 的主单居然放行了")
            except company_api.CompanyLocked as e:
                assert str(st) in str(e), f"要说清是哪一档：{e}"
            assert len(sent) == 1, f"闸门拦下时绝不能发出写请求（SEND_STATUS={st}）"
        finally:
            restore()
    assert not (tmp / "master_submitted.json").exists(), "两条被拦的都该没落台账"
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
