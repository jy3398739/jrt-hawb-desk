# -*- coding: utf-8 -*-
"""「暂存」回归：核对结果存到服务器，但**绝不**发公司。

这一层是需求二与需求三的地基：
  - 录入员要能读到别人核对过的内容（所以值必须落服务器，不能只在浏览器里）；
  - 解析正确率要能按人分账（所以每次改动是谁改的、改了哪几列，必须逐字段留下）。
本模块不产生任何外部调用；第一条用例就是把外发口打死来验这件事。
"""
import json
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import company_api
import config
import server
import store

MAWB, HAWB, STEM = "235-96146363", "CLA26090022", "CLA26090022"


def _model_air(**over):
    """output/air 里那份"模型说的原样"：暂存时的基线。"""
    air = {"MAWB_NO": MAWB, "HAWB_NO": HAWB, "ORIGIN_NAME": "BJS", "DEST_NAME": "LAX",
           "PIECES": 1, "WEIGHT": 170.0, "GOODS_INFO": "STEEL PARTS", "SEND_STATUS": "PENDING"}
    air.update(over)
    return air


def _env(tmp: Path, air: dict | None = None):
    """把暂存目录、L3、质检、台账、账号表全指到临时目录；返回旧值供还原。"""
    old = (config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
           config.SUBMIT_LEDGER, config.ARCHIVE_DIR, auth.USERS_FILE)
    config.STAGED_DIR = tmp / "staged"
    config.OUTPUT_AIR_DIR = tmp / "air"
    config.OUTPUT_QC_DIR = tmp / "qc"
    config.SUBMIT_LEDGER = tmp / "submitted.json"
    config.ARCHIVE_DIR = tmp / "archive"
    auth.USERS_FILE = tmp / "users.json"
    config.STAGED_DIR.mkdir(parents=True)
    config.OUTPUT_AIR_DIR.mkdir(parents=True)
    config.OUTPUT_QC_DIR.mkdir(parents=True)
    (config.ARCHIVE_DIR / STEM).mkdir(parents=True)
    a = _model_air() if air is None else air
    (config.OUTPUT_AIR_DIR / f"{STEM}.json").write_text(
        json.dumps(a, ensure_ascii=False), encoding="utf-8")
    (config.OUTPUT_QC_DIR / f"{STEM}.json").write_text(json.dumps(
        {"source_name": STEM + ".pdf", "channel": "vlm", "model": "qwen3.8-flash",
         "model_choice": "qwen38-flash-bailian", "processed_at": "2026-10-08T10:00:00",
         "uploader": "马殿齐", "uploader_role": "reviewer", "flags": [], "error": ""},
        ensure_ascii=False), encoding="utf-8")
    return old


def _undo(old):
    (config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
     config.SUBMIT_LEDGER, config.ARCHIVE_DIR, auth.USERS_FILE) = old


def _client_as(tmp: Path, name: str, role: str):
    """临时账号表 + 以指定角色登录的会话（种子 admin 之外再建一个同名账号）。"""
    auth.ensure_seed()
    pw = "pw-123456"
    u, err = auth.set_password(name, pw, role)
    assert err is None, err
    client = TestClient(server.app)
    r = client.post("/login", json={"name": name, "password": pw})
    assert r.status_code == 200, f"{name}({role}) 登录失败：{r.text[:120]}"
    return client


def _ticket(**over):
    tk = {"stem": STEM, "filename": STEM + ".pdf",
          "air_reviewed": {"MAWB_NO": MAWB, "HAWB_NO": HAWB, "ORIGIN_NAME": "BJS",
                           "DEST_NAME": "LAX", "PIECES": 1, "WEIGHT": 170.0,
                           "GOODS_INFO": "STEEL PARTS", "SEND_STATUS": "PENDING"},
          "acked_flags": [], "edited_fields": []}
    tk.update(over)
    return tk


def test_stage_never_calls_the_company():
    """暂存这个动作必须**结构上**发不出去：三个对外写出口全打成 raise AssertionError，
    点暂存仍要回 200 并把值写进服务器。

    这条不是形式：用户反复强调"发送公司是人工动作，AI 不许点"。把它写成一条会失败的测试，
    而不是写在注释里，才能保证以后有人图省事在暂存里顺手加一次回传时立刻变红。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("暂存居然碰了公司接口"))
    real = (server._company_submit, company_api.submit_order, company_api.submit_master)
    try:
        server._company_submit, company_api.submit_order = boom, boom
        company_api.submit_master = boom
        client = _client_as(tmp, "马殿齐", "reviewer")
        r = client.post("/stage", json={"tickets": [_ticket()]})
        assert r.status_code == 200, f"暂存被公司接口绊住了：{r.status_code} {r.text[:200]}"
        assert (config.STAGED_DIR / f"{STEM}.json").is_file(), "暂存没落服务器，别人读不到"
    finally:
        server._company_submit, company_api.submit_order, company_api.submit_master = real
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_stage_needs_a_login_session():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        r = TestClient(server.app).post("/stage", json={"tickets": [_ticket()]})
        assert r.status_code == 401, "暂存存的是票面内容，不登录不许写"
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_stage_saves_human_values_and_the_per_field_diff():
    """改了什么值必须逐字段落盘：从前只有 `edited_fields` 那串字段名，值存在哪儿没人知道
    （`edited_fields` 本身还来自浏览器，前端说没改就是没改）。

    现在差异由服务器算：拿请求里的值和 output/air 那份模型原样比。
    于是两种撒谎都拦得住——前端报空列表但真改了值 → 照样记；前端报了个没改的字段名 → 不记。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        tk = _ticket()
        tk["air_reviewed"]["DEST_NAME"] = "ORD"          # 模型把到达机场读错了
        tk["air_reviewed"]["GOODS_INFO"] = "STEEL PARTS HS 7326"
        tk["edited_fields"] = []                          # 前端谎报"没改过"
        client = _client_as(tmp, "马殿齐", "reviewer")
        r = client.post("/stage", json={"tickets": [tk]})
        assert r.status_code == 200, r.text
        rec = json.loads((config.STAGED_DIR / f"{STEM}.json").read_text(encoding="utf-8"))
        assert rec["air_final"]["DEST_NAME"] == "ORD", "人工值没存下来，录入员来读还是模型那一版"
        assert rec["air_model"]["DEST_NAME"] == "LAX", "模型原样被人工值覆盖了：以后没法对照"
        assert set(rec["edits"]) == {"DEST_NAME", "GOODS_INFO"}, f"逐字段差异算错：{rec['edits']}"
        assert rec["edits"]["DEST_NAME"] == {"from": "LAX", "to": "ORD"}, rec["edits"]["DEST_NAME"]
        assert rec["stager"] == "马殿齐" and rec["stager_role"] == "reviewer", rec
        assert rec["model"] == "qwen3.8-flash", "暂存里没带模型名：按模型分账要再去翻 qc"
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_each_stage_appends_and_the_baseline_stays_the_model():
    """同一张票被两个人先后暂存：改动要各记各的，基线始终是模型那一版。

    这正是需求三要的分工证据：制单员改了 3 个字段、录入员又改 1 个，
    盖成一格就只剩"这张票改过 4 次"，答不了"模型读得准不准"和"还需要不需要人"。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        t1 = _ticket()
        t1["air_reviewed"]["DEST_NAME"] = "ORD"
        c1 = _client_as(tmp, "马殿齐", "reviewer")
        assert c1.post("/stage", json={"tickets": [t1]}).status_code == 200, c1.post("/stage", json={"tickets": [t1]}).text

        t2 = _ticket()
        t2["air_reviewed"]["DEST_NAME"] = "ORD"
        t2["air_reviewed"]["PIECES"] = 2                 # 录入员只再改了件数
        c2 = _client_as(tmp, "刘明", "inputter")
        r2 = c2.post("/stage", json={"tickets": [t2]})
        assert r2.status_code == 200, r2.text

        rec = json.loads((config.STAGED_DIR / f"{STEM}.json").read_text(encoding="utf-8"))
        assert rec["air_model"]["DEST_NAME"] == "LAX", "基线被第二次暂存改写了：模型说过的话查不回来了"
        assert rec["air_final"]["PIECES"] == 2, "最新人工值没跟上"
        events = rec["events"]
        assert [e["role"] for e in events] == ["reviewer", "inputter"], events
        assert set(events[0]["edits"]) == {"DEST_NAME"}, events[0]["edits"]
        assert set(events[1]["edits"]) == {"PIECES"}, f"第二次该只记录入员改的那一列：{events[1]['edits']}"
        assert events[1]["by"] == "刘明"
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_reparse_does_not_move_the_model_baseline():
    """基线一旦在第一次暂存时快照下来就不再动。

    这条防的是重解析：`output/air/<stem>.json` 在重跑那张票时会被**覆盖**（换个模型再读一遍，
    或第一次读砸了重来）。要是每次都现读 air 当基线，"模型当初到底读了什么"就在第二次
    暂存那一刻永久丢失——而逐字段准确率要对照的正是第一次那份原样。
    """
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        t1 = _ticket()
        t1["air_reviewed"]["DEST_NAME"] = "ORD"
        assert _client_as(tmp, "马殿齐", "reviewer").post(
            "/stage", json={"tickets": [t1]}).status_code == 200

        # 第二次暂存之前，这张票被重跑了一遍：这回模型读成了 SFO
        (config.OUTPUT_AIR_DIR / f"{STEM}.json").write_text(
            json.dumps(_model_air(DEST_NAME="SFO", GOODS_INFO="STEEL PARTS HS 7326"),
                       ensure_ascii=False), encoding="utf-8")

        t2 = _ticket()
        t2["air_reviewed"]["DEST_NAME"] = "ORD"
        t2["air_reviewed"]["GOODS_INFO"] = "STEEL PARTS HS 7326"
        assert _client_as(tmp, "马殿齐", "reviewer").post(
            "/stage", json={"tickets": [t2]}).status_code == 200

        rec = json.loads((config.STAGED_DIR / f"{STEM}.json").read_text(encoding="utf-8"))
        assert rec["air_model"]["DEST_NAME"] == "LAX", \
            "基线被重解析带着走了：现在记的是 %r，第一次那份 LAX 从此查无对证" % (
                rec["air_model"]["DEST_NAME"],)
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_stage_refuses_a_stem_that_is_not_a_single_name():
    """stem 是前端给的，而暂存文件的路径就是 `staged/<stem>.json`——不挡路径就等于
    把"往服务器任意位置写文件"开放给任何登录会话。/ticket 那侧从前只认"目录存在"，
    写文件的入口比读文件的入口更需要这道闸。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        client = _client_as(tmp, "马殿齐", "reviewer")
        for bad in ("..", "../escape", r"..\..\windows\x", "a/b", "", "  "):
            tk = _ticket()
            tk["stem"] = bad
            r = client.post("/stage", json={"tickets": [tk]})
            assert r.status_code == 400, f"{bad!r} 居然被收下了：{r.status_code} {r.text[:120]}"
        stray = list(config.STAGED_DIR.glob("*.json"))
        assert stray == [], f"非法 stem 写出了文件：{stray}"
        assert not (tmp / "escape.json").exists(), "../escape 越出暂存目录写了文件"
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_stem_guard_is_one_rule_not_two_copies():
    """读原件的入口（server._safe_stem）与写暂存的入口（store.safe_stem）必须同一条判定。

    两处各写一份"是不是单段名字"迟早分叉——一边放过 `a\\b` 另一边拦住，就会出现"能存不能读"
    或反过来的票。写文件那侧要求更高（读错了只是 404，写错了等于在服务器上任意位置落文件），
    所以真身在 store 这边，server 转调它。
    """
    for bad in ("..", ".", "", "a/b", r"a\\b", "/", None, "CLA/..", "  /  "):
        assert store.safe_stem(bad) is None, f"store 放过了 {bad!r}"
        assert server._safe_stem(bad) is None, f"server 与 store 判定分叉了：{bad!r}"
    for ok in ("CLA26090022", "某分单 TAO1234567", "EMAIL COPY - HAWB No_ BJS001512355"):
        assert store.safe_stem(ok) == ok and server._safe_stem(ok) == ok, ok


def test_format_only_churn_is_not_counted_as_an_edit():
    """改了个空格、170.0 写成 170、单号少了根连字符——这些不算改。

    统计的分子要的是"模型读错了"，不是"人重新敲了一遍"。把它们记成错误，
    对接可行性那个数会被人为压低，而压低方向的误判比抬高更贵（会否掉本来能上的东西）。
    数字列按数值比，主/分单号按归一化比，其余按空白折叠后的原文比（大小写仍算差异：
    Italy→ITALY 是人真动过）。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        tk = _ticket()
        tk["air_reviewed"]["MAWB_NO"] = "23596146363"      # 少连字符
        tk["air_reviewed"]["WEIGHT"] = "170"               # 字符串 vs 170.0
        tk["air_reviewed"]["GOODS_INFO"] = "STEEL  PARTS"  # 中间两个空格
        client = _client_as(tmp, "马殿齐", "reviewer")
        r = client.post("/stage", json={"tickets": [tk]})
        assert r.status_code == 200, r.text
        rec = json.loads((config.STAGED_DIR / f"{STEM}.json").read_text(encoding="utf-8"))
        assert rec["edits"] == {}, f"格式差异被当成改动了：{rec['edits']}"

        tk2 = _ticket()
        tk2["air_reviewed"]["GOODS_INFO"] = "steel parts"   # 大小写变了：这算人动过
        r2 = _client_as(tmp, "马殿齐", "reviewer").post("/stage", json={"tickets": [tk2]})
        assert r2.status_code == 200, r2.text
        rec2 = json.loads((config.STAGED_DIR / f"{STEM}.json").read_text(encoding="utf-8"))
        assert set(rec2["edits"]) == {"GOODS_INFO"}, rec2["edits"]
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_stage_keeps_a_ticket_visible_without_complete_numbers_but_out_of_the_index():
    """暂存不卡号：存工作进度≠交付公司，缺号也让人存得下来（那张票还是同一张，
    同事可以接着补）。但复合键索引仍只收号齐全的——缺号的票本来就检索不到，不用特判。

    与 /submit 的区别是刻意的：那边缺号必须 400，因为进公司库和进索引都靠号认票。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        tk = _ticket()
        tk["air_reviewed"]["HAWB_NO"] = ""
        client = _client_as(tmp, "马殿齐", "reviewer")
        r = client.post("/stage", json={"tickets": [tk]})
        assert r.status_code == 200, f"暂存不该因为缺分单号被拒：{r.status_code} {r.text[:200]}"
        rec = json.loads((config.STAGED_DIR / f"{STEM}.json").read_text(encoding="utf-8"))
        assert rec["air_final"]["HAWB_NO"] == ""
        assert store.lookup_stem(MAWB, "") is None, "缺号的暂存票混进了复合键索引"
        assert [e["stem"] for e in store.ticket_rows(mawb=MAWB, state="staged")] == [STEM], \
            "暂存的票要能按主单号被列出来，否则需求一的检索还是看不见它"
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_staged_ticket_can_be_reopened_by_another_role():
    """录入员要能打开别人暂存的票——这是需求二"录入员核对之后再发公司"的读侧。

    `/ticket/{stem}` 给的是模型口径（air/raw/qc），读不到人工值；这里给的是暂存记录本身。
    任何登录角色都能读（内部部署，登录是门槛），但没暂存过要说清楚为什么没有。
    """
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        tk = _ticket()
        tk["air_reviewed"]["DEST_NAME"] = "ORD"
        assert _client_as(tmp, "马殿齐", "reviewer").post("/stage", json={"tickets": [tk]}).status_code == 200

        anon = TestClient(server.app)
        assert anon.get("/staged/" + STEM).status_code == 401, "暂存里有整张票面内容，不登录不给读"

        inp = _client_as(tmp, "刘明", "inputter")
        r = inp.get("/staged/" + STEM)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["air_final"]["DEST_NAME"] == "ORD", "录入员读到的还是模型那一版"
        assert j["air_model"]["DEST_NAME"] == "LAX"
        assert j["stager"] == "马殿齐" and j["reviewed_by_inputter"] is False, j
        assert j["edits"]["DEST_NAME"] == {"from": "LAX", "to": "ORD"}, j["edits"]
        assert len(j["events"]) == 1

        miss = inp.get("/staged/从没暂存过的票")
        assert miss.status_code == 404 and "暂存" in miss.json()["detail"], miss.text
        bad = inp.get("/staged/" + "a/b")
        assert bad.status_code in (400, 404), f"带路径的 stem 要挡：{bad.status_code}"
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_lists_staged_tickets_under_that_mawb():
    """主单检索的结果里必须看得见"本机暂存过、公司还没有"的分单。

    名下分单表读的是公司接口，公司只认提交过的票——制单员刚暂存的票要是只出现在
    「本台待提交」那种浏览器局部视图里，换个人查主单就还是"没有分单"（2026-09-29 用户实测
    过的就是这个，当时的补丁 pendingUnder 只治本机本人）。现在由服务器把 pending 一起回。
    """
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        store.stage_ticket(STEM, {"MAWB_NO": MAWB, "HAWB_NO": HAWB, "ORIGIN_NAME": "BJS",
                                  "DEST_NAME": "ORD"}, "马殿齐", "reviewer")
        j = company_api.search_mawb(MAWB)
        pend = j["pending"]
        assert [p["stem"] for p in pend] == [STEM], pend
        assert pend[0]["hawb"] == HAWB and pend[0]["stager"] == "马殿齐", pend[0]
        assert pend[0]["has_original"] is True, "归档在机器上却报没有原件，前端就不给开票面了"
        assert pend[0]["reviewed_by_inputter"] is False

        assert company_api.search_mawb("111-11111111")["pending"] == [], "别的主单不该串进来"
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_lists_tickets_that_were_only_uploaded_too():
    """只上传、只解析、还没人暂存的票也要在 pending 里。

    需求一的原话是"制单员上传分单要真实上传到服务器，录入员要能检索到主单和分单（分单原件）"，
    并没有要求制单员先点暂存。少了这一档，检索就只对"勤快点了暂存的人"成立；
    而从前的浏览器名单（pendingUnder）恰恰连解析过的票一起报，不能改完反而更看不见。
    """
    tmp = Path(tempfile.mkdtemp(prefix="hawb_stage_"))
    old = _env(tmp)
    try:
        store.stage_ticket(STEM, {"MAWB_NO": MAWB, "HAWB_NO": HAWB}, "马殿齐", "reviewer")
        store.save_result("另张只解析的票", {"MAWB_NO": MAWB, "HAWB_NO": "ONLYPARSED"},
                          {"MAWB_NO": MAWB, "HAWB_NO": "ONLYPARSED", "DEST_NAME": "AMS"})
        store.save_qc("另张只解析的票", {"source_name": "另张只解析的票.pdf", "channel": "vlm",
                                        "processed_at": "2026-10-08T11:00:00", "flags": [],
                                        "error": ""})
        (config.ARCHIVE_DIR / "另张只解析的票").mkdir()

        pend = company_api.search_mawb(MAWB)["pending"]
        assert sorted(p["hawb"] for p in pend) == sorted([HAWB, "ONLYPARSED"]), pend
        bystem = {p["stem"]: p for p in pend}
        assert bystem[STEM]["state"] == "staged" and bystem[STEM]["stager"] == "马殿齐", bystem[STEM]
        only = bystem["另张只解析的票"]
        assert only["state"] == "parsed" and only["stager"] == "", \
            "解析档要老实标成解析，别让人以为已经有人核对过"
        assert only["has_original"] is True, "原件在归档却没报 has_original，前端就不给开票面"
    finally:
        _undo(old)
        shutil.rmtree(tmp, ignore_errors=True)
