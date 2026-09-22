# -*- coding: utf-8 -*-
"""控制台编码回归：票面文件名里的 U+00A0 曾把整批处理打断在 print 上。
子进程用 PYTHONIOENCODING=gbk 复现"重定向到文件/非 chcp 终端"的场景。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "CLA26090022\u00a0HAWB.pdf"     # 真实样本名，NBSP 不是空格


def _run(code: str):
    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(ROOT), env=env)


def test_nbsp_needs_fix_console():
    bad = _run(f"print({NAME!r})")
    assert bad.returncode != 0 and "UnicodeEncodeError" in (bad.stderr or ""), \
        f"GBK 控制台下居然没炸，测试前提变了：rc={bad.returncode}"
    good = _run(f"import config; config.fix_console(); print({NAME!r})")
    assert good.returncode == 0, f"fix_console 之后仍失败：{good.stderr[-300:]}"
    assert "HAWB.pdf" in good.stdout


def test_unencodable_chars_are_replaced_not_fatal():
    """errors="replace" 那一半：Windows 文件名里可能出现 UTF-8 也编不出的孤立代理字符，
    打印它必须替换成 ? 继续跑，而不是让日志把任务搞死。"""
    r = _run('import config; config.fix_console(); print("a\\ud800b")')
    assert r.returncode == 0, f"坏字符未被替换，print 仍然抛错：{r.stderr[-300:]}"
    assert "b" in r.stdout


def test_cli_entries_call_fix_console():
    for f in ("run_batch.py", "watch_folder.py", "ask_vision.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert "config.fix_console()" in src, f"{f} 的入口没调 config.fix_console()，打印票面名会打断任务"
