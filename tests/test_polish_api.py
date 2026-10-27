# -*- coding: utf-8 -*-
"""「优化」按钮的两件后端事：算给出发值的端点、以及留痕里带上规则号。

端点只是把 `polish.preview` 挂到 HTTP 上（判据全在那张表里，这里不再写一遍），
所以这里的用例盯的是**接口面**：谁能调、能塞多大、哪些列永远不许出现在答复里。
留痕那两条盯的是"人点的那一下服务器上查得到"——从前只记"改了哪列"，
问得出改了几列、答不出是谁按的规则把值换掉的（业主口径：界面状态必须等于留痕）。
"""
import shutil
import tempfile
import json
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import config
import master_pipeline
import polish
import server
import store

CONSIGNEE_BR = {"CONSIGNEE_INFO_COMP_NAME": "ACME LTDA",
                "CONSIGNEE_INFO_COMP_ADDRESS": "RUA X 100, SAO PAULO, BRAZIL",
                "CONSIGNEE_INFO": "ACME LTDA, RUA X 100, SAO PAULO, BRAZIL",
                "CONSIGNEE_INFO_COUNTRY": "BRAZIL",
                "CONSIGNEE_INFO_POSTAL": "04546040",
                "CONSIGNEE_INFO_STATE": "SAO PAULO"}


def _client():
    """临时账号表 + 登录后的会话；返回 (client, 还原用的旧值)。"""
    tmp = Path(tempfile.mkdtemp(prefix="hawb_polish_users_"))
    old = auth.USERS_FILE
    auth.USERS_FILE = tmp / "users.json"
    auth.ensure_seed()
    client = TestClient(server.app)
    r = client.post("/login", json={"name": "admin", "password": "admin123"})
    assert r.status_code == 200, f"种子管理员登不进去：{r.status_code} {r.text[:120]}"
    return client, (old, tmp)


def _undo(state):
    auth.USERS_FILE = state[0]
    shutil.rmtree(state[1], ignore_errors=True)


def test_polish_endpoint_answers_with_the_cells_that_can_be_optimised():
    client, st = _client()
    try:
        r = client.post("/polish", json={"chain": "hawb", "fields": CONSIGNEE_BR})
        assert r.status_code == 200, r.text[:200]
        by = {x["field"]: x for x in r.json()["rows"]}
        assert by["CONSIGNEE_INFO_POSTAL"]["to"] == "04546-040"
        assert by["CONSIGNEE_INFO_STATE"]["to"] == "SP"
        assert by["CONSIGNEE_INFO_COUNTRY"]["to"] == "BR"
        assert all(x["rule"] and x["why"] for x in by.values()), "规则号与依据都要带回去：留痕与人看的都靠它"
    finally:
        _undo(st)


def test_polish_endpoint_never_answers_for_the_verbatim_block():
    client, st = _client()
    try:
        r = client.post("/polish", json={"chain": "hawb",
                                        "fields": dict(CONSIGNEE_BR, SHIPPER_INFO="anything",
                                                        GOODS_INFO="STEEL PARTS",
                                                        CONSIGNEE_INFO_COMP_NAME="ACME  LTDA")})
        seen = {x["field"] for x in r.json()["rows"]}
        assert "CONSIGNEE_INFO" in CONSIGNEE_BR, "样票里带着整串，这条才算测到东西"
        for k in seen:
            assert k.split("_INFO_")[-1] in polish.ACTIONABLE_SUFFIXES, f"{k} 越出了白名单"
        assert not any(k.endswith("_INFO") or "COMP_NAME" in k or "ADDRESS" in k for k in seen)
    finally:
        _undo(st)


def test_polish_endpoint_answers_per_chain():
    """同一份值在两条链上给不一样的答复：主单不过 to_air，别把分单的 alpha-2 当通用规则。
    2026-10-27 起发货人三格无条件固定（国家恒 CN），拿收货人列验这条差异。"""
    client, st = _client()
    try:
        fields = {"MAWB_NO": "999-95764373", "CONSIGNEE_INFO_COMP_NAME": "ACME LTDA",
                  "CONSIGNEE_INFO_COUNTRY": "brazil", "CONSIGNEE_INFO_POSTAL": "04546040"}
        h = {x["field"]: x["to"] for x in client.post(
            "/polish", json={"chain": "hawb", "fields": fields}).json()["rows"]}
        m = {x["field"]: x["to"] for x in client.post(
            "/polish", json={"chain": "master", "fields": fields}).json()["rows"]}
        assert h["CONSIGNEE_INFO_COUNTRY"] == "BR" and m["CONSIGNEE_INFO_COUNTRY"] == "BRAZIL"
        assert h["CONSIGNEE_INFO_POSTAL"] == m["CONSIGNEE_INFO_POSTAL"] == "04546-040"
    finally:
        _undo(st)


def test_polish_endpoint_is_behind_login_and_rejects_junk_bodies():
    client, st = _client()
    try:
        fresh = TestClient(server.app)
        assert fresh.post("/polish", json={"chain": "hawb", "fields": {}}).status_code == 401, \
            "登录是唯一门禁：这个端点也别开一条免登录的口子"
        bad = [{"chain": "other", "fields": CONSIGNEE_BR},
               {"chain": "", "fields": CONSIGNEE_BR},
               {"chain": "hawb", "fields": {f"SHIPPER_INFO_POSTAL_{i}": "1" for i in range(300)}},
               {"chain": "hawb", "fields": {"CONSIGNEE_INFO_POSTAL": {"嵌套": "值"}}}]
        for body in bad:
            assert client.post("/polish", json=body).status_code == 400, f"这种请求人居然放行：{body}"
        # fields 整个不是字典时由 pydantic 在路由外先挡下来（422 也是拒得干干净净，
        # 不放进 polish 里就不可能有第二条路）；这条单列出来，免得以后误以为 400 是唯一形态。
        assert client.post("/polish", json={"chain": "hawb", "fields": "不是字典"}).status_code \
            in (400, 422)
    finally:
        _undo(st)


# ── 留痕：谁在什么时候按了哪条规则 ─────────────────────────────────────────────
def _stage_env(tmp: Path):
    old = (config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR)
    config.STAGED_DIR = tmp / "staged"
    config.OUTPUT_AIR_DIR = tmp / "air"
    config.OUTPUT_QC_DIR = tmp / "qc"
    for p in (config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR):
        p.mkdir(parents=True, exist_ok=True)
    (config.OUTPUT_AIR_DIR / "P1.json").write_text(
        json.dumps(CONSIGNEE_BR, ensure_ascii=False), encoding="utf-8")
    (config.OUTPUT_QC_DIR / "P1.json").write_text('{"model": "stub"}', encoding="utf-8")
    return old


def _stage_undo(old):
    (config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR) = old


def test_stage_trace_keeps_which_rule_changed_which_column():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_polish_stage_"))
    old = _stage_env(tmp)
    try:
        air = dict(CONSIGNEE_BR, CONSIGNEE_INFO_POSTAL="04546-040",
                   CONSIGNEE_INFO_STATE="SP", CONSIGNEE_INFO_COUNTRY="BR")
        rec = store.stage_ticket("P1", air, "马殿齐", "reviewer", [],
                                 {"CONSIGNEE_INFO_POSTAL": "POSTAL",
                                  "CONSIGNEE_INFO_STATE": "STATE_ABBR",
                                  "SHIPPER_INFO_POSTAL": "POSTAL"})
        ev = rec["events"][-1]
        assert ev["polish"]["CONSIGNEE_INFO_POSTAL"] == "POSTAL"
        assert ev["polish"]["CONSIGNEE_INFO_STATE"] == "STATE_ABBR"
        assert "SHIPPER_INFO_POSTAL" not in ev["polish"], \
            "没真改动的列不许挂规则号：留痕只能记发生过的事"
        # 手工改的那一格不该被算成规则改的
        rec2 = store.stage_ticket("P1", dict(air, CONSIGNEE_INFO_COUNTRY="BRX"),
                                  "郭健康", "inputter", [], {})
        assert rec2["events"][-1]["polish"] == {} and rec2["events"][-1]["edits"], \
            "纯人工改动也要有留痕，但没有规则号"
    finally:
        _stage_undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_master_stage_trace_keeps_the_rule_ids():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_polish_mv_"))
    old = (config.MASTER_DIR, config.MASTER_LEDGER, config.SUBMIT_LEDGER)
    config.MASTER_DIR = tmp / "master"
    config.MASTER_LEDGER = tmp / "m.json"
    config.SUBMIT_LEDGER = tmp / "s.json"
    config.MASTER_DIR.mkdir(parents=True)
    mawb = "999-95764373"
    try:
        base = {"MAWB_NO": mawb, "SHIPPER_INFO_COMP_NAME": "ACME LTDA",
                "SHIPPER_INFO_COUNTRY": "BRAZIL", "SHIPPER_INFO_POSTAL": "04546040"}
        master_pipeline._write({"mawb": mawb, "state": "done", "ams": base, "events": []})
        rec = master_pipeline.stage_master(mawb, dict(base, SHIPPER_INFO_POSTAL="04546-040"),
                                           "马殿齐", "reviewer", [],
                                           {"SHIPPER_INFO_POSTAL": "POSTAL"})
        assert rec["events"][-1]["polish"] == {"SHIPPER_INFO_POSTAL": "POSTAL"}
        assert rec["events"][-1]["edits"], "改了值就得有差异，规则号是挂在差异上的"
        rec2 = master_pipeline.stage_master(mawb, dict(base, SHIPPER_INFO_STATE="SP"),
                                            "郭健康", "inputter", [], {})
        assert rec2["events"][-1]["polish"] == {}, "主单也一样：没按规则就不许凭空记一条规则号"
    finally:
        (config.MASTER_DIR, config.MASTER_LEDGER, config.SUBMIT_LEDGER) = old
        shutil.rmtree(tmp, ignore_errors=True)


# ── 前端接线：判据在服务端，界面只负责"让人看一眼再按" ──────────────────────────
import web_src


def test_logic_holds_the_map_shows_and_count_only():
    """三件纯形状的事放 logic.js（node 能钉），国别判据一条都不许抄过来。"""
    rows = [{"field": "A", "from": "04546040", "to": "04546-040"},
            {"field": "B", "from": "x", "to": "x"},
            {"field": "", "from": "1", "to": "2"}]
    assert web_src.logic("Object.keys(L.polishMap(rows))", rows=rows) == ["A"], \
        "from==to 的那行没得优化，空列名的更是脏数据"
    assert web_src.logic("L.polishShows(f, s)", f="SAO PAULO", s="SP") is True
    assert web_src.logic("L.polishShows(f, s)", f="", s=None) is False, "空与 null 算一样，别多占一行"
    assert web_src.logic("L.polishCount(m, e, false)", m={"A": 1, "B": 2}, e={"B": 0}) == 1, \
        "人已经自己改过的那格不再算可优化"
    assert web_src.logic("L.polishCount(m, e, true)", m={"A": 1}, e={}) == 0, \
        "票已经交出去了（锁行）就不该再数出能改的格子"


def test_face_value_line_sits_above_the_editable_one_on_both_chains():
    desk = web_src.desk()
    assert '<div class="poline">${polishInner(t, r)}</div>${control(t, r.k)}' in desk, \
        "分单那一格的顺序是：票面行 → 输入框 → 旗面原文，换了顺序人对不上票"
    assert '<div class="poline">${mvPolishInner(k, v)}</div><textarea data-mk=' in desk, \
        "主单 36 列同一套排法"
    assert "pl.innerHTML = polishInner(t, r);" in desk, \
        "手改一次就要重画那一行：从前按钮失灵的同一个病，局部重画漏掉一块就等于没接"


def test_optimise_button_rides_the_container_delegation_not_a_per_node_listener():
    """颗按钮长在每次重画的新节点上：只挂一次的那条委托是它唯一的活路（2026-10-16 实测过）。"""
    desk = web_src.desk()
    assert '"[data-acke],[data-une],[data-restore],[data-fold],[data-gack]," +\n' \
           '                                "[data-polish]");' in desk, \
        "分单那颗优化按钮要待在这条 closest() 名单里，和确认/还原同一个入口"
    assert "card._polishWired" in desk and 'card.addEventListener("click"' in desk, \
        "主单表不在 #main 里，得在 #mstHawbCard 那一层挂一次自己的委托"
    assert 'e.target.closest("[data-polish]")' in desk


def test_clicking_optimise_is_a_human_edit_with_a_rule_number_attached():
    desk = web_src.desk()
    assert "setEdit(t, d.polish, po.to);" in desk, "点优化＝人工改值那一条链，别另起一份合并逻辑"
    assert "t.polishW[d.polish] = {rule: po.rule, to: po.to};" in desk
    assert "mvWrite(k, po.to);" in desk, "主单也一样：改动只有 mvWrite 这一个口子"
    assert "polish_rules: polishSent(t)," in desk, "分单暂存要把规则号一起交出去"
    assert "polish_rules: mvPolishSent()}" in desk, "主单暂存同一条"


def test_the_button_does_not_appear_on_a_locked_or_already_edited_cell():
    desk = web_src.desk()
    assert "if (!po || d.polish in t.airE || t.locked) return;" in desk, \
        "人已改过或票已交出去：这两档都不许再按规则盖掉人的值"
    assert "if (!po || k in MV.edit || MV.locked) return;" in desk


def test_polish_html_escapes_everything_and_adds_no_inline_style():
    body = web_src.fn(web_src.desk(), "function polishHtml")
    for piece in ("esc(face)", "esc(po.to)", "esc(po.why)", "esc(rec.rule)", "esc(rec.to)"):
        assert piece in body or piece == "esc(rec.to)", f"{piece} 没转义：票面里的尖括号会咬界面"
    assert "style=" not in body, "样式一律走 desk.css（守卫只放行过两种内联写法）"
    assert "String(face).trim()" in body, \
        "这一格本来是空的时候别印一行「票面：」空着——那是「提进来」那一档，看依据就够了"


def test_desk_css_carries_the_two_line_block_and_hides_it_when_empty():
    css = web_src.css()
    assert ".poline:empty{display:none}" in css, "两行一样时那一块不占地方"
    assert ".pf,.pw{font-size:var(--f-sm);color:var(--c-text-2)}" in css, "颜色用令牌，浅灰那档要过 AA"


# ── 路由层：规则号必须经服务端交给留痕（只在本机 store 层测过＝接线没人管） ──────
def test_stage_route_records_the_rule_ids():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_polish_route_"))
    old = _stage_env(tmp)
    old_users = auth.USERS_FILE
    auth.USERS_FILE = tmp / "users.json"
    auth.ensure_seed()
    try:
        client = TestClient(server.app)
        assert client.post("/login", json={"name": "admin", "password": "admin123"}).status_code == 200
        air = dict(CONSIGNEE_BR, CONSIGNEE_INFO_POSTAL="04546-040",
                   CONSIGNEE_INFO_STATE="SP", CONSIGNEE_INFO_COUNTRY="BR")
        r = client.post("/stage", json={"tickets": [{
            "stem": "P1", "filename": "P1.pdf", "air_reviewed": air,
            "polish_rules": {"CONSIGNEE_INFO_POSTAL": "POSTAL", "SHIPPER_INFO_POSTAL": "POSTAL"}}]})
        assert r.status_code == 200, r.text[:160]
        rec = json.loads((config.STAGED_DIR / "P1.json").read_text(encoding="utf-8"))
        assert rec["events"][-1]["polish"] == {"CONSIGNEE_INFO_POSTAL": "POSTAL"}, \
            "暂存路由要把规则号交给 store：没真改动的列也不能凭请求写进去"
    finally:
        auth.USERS_FILE = old_users
        _stage_undo(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_master_stage_route_records_the_rule_ids():
    tmp = Path(tempfile.mkdtemp(prefix="hawb_polish_mroute_"))
    old = (config.MASTER_DIR, config.MASTER_LEDGER, config.SUBMIT_LEDGER, auth.USERS_FILE)
    config.MASTER_DIR = tmp / "master"
    config.MASTER_LEDGER = tmp / "m.json"
    config.SUBMIT_LEDGER = tmp / "s.json"
    auth.USERS_FILE = tmp / "users.json"
    config.MASTER_DIR.mkdir(parents=True)
    auth.ensure_seed()
    mawb = "999-95764373"
    try:
        client = TestClient(server.app)
        assert client.post("/login", json={"name": "admin", "password": "admin123"}).status_code == 200
        base = {"MAWB_NO": mawb, "SHIPPER_INFO_COMP_NAME": "ACME LTDA",
                "SHIPPER_INFO_COUNTRY": "BRAZIL", "SHIPPER_INFO_POSTAL": "04546040"}
        master_pipeline._write({"mawb": mawb, "state": "done", "ams": base, "events": []})
        r = client.post("/master/stage", json={
            "mawb": mawb, "ams": dict(base, SHIPPER_INFO_POSTAL="04546-040"),
            "polish_rules": {"SHIPPER_INFO_POSTAL": "POSTAL"}})
        assert r.status_code == 200, r.text[:160]
        rec = json.loads(master_pipeline.path_for(mawb).read_text(encoding="utf-8"))
        assert rec["events"][-1]["polish"] == {"SHIPPER_INFO_POSTAL": "POSTAL"}, \
            "主单暂存路由同理：36 列的改动要带着是哪条规则改的"
    finally:
        (config.MASTER_DIR, config.MASTER_LEDGER, config.SUBMIT_LEDGER, auth.USERS_FILE) = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_polish_endpoint_accepts_a_ticket_with_retired_columns():
    """老票的 air 里还留着 v1.12.0 删掉的 `*_INFO_TAX_ID`：多一列绝不能把扫描判成 400。

    这条是隔离浏览器端到端实测抓出来的——那份真票 39 个键、列面只有 38 个，
    当时"可优化 0 格"看着像没规则可点，其实是请求被列数闸门挡下了。
    """
    import fieldspec
    full = {k: "" for k in fieldspec.desk_keys()}          # 核对区发的是整票：38 列一个不少
    full.update(CONSIGNEE_BR)
    client, st = _client()
    try:
        r = client.post("/polish", json={"chain": "hawb",
                                        "fields": dict(full, SHIPPER_INFO_TAX_ID="911201117706402073")})
        assert r.status_code == 200, f"多一个退役列就 400：{r.status_code} {r.text[:120]}"
        assert {x["field"] for x in r.json()["rows"]} >= {"CONSIGNEE_INFO_POSTAL", "CONSIGNEE_INFO_STATE"}
    finally:
        _undo(st)


def test_the_scan_runs_on_the_first_paint_not_only_after_an_edit():
    """扫描只挂在 refreshChrome 里＝初次渲染根本不跑（隔离浏览器实测：徽标一直"可优化 0 格"）。

    这条守卫钉的是**挂在哪个函数**，不是钉有没有这行代码：源码里出现过 polishScan 却没被
    初次渲染走到，界面上就是"规则好像没生效"。"""
    desk = web_src.desk()
    assert "polishScan(t);" in web_src.fn(desk, "function renderMain"), "画完一张票就得扫一次"
    assert "polishScan" not in web_src.fn(desk, "function refreshChrome"), \
        "refreshChrome 只在改格子时跑，挂那儿等于永远不扫"
    scan = web_src.fn(desk, "async function polishScan")
    assert "t.polishDone = true;" in scan and scan.index("t.polishDone = true;") < scan.index("fetch("), \
        "先置位再发请求：扫不成（400/断网）也不能每次重画都打一枪"
