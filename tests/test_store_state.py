# -*- coding: utf-8 -*-
"""监控/断点续跑用的"这单处理过没有"判定回归。
判错方向的代价不对称：假阳=票被漏掉，假阴=白烧一次额度，所以只有
"L2 输出在 + L0 归档记录的 MD5 与当前原件一致"两个条件同时成立才算处理过。
"""
import json
import tempfile
from pathlib import Path

import config
import store


def _setup(tmp: Path):
    """把归档/输出目录指到临时目录，避免碰到真 output。"""
    old = (config.ARCHIVE_DIR, config.OUTPUT_RAW_DIR)
    config.ARCHIVE_DIR = tmp / "archive"
    config.OUTPUT_RAW_DIR = tmp / "raw"
    config.ARCHIVE_DIR.mkdir(parents=True), config.OUTPUT_RAW_DIR.mkdir(parents=True)
    return old


def test_already_done_states():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        old = _setup(tmp)
        try:
            src = tmp / "TAO1234567.pdf"
            src.write_bytes(b"%PDF-1.4 original-hawb")

            assert store.already_done(src) is False, '还没处理过就报"已处理"，会漏单'

            (config.OUTPUT_RAW_DIR / f"{src.stem}.json").write_text('{"HAWB_NO":"TAO1234567"}',
                                                                    encoding="utf-8")
            assert store.already_done(src) is False, "L2 有了但无归档凭证(MD5 无从核对) → 宁可重跑"

            man = store.archive_original(src)
            assert store.already_done(src) is True, "已归档且 MD5 一致仍重跑 = 重启监控白烧额度"

            (config.OUTPUT_RAW_DIR / f"{src.stem}.json").unlink()
            assert store.already_done(src) is False, "归档了但 L2 结果不在（上次中途崩了），必须重跑"
            (config.OUTPUT_RAW_DIR / f"{src.stem}.json").write_text('{"HAWB_NO":"TAO1234567"}',
                                                                    encoding="utf-8")

            src.write_bytes(b"%PDF-1.4 edited-hawb")
            assert store.already_done(src) is False, "票面内容变了必须重跑"

            src.write_bytes(b"%PDF-1.4 original-hawb")
            man["md5"] = "0" * 32
            (config.ARCHIVE_DIR / src.stem / "manifest.json").write_text(
                json.dumps(man), encoding="utf-8")
            assert store.already_done(src) is False, "MD5 不一致却跳过 = 拿旧结果冒充新票"

            (config.ARCHIVE_DIR / src.stem / "manifest.json").write_text("{坏文件", encoding="utf-8")
            assert store.already_done(src) is False, "manifest 读不动时要重跑，不能崩"
        finally:
            config.ARCHIVE_DIR, config.OUTPUT_RAW_DIR = old
