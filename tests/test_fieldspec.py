# -*- coding: utf-8 -*-
"""字段面只有一个真源（设计文档 §4.3）：fieldspec.py。

前端 desk.js 里那份 `FIELDS` 是生成的；导出/入库顺序仍是 `codes.TARGET_KEYS_OUT`（业务系统直读，
不能因为换实现就改列序）。两边必须覆盖同一批列——加一列漏一处，就是"接口收得进、界面看不见"。
"""
import ast
import io
import re
import subprocess
import sys
from pathlib import Path

import codes
import fieldspec

ROOT = Path(__file__).resolve().parent.parent


def test_fieldspec_is_self_consistent():
    keys = fieldspec.desk_keys()
    assert len(keys) == len(set(keys)), "同一个键出现两次：审核台会渲染两行，改哪一行说不清"
    assert set(keys) == set(codes.TARGET_KEYS_OUT), \
        "字段面与导出列不一致：\n  只在 fieldspec：%s\n  只在导出：%s" % (
            sorted(set(keys) - set(codes.TARGET_KEYS_OUT)), sorted(set(codes.TARGET_KEYS_OUT) - set(keys)))
    for k, lab, g in fieldspec.DESK_FIELDS:
        assert lab.strip(), f"{k} 没有中文名：审核台上会出现看不懂的裸键名"
        assert g in fieldspec.GROUP_LABELS, f"{k} 的分组 {g} 没定义"
    for k in fieldspec.NUM:
        assert k in keys, f"{k} 被标成数字列但不在字段面里（拼错了？）"
    for k in fieldspec.LONG:
        assert k in keys, f"{k} 被标成长文本列但不在字段面里"


def test_excel_headers_only_use_known_columns():
    """导出表头是另一套用词（面向收件人），允许与审核台不同；但键名写错必须当场发现——
    IT 那次改列名（NOTIFYE_INFO_COUNTRY → NOTIFY_INFO_COUNTRY）差点就是这种错。"""
    import export_excel
    unknown = sorted(set(export_excel.CN_HEADERS) - set(codes.TARGET_KEYS_OUT))
    assert not unknown, f"CN_HEADERS 里有字段面不认识的列：{unknown}"


def test_desk_js_field_block_is_generated_not_handwritten():
    """重新生成一次，字节必须相同：不同就说明有人手改了生成区，下次重生成会静默吃掉他的改动。"""
    js = io.open(ROOT / "web" / "js" / "desk.js", encoding="utf-8").read()
    assert "/* ==FIELDS== */" in js and "/* ==/FIELDS== */" in js, "生成标记区不见了"
    r = subprocess.run([sys.executable, str(ROOT / "deploy" / "gen_fields.py"), "--check"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, (r.stdout + r.stderr).strip()
    # 数字列与长文本列的标记要在生成的块里，desk.js 其余逻辑全靠它们
    assert "const KEYS = FIELDS.map" in js and "const NUM = {" in js


def test_generated_block_still_parses_as_javascript():
    if not subprocess.run(["node", "--version"], capture_output=True).returncode == 0:
        return
    r = subprocess.run(["node", "--check", str(ROOT / "web" / "js" / "desk.js")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, "desk.js 语法坏了：" + (r.stderr or r.stdout)


def test_nobody_hardcodes_the_column_count():
    """"39 字段"这类写在注释和页面里的数字，加一列那天就变成假话（税号那批就把 37 改成过 39）。
    要表达就说"全字段"，要数字请从列面现算。"""
    bad = []
    for p in list(ROOT.glob("*.py")) + list((ROOT / "web").rglob("*")):
        if not p.is_file() or p.suffix not in (".py", ".html", ".js", ".css", ".sql") or "tests" in p.parts:
            continue
        txt = p.read_text(encoding="utf-8")
        for m in re.finditer(r"(3[0-9]|4[0-9])\s*(个)?字段", txt):
            window = txt[max(0, m.start() - 24):m.end() + 24]
            if "时代" in window or "当时" in window:
                continue        # 允许"37 字段时代没有归处"这种历史说法
            bad.append(f"{p.relative_to(ROOT)}:{m.group(0)}")
    assert not bad, f"这些地方的列数是写死的，加一列就是假话：{bad}"

    """谁再写一个"整份 40 列"的列表字面量，就是第二真源：改列时漏改它，
    会出现"提示词让模型填、界面却没有这格"或反过来。
    （prompt 正文里按段落讲字段、fidelity 只挑 5 个长文本列，都不算抄表。）"""
    keys = set(codes.TARGET_KEYS_OUT)
    hits = []
    for p in sorted(ROOT.glob("*.py")):
        if p.name in ("fieldspec.py", "codes.py"):
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.List):
                vals = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                # 覆盖率不到九成就当作"有意的子集"（fidelity 只回查文本列、不含数值/日期/合并串，
                # 那是口径不是抄表；把它的子集扩成全列面反而会改坏保真判定）
                if len(set(vals)) >= int(len(keys) * 0.9) and set(vals) <= keys:
                    hits.append(f"{p.name}:{node.lineno}（{len(set(vals))}/{len(keys)} 列）")
    assert not hits, ("这些列表字面量抄了整份列面，请改指 codes.TARGET_KEYS_OUT / fieldspec："
                      + str(hits))
