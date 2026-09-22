# -*- coding: utf-8 -*-
"""监控循环回归：坏票重试到上限就收手（每次都要烧额度），已处理过的直接跳过。
不改动真实 output/，不产生任何 API 调用（handle_file 打桩）。
"""
import sys
import tempfile
from pathlib import Path

import config
import store
import watch_folder


def _drive(tmp: Path, handle, done, tries=6, extra=()):
    """跑监控主循环：sleep 打桩成迭代计数器，够数就按 Ctrl+C 退出。"""
    old = (sys.argv, config.INPUT_DIR, config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR,
           config.TRANSCRIPT_DIR, watch_folder.handle_file, watch_folder.time.sleep,
           store.already_done)
    sys.argv = ["watch_folder.py", str(tmp), "--interval", "0", *extra]
    config.INPUT_DIR = tmp
    config.OUTPUT_RAW_DIR = tmp / "raw"
    config.OUTPUT_AIR_DIR = tmp / "air"
    config.TRANSCRIPT_DIR = tmp / "transcript"
    watch_folder.handle_file = handle
    store.already_done = done
    n = {"i": 0}

    def fake_sleep(_):
        n["i"] += 1
        if n["i"] >= tries:
            raise KeyboardInterrupt

    watch_folder.time.sleep = fake_sleep
    try:
        watch_folder.main()
    finally:
        (sys.argv, config.INPUT_DIR, config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR,
         config.TRANSCRIPT_DIR, watch_folder.handle_file, watch_folder.time.sleep,
         store.already_done) = old
    return n["i"]


def test_bad_file_stops_after_retry_limit():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "坏票.pdf").write_bytes(b"%PDF-1.4 broken")
        calls = []

        def handle(path, save=True):
            calls.append(Path(path).name)
            return {"stem": "x", "channel": "", "elapsed": 0.1, "raw": None, "air": None,
                    "transcript": None, "warns": [], "fidelity": None, "archive": None,
                    "error": "RuntimeError: 模型无响应"}

        _drive(tmp, handle, lambda p: False, tries=12)
        assert len(calls) == 3, f"重试次数失控：{len(calls)} 次调用 = {len(calls)} 次额度"
        assert calls == ["坏票.pdf"] * 3
        logs = list((tmp / "_失败").glob("*.log"))
        assert len(logs) == 1, "失败要留痕，否则运维只能猜"
        assert logs[0].read_text(encoding="utf-8").count("模型无响应") == 3


def test_done_files_are_not_reprocessed():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "已处理.pdf").write_bytes(b"%PDF-1.4 ok")
        calls = []

        def handle(path, save=True):
            calls.append(Path(path).name)
            return {"stem": "ok", "channel": "vlm", "elapsed": 0.1, "raw": {}, "air": {},
                    "transcript": None, "warns": [], "fidelity": None, "archive": None,
                    "error": ""}

        _drive(tmp, handle, lambda p: True)
        assert calls == [], "重启监控把整目录重跑一遍 = 白烧额度"


def test_skip_existing_flag():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "存量.pdf").write_bytes(b"%PDF-1.4 old")
        calls = []
        _drive(tmp, lambda p, save=True: calls.append(p) or {"error": ""},
               lambda p: False, tries=4, extra=("--skip-existing",))
        assert calls == [], "--skip-existing 不该碰存量文件"
