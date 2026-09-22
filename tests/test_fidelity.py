# -*- coding: utf-8 -*-
"""保真层回归：L2⊆L1 正向回查 + 主单号漏抄反向核查，跑在 5 份真实票面夹具上。
夹具是从 output/ 冻结出来的（含 PDF 文字层转录），所以这条测试不碰 API、也不受后续批处理覆盖影响。
每份夹具的期望值是这次人工核过的结论，改期望值等于改判定口径，要看清 diff 再动。
"""
import json
from pathlib import Path

from fidelity import date_candidates, find_missing_mawb, find_missing_tax, verify_fidelity

FIX = Path(__file__).resolve().parent / "fixtures" / "fidelity"


def _cases():
    for p in sorted(FIX.glob("*.json")):
        yield p.name, json.loads(p.read_text(encoding="utf-8"))


def test_fidelity_fixture_count():
    assert len(list(FIX.glob("*.json"))) == 6, "保真夹具数量变了，确认是否被误删"


def test_forward_and_reverse_fidelity():
    bad = []
    for name, c in _cases():
        fid = verify_fidelity(c["raw"], c["transcript"])
        got = {"checked": fid["checked"], "passed": fid["passed"],
               "failed_fields": sorted(x["field"] for x in fid["failed"]),
               "missing_mawb": find_missing_mawb(c["raw"], c["transcript"]),
               "missing_tax": [list(t) for t in find_missing_tax(c["raw"], c["transcript"])]}
        if got != c["expect"]:
            bad.append(f"{name}({c['source_name']}):\n     期望 {c['expect']}\n     实际 {got}")
    assert not bad, f"{len(bad)} 份保真结果漂移：\n    " + "\n    ".join(bad)


def test_missing_mawb_not_triggered_by_phones():
    """反向核查的误报闸门：票面里的 11 位电话号码撞得上'3位+8位'形态，但过不了 IATA 校验位。"""
    for name, c in _cases():
        hits = find_missing_mawb(c["raw"], c["transcript"])
        for cand in hits:
            serial = cand.split("-")[1]
            assert int(serial[:7]) % 7 == int(serial[7]), f"{name}: {cand} 校验位不成立，是误报"


def test_known_omissions_are_still_caught():
    """两张真漏抄的票必须持续被抓到：GL26090330 / SALCN0002822 的 MAWB_NO 当时是空的。"""
    caught = {n: find_missing_mawb(c["raw"], c["transcript"]) for n, c in _cases()}
    hits = {n: v for n, v in caught.items() if v}
    assert set(hits) == {"gl26090330_hawb.json", "salcn0002822.json"}, hits
    assert hits["gl26090330_hawb.json"] == ["235-96146363"]
    assert hits["salcn0002822.json"] == ["232-19509626"]   # 空格分组也要认得


DATE_CASES = [  # (票面片段, 期望解析出的带年份日期)
    ("Issued at TAO 2026-09-20", {(2026, 9, 20)}),
    ("Executed on 20-Sep-26", {(2026, 9, 20)}),
    ("Executed on 21 SEP 2026", {(2026, 9, 21)}),
    ("Date Sep 21, 2026", {(2026, 9, 21)}),
    ("签发日期 2026/9/22 北京", {(2026, 9, 22)}),
    ("2026年9月22日", {(2026, 9, 22)}),
    ("制单日期 19-9月-26", {(2026, 9, 19)}),
    ("26/09/2026", {(2026, 9, 26)}),
    ("20260920 制单", {(2026, 9, 20)}),
    ("MAWB 020-19025661 HAWB TAO7268550", set()),   # 单号不能撞出日期来
    ("Montreal on 28 May 1999", {(1999, 5, 28)}),
]


def test_date_candidate_forms():
    for text, want in DATE_CASES:
        got = {c for c in date_candidates(text) if c[0]}
        assert want <= got, f"{text!r} 解析出 {got}，缺了 {want - got}"
        assert got <= want or want, f"{text!r} 多解析出 {got - want}"


def test_day_month_without_year_is_a_loose_candidate():
    """TK0089/24SEP 这类没有年份的写法：只当月日候选，年份另在票面核对。"""
    assert (None, 9, 24) in date_candidates("Flight TK0089/24SEP")


def test_yearless_ticket_date_still_verifies():
    """票面日期不带年份（24SEP）但年份在别处出现（2026 年制单）时，L2=2026-09-24 该判通过。"""
    tr = {"lines": [{"i": 1, "text": "HAWB TAO7268550  2026 年制单  Flight TK0089/24SEP"}]}
    fid = verify_fidelity({"HAWB_NO": "TAO7268550", "CREATE_TIME": "2026-09-24"}, tr)
    assert not [f for f in fid["failed"] if f["field"] == "CREATE_TIME"], fid["failed"]
    bad = verify_fidelity({"HAWB_NO": "TAO7268550", "CREATE_TIME": "2026-09-25"}, tr)
    assert [f for f in bad["failed"] if f["field"] == "CREATE_TIME"], "对不上票面就必须报"


def test_wrong_create_time_is_flagged_on_real_ticket():
    """分单AE20260916 票面是 19-9月-26，L2 当时抄成 2026-09-26——真实漏网错单，必须持续被抓到。"""
    c = json.loads((FIX / "ae20260916.json").read_text(encoding="utf-8"))
    hits = [f for f in verify_fidelity(c["raw"], c["transcript"])["failed"]
            if f["field"] == "CREATE_TIME"]
    assert len(hits) == 1 and hits[0]["value"] == "2026-09-26", hits
    fixed = dict(c["raw"], CREATE_TIME="2026-09-19")
    assert not [f for f in verify_fidelity(fixed, c["transcript"])["failed"]
                if f["field"] == "CREATE_TIME"], "改成票面日期后不该再报"


def test_date_check_stays_silent_when_ticket_has_no_date():
    """签发区是印章/手写、L1 里一个带年份日期都没有时无从回查：不报也不算通过。"""
    tr = {"lines": [{"i": 1, "text": "MAWB 020-19025661   HAWB AE20260916   QD"}]}
    fid = verify_fidelity({"MAWB_NO": "020-19025661", "CREATE_TIME": "2026-09-26"}, tr)
    assert not [f for f in fid["failed"] if f["field"] == "CREATE_TIME"]

