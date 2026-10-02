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


def test_fix_console_keeps_the_console_own_codepage():
    """fix_console 的本职是"别炸"，不是"改编码"。它原先把输出强行写成 UTF-8，而中文
    Windows 控制台是 cp936 —— 不死之后换来的是**一屏乱码**，双击 bat 的人根本看不懂。
    所以这里要求：按控制台的 GBK 能解开，而不是只有 UTF-8 才对。"""
    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    r = subprocess.run([sys.executable, "-c",
                        "import config; config.fix_console(); print('通过 306，全部绿灯')"],
                       capture_output=True, cwd=str(ROOT), env=env)
    assert r.returncode == 0, r.stderr.decode("gbk", errors="replace")[-300:]
    assert "全部绿灯" in r.stdout.decode("gbk"), \
        "输出不是控制台那一套编码（GBK 解不开）——那就是又回到一屏乱码"


def test_regression_runner_is_readable_in_a_gbk_console():
    """跑回归的那个子进程入口（tests/run_tests.py）也走同一规矩：GBK 窗口里看得懂，
    且 GBK 编不出的字符（那行末尾的 ✅）要替换掉继续跑，而不是打断整个套件。"""
    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "run_tests.py"), "gitignore"],
                       capture_output=True, cwd=str(ROOT), env=env)
    out = r.stdout.decode("gbk", errors="replace")
    assert r.returncode == 0, out[-400:] + r.stderr.decode("gbk", errors="replace")[-300:]
    assert "全部绿灯" in out, out[-400:]
