# -*- coding: utf-8 -*-
"""统计口径：带规则号的改动**不算人工修正**，也不进分母，另记一档（业主 2026-10-21 定案）。

为什么两头都要扣：这一格要么是"模型留空、L3 从地址里把号提回来"，要么是"写法由国别规则补的"——
算进 doc/inp 就等于把人没做的事记成人修正过（正确率被压低），留在分母里又等于默认模型读对了
（正确率被抬高）。两头都不算、单独报一个数，才是这件事的真实形状：人确实点了一下，但那是接受规则，
不是纠正模型。

判据是"**最后一次动过这一列的事件**说了算"：先按规则、之后人又自己改过一次的那一列，
就是人工修正，不能因为它历史上被规则碰过就一直免记。
"""
import stats
import web_src


P = "CONSIGNEE_INFO_POSTAL"


def _row(model_val, final_val, events):
    model = {"MAWB_NO": "999-95764373", "HAWB_NO": "JRT1", P: model_val}
    final = {"MAWB_NO": "999-95764373", "HAWB_NO": "JRT1", P: final_val}
    staged = {"stem": "S1", "air_model": model, "air_final": final, "events": events,
              "stager": "马殿齐", "stager_role": "reviewer", "staged_at": "2026-10-21T10:00:00"}
    return stats.ticket_row("S1", {}, staged, {})


def _ev(edits, rules, role="reviewer", by="马殿齐"):
    return {"by": by, "role": role, "at": "2026-10-21T10:00:00",
            "edits": {k: {"from": "", "to": ""} for k in edits},
            "acked_flags": [], "polish": dict(rules)}


def test_a_rule_driven_cell_is_neither_a_correction_nor_denominator():
    r = _row("04546040", "04546-040", [_ev([P], {P: "POSTAL"})])
    assert r["total"] == 0 and r["doc"] == [] and r["inp"] == [], r
    assert r["polished"] == [P], "另记一档：这一列是按规则优化的"
    assert P not in r["fields"] and r["comparable"] == 2, \
        "分母也要扣掉：留着它就等于默认模型这一格读对了"


def test_the_last_touch_wins_a_hand_edit_after_the_rule_counts_again():
    r = _row("04546040", "04546-999", [_ev([P], {P: "POSTAL"}), _ev([P], {})])
    assert r["polished"] == [], "人后来自己改过：不能因为它被规则碰过就一直免记"
    assert r["total"] == 1 and r["doc"] == [P], r


def test_a_rule_cell_touched_first_by_the_other_role_still_splits_by_person():
    r = _row("04546040", "04546-040", [_ev([P], {P: "POSTAL"}, role="reviewer"),
                                        _ev([P], {P: "POSTAL"}, role="inputter", by="郭健康")])
    assert r["polished"] == [P] and r["doc"] == [] and r["inp"] == [], \
        "两边都是按规则点的，就不该有一边被记成修正"


def test_summary_reports_polished_as_its_own_line_and_names_it_for_export():
    rule = _row("04546040", "04546-040", [_ev([P], {P: "POSTAL"})])
    human = _row("A", "B", [_ev(["HAWB_NO"], {})])
    s = stats.summary([rule, human])
    assert s["edits_doc"] == 1 and s["edits_total"] == 1, s
    assert s["edits_polished"] == 1, "另记的那一档要能汇总，不然免掉的列数就凭空消失了"
    assert s["polish_by_rule"] == {"POSTAL": 1}, s
    assert "edits_polished" in stats._CN and "按规则" in stats._CN["edits_polished"], \
        "导出口径表用的是同一份中文列名，没登记就会在 Excel 里露出英文键"


def test_a_ticket_with_no_rule_marks_is_unchanged_from_the_old_shape():
    # HAWB_NO 这列也真的变了，两列都是人改的：这一档不该动它一根毫毛
    model = {"MAWB_NO": "999-95764373", "HAWB_NO": "JRT1", P: "A"}
    final = {"MAWB_NO": "999-95764373", "HAWB_NO": "JRT2", P: "B"}
    staged = {"stem": "S1", "air_model": model, "air_final": final,
              "events": [_ev(["HAWB_NO", P], {})], "stager": "马殿齐",
              "staged_at": "2026-10-21T10:00:00"}
    r = stats.ticket_row("S1", {}, staged, {})
    assert r["polished"] == [] and r["total"] == 2 and r["doc"] == sorted(["HAWB_NO", P]), \
        "没有规则号的票一个字都不该变：这一档只对真的按过优化的格子生效"


def test_the_board_says_out_loud_how_many_columns_left_the_denominator():
    """免记的列要从「可比」里走掉：口径条不写清楚，人只会以为模型那一版少读了几列。
    这一条盯的是页面会说，不是数算得对（算的那五用例在上头）。"""
    js = web_src.desk()
    kpi = js[js.index("const ST_KPI = ["):js.index("function statsQS(")]
    assert 'k: "edits_polished"' in kpi, "七张卡里没这颗：按规则优化的列数界面上看不见"
    body = web_src.fn(js, "function renderStats")
    assert "edits_polished" in body, "口径条没提这一档：可比列数凭空少了几列没人解释"


def test_the_excel_spells_the_rule_bucket_out_instead_of_dumping_a_dict():
    """拿去开会的往往是 Excel 那份：口径表要是把 polish_by_rule 原样写进去，落到单元格里就是
    `{'POSTAL': 1}` 这种 Python 字面量；逐张表少了「按规则优化的列」这一栏，页面能看见、Excel
    里查不到，两边各说一套。"""
    import io

    import pandas as pd
    from fastapi.testclient import TestClient

    import server
    import store
    import test_stats as T

    with T._world():
        m = T._parsed("P1", {"CONSIGNEE_INFO_COUNTRY": "BRAZIL",
                             "CONSIGNEE_INFO_POSTAL": "04546040"})
        store.stage_ticket("P1", dict(m, CONSIGNEE_INFO_POSTAL="04546-040"),
                           "马殿齐", "reviewer", None, {"CONSIGNEE_INFO_POSTAL": "POSTAL"})
        adm = TestClient(server.app)
        adm.post("/login", json={"name": "admin", "password": "admin123"})
        r = adm.get("/stats/export")
        assert r.status_code == 200, r.text
        head = {str(row["指标"]): row["值"]
                for _, row in pd.read_excel(io.BytesIO(r.content), sheet_name="口径").iterrows()}
        assert head["按规则优化的列（不算人工修正）"] == 1, head
        say = str(head["按规则优化（逐规则）"])
        assert "POSTAL" in say and "{" not in say, "逐规则那一格要写成话，不是字典：" + say
        per = pd.read_excel(io.BytesIO(r.content), sheet_name="每张票")
        assert list(per["按规则优化的列"]) == ["CONSIGNEE_INFO_POSTAL"], per
        assert per["改动列数"][0] == 0, "这一列不算人工修正：改动列数里必须没有它"


def test_a_legacy_row_still_carries_the_polish_key():
    """历史票没有逐字段留痕，但行形状要和可算的票一致：导出的每一栏都按同一组键摊，
    少一个键就是 Excel 里整列缺位、页面上一取就 KeyError。"""
    import test_stats as T

    with T._world():
        r = stats.ticket_row("L9", {}, {}, {"mawb": "999-95764373", "hawb": "L9"})
        assert r["legacy"] and r["polished"] == [], r
