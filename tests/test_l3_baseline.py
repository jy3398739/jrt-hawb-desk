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


def test_l3_has_no_iata_warnings():
    """L3 出口必须全是三字码：码表缺项在这里被抓住，而不是等业务方发现。"""
    bad = []
    for p in sorted((FIX / "expected").glob("*.json")):
        w = validate_air(_read(p))
        if w:
            bad.append(f"{p.name}: {w}")
    assert not bad, "L3 存在未映射的地名（补 codes.CITY_IATA 后 --update-baseline）：\n    " + "\n    ".join(bad)
