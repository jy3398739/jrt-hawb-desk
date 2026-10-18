# -*- coding: utf-8 -*-
"""L1 转录复用的口径（2026-10-18 效率审查 E1）。

L1（票面逐字转录）是**单独收费**的一步：扫描件一张票转录一次、提取一次，各算一次钱。
从前每次重跑这张票都要再买一次转录——失败重试、重复上传、重启后的重放全都重付。

口径（2026-10-18 定）：**只对自动路径复用**（批处理 / 监控 / 站点 worker），
**人工重解析照旧重跑**——人点重跑多半是因为结果不对，这时把上次那份转录再喂回去，
等于把同一个错误再算一遍，还多花一次提取的钱。

复用必须认指纹：原件 md5 + 当时的 L1 模型 + 转录提示词，三者全对上才算同一份票面。
没有指纹的老转录一律不认：宁可重跑一次，也不把一份来路不明的转录当票面真值。
"""
import contextlib
import json
import tempfile
from pathlib import Path

import config
import jobs
import pipeline
import store

TR = {"lines": [{"i": 1, "text": "SHIPPER ALPHA", "bbox": [0, 0, 9, 9]}],
      "full_text": "SHIPPER ALPHA"}
TR2 = {"lines": [{"i": 1, "text": "SHIPPER BETA", "bbox": [0, 0, 9, 9]}],
       "full_text": "SHIPPER BETA"}

_DIRS = ("ARCHIVE_DIR", "TRANSCRIPT_DIR", "OUTPUT_RAW_DIR", "OUTPUT_AIR_DIR",
         "OUTPUT_QC_DIR", "PREVIEW_DIR")


@contextlib.contextmanager
def _dirs(tmp: Path):
    """落盘目录全指到临时目录：这一组用例一个字节都不许写进真实 output/。"""
    old = {n: getattr(config, n) for n in _DIRS}
    for n in _DIRS:
        setattr(config, n, tmp / n.lower())
    try:
        yield
    finally:
        for n, v in old.items():
            setattr(config, n, v)


def _ticket(tmp: Path, stem: str, body: bytes = b"%PDF-1.4 ticket") -> Path:
    p = tmp / f"{stem}.pdf"
    p.write_bytes(body)
    return p


def _stub_pf(got, *, raise_after_l1=False, produced=None):
    """替掉 jobs.process_file：记下收到的 reuse，并照管道的样子把 L1 交给 bag。"""
    def pf(path, do_transcript=True, reuse=None, bag=None):
        got["reuse"] = reuse
        tr = reuse if reuse is not None else (produced or TR)
        if bag is not None:
            bag["transcript"] = tr      # 真管道里 L1 一跑完就交出来，后面 L2 死了也留得住
        if raise_after_l1:
            raise RuntimeError("L2 提取失败")
        return {"raw": {}, "air": {}, "channel": "vlm", "transcript": tr, "src": None,
                "model": "m", "model_choice": "c", "timings": {}}
    return pf


def _run(tmp: Path, stem: str, *, fresh=False, raise_after_l1=False):
    got = {}
    old = jobs.process_file
    jobs.process_file = _stub_pf(got, raise_after_l1=raise_after_l1)
    try:
        return jobs.handle_file(_ticket(tmp, stem), save=True, fresh=fresh), got
    finally:
        jobs.process_file = old


def test_an_auto_rerun_reuses_the_stored_l1():
    """批处理 / 监控重跑一张票：指纹对得上就把上次那份转录交下去，不再调一次模型。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _dirs(tmp):
            p = _ticket(tmp, "T1")
            store.save_transcript("T1", TR, prov=pipeline.l1_prov(store.md5_of(p)))
            r, got = _run(tmp, "T1")
            assert got["reuse"] == TR, f"没把上次那份转录交给管道：{got['reuse']!r}"
            assert r["transcript"] == TR, "结果里那份转录不是复用的那一份"


def test_a_manual_redo_runs_l1_again():
    """人工重解析：原件没变、指纹也对得上，但人要重跑——这时不许复用，
    因为他多半就是因为上次结果不对才点的重跑。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _dirs(tmp):
            p = _ticket(tmp, "T1")
            store.save_transcript("T1", TR, prov=pipeline.l1_prov(store.md5_of(p)))
            r, got = _run(tmp, "T1", fresh=True)
            assert got["reuse"] is None, "人工重解析还是把上次的转录喂回去了"
            assert r["transcript"] == TR, "重跑也要照常产出转录"


def test_a_changed_original_is_never_reused():
    """同一张票换了原件（内容变了、md5 变了）：指纹对不上，必须重新转录。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _dirs(tmp):
            old_p = _ticket(tmp, "T1", b"%PDF-1.4 first")
            store.save_transcript("T1", TR, prov=pipeline.l1_prov(store.md5_of(old_p)))
            _ticket(tmp, "T1", b"%PDF-1.4 second")
            r, got = _run(tmp, "T1")
            assert got["reuse"] is None, "原件内容变了还在复用旧转录"
            assert r["transcript"] == TR


def test_reuse_needs_the_same_original_model_and_prompt():
    """指纹三样：原件 md5、当时的 L1 模型、转录提示词。少对一样都不算同一份票面。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _dirs(tmp):
            prov = pipeline.l1_prov("MD5-A")
            store.save_transcript("T1", TR, prov=prov)
            assert store.reusable_transcript("T1", prov) == TR
            assert store.reusable_transcript("T1", pipeline.l1_prov("MD5-B")) is None, \
                "换了原件还认这份转录"
            other = dict(prov)
            other["model"] = "另一个模型"
            assert store.reusable_transcript("T1", other) is None, "换了 L1 模型还认这份转录"
            other = dict(prov)
            other["prompt"] = "提示词改过了"
            assert store.reusable_transcript("T1", other) is None, "转录提示词改了还认这份转录"


def test_a_transcript_without_a_fingerprint_is_never_reused():
    """老转录（这次改动之前落的，没有指纹）：一律重跑。
    指纹的作用就是把「这份转录是哪个模型、按哪版提示词、对哪份原件读的」钉住，
    没有它就无从判断能不能当同一份票面真值。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _dirs(tmp):
            store.save_transcript("T1", TR)
            data = json.loads((config.TRANSCRIPT_DIR / "T1.json").read_text(encoding="utf-8"))
            assert "_l1" not in data, "没给指纹就不该凭空写一个上去"
            assert store.reusable_transcript("T1", pipeline.l1_prov("MD5-A")) is None, \
                "没有指纹的老转录被当成可复用了"


def test_the_reused_body_carries_no_fingerprint():
    """复用交回去的是转录本体：指纹只用来判能不能用，不该混进 lines/full_text 那份数据里。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _dirs(tmp):
            prov = pipeline.l1_prov("MD5-A")
            store.save_transcript("T1", TR, prov=prov)
            got = store.reusable_transcript("T1", prov)
            assert "_l1" not in got, got


def test_a_failed_ticket_keeps_the_l1_it_already_paid_for():
    """L2 挂了，L1 的钱已经花出去了：那份转录照样落盘。
    不落的话，下次自动重跑这张票还得再买一次同样的转录——钱白花两遍。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _dirs(tmp):
            p = _ticket(tmp, "T1")
            r, _ = _run(tmp, "T1", raise_after_l1=True)
            assert r["error"].startswith("RuntimeError"), r["error"]
            saved = config.TRANSCRIPT_DIR / "T1.json"
            assert saved.is_file(), "L1 已经付过钱了，L2 失败也得把它落下来"
            data = json.loads(saved.read_text(encoding="utf-8"))
            assert data["full_text"] == TR["full_text"]
            assert data["_l1"]["md5"] == store.md5_of(p), data["_l1"]

            # 落下来就有意义：下一次自动重跑直接复用，不再付 L1
            r2, got = _run(tmp, "T1")
            assert got["reuse"] and got["reuse"]["full_text"] == TR["full_text"], got
            assert r2["error"] == ""
