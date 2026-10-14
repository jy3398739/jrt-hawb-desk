# -*- coding: utf-8 -*-
"""L2→L3 全量回归：31 份历史票面重算，必须与冻结期望值逐字段一致。
L3 是纯确定性代码（to_air.py），所以这条测试零 API、零模型噪声，是改码表时的唯一护栏。
期望值由 fixtures/l3/expected 冻结；有意改行为时用 run_tests.py --update-baseline 复核后更新。
"""
import json
from pathlib import Path

import to_air
from validator import validate_air

FIX = Path(__file__).resolve().parent / "fixtures" / "l3"


def _read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_l3_matches_baseline():
    inputs = sorted((FIX / "input").glob("*.json"))
    assert len(inputs) >= 31, f"L3 回归夹具只剩 {len(inputs)} 份，基线被误删了"
    bad = []
    for p in inputs:
        ep = FIX / "expected" / p.name
        assert ep.exists(), f"夹具 {p.name} 缺 expected，先跑 --update-baseline"
        new = to_air.to_air(_read(p))
        old = _read(ep)
        for k in new:
            if str(old.get(k, "") or "") != str(new.get(k, "") or ""):
                bad.append(f"{p.name}:{k} 期望{str(old.get(k,''))[:60]!r} 实际{str(new.get(k,''))[:60]!r}")
    assert not bad, f"{len(bad)} 处 L3 漂移：\n    " + "\n    ".join(bad[:12])


def test_l3_output_is_exactly_the_current_column_contract():
    """L3 出口必须**正好**是现行列面：少一列＝静默丢字段，多一列＝往契约外写东西。

    上一条按 `for k in new` 遍历，所以 new 里少了哪一列它根本看不见（2026-10-13 效率审查
    确认的那处护栏缺口：改列面时 L3 少输出一列，回归照样全绿）。这一条补的就是那个洞，
    而且判据取自 fieldspec 这一份单一真源，不抄列数。"""
    import fieldspec
    want = set(fieldspec.desk_keys())
    bad = []
    for p in sorted((FIX / "input").glob("*.json")):
        got = set(to_air.to_air(_read(p)))
        if got != want:
            bad.append(f"{p.name}：缺 {sorted(want - got)} 多 {sorted(got - want)}")
    assert not bad, f"{len(bad)} 份夹具的 L3 出口与现行列面不一致：\n    " + "\n    ".join(bad[:8])


def test_l3_keeps_the_party_whole_strings_as_transcribed():
    """整串这一列只有一个来源：L2 那份照抄串（2026-10-14 业主定案「整串提取不要有优化」）。

    这条不看 expected、直接拿 input 比：L3 一旦恢复从子字段重拼，这里先红，而不是等 40 份
    expected 一起漂移了才在 diff 里发现。expected 也要一起核——基线里还留着旧的重拼串，
    说明 --update-baseline 没跑，发出去的就是两份口径。"""
    bad = []
    for p in sorted((FIX / "input").glob("*.json")):
        d = _read(p)
        got = to_air.to_air(d)
        ep = FIX / "expected" / p.name
        old = _read(ep) if ep.exists() else {}
        for k in ("SHIPPER_INFO", "CONSIGNEE_INFO"):
            if str(got.get(k, "") or "") != str(d.get(k, "") or ""):
                bad.append(f"{p.name}:{k} 整串被 L3 改了")
            if ep.exists() and str(old.get(k, "") or "") != str(d.get(k, "") or ""):
                bad.append(f"{p.name}:{k} 冻结的期望值还是重拼那一份")
    assert not bad, "整串口径没对上：\n    " + "\n    ".join(bad[:10])


def test_l3_has_no_iata_warnings():
    """L3 出口必须全是三字码：码表缺项在这里被抓住，而不是等业务方发现。"""
    bad = []
    for p in sorted((FIX / "expected").glob("*.json")):
        w = [x for x in validate_air(_read(p)) if "三字码" in x]
        if w:
            bad.append(f"{p.name}: {w}")
    assert not bad, "L3 存在未映射的地名（补 codes.CITY_IATA 后 --update-baseline）：\n    " + "\n    ".join(bad)


# 识别号格（EORI，今天税号也住这里）的形态红旗：补码表消不掉，只能人工核票改那一格，
# 所以它不归上一条"零告警"的闸门管。冻一张已知清单在这里：多出一格就得是有意为之，
# 而不是某次改动把号弄坏了没人看见。
KNOWN_BAD_ID_CELLS = {
    "94fe2613ad9be1ca6d2a3958421348e.json": ["SHIPPER_INFO_EORI"],   # 票面 NOTICE/CN/SHP/T/USCI… 整段被塞进格子
}


def test_l3_id_cell_warnings_are_the_known_list():
    found = {}
    for p in sorted((FIX / "expected").glob("*.json")):
        cols = [x.split()[0] for x in validate_air(_read(p)) if "EORI" in x]
        if cols:
            found[p.name] = sorted(cols)
    assert found == KNOWN_BAD_ID_CELLS, (
        f"识别号格红旗与已知清单不一致：新增={ {k: v for k, v in found.items() if KNOWN_BAD_ID_CELLS.get(k) != v} }")
