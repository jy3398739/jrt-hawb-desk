# -*- coding: utf-8 -*-
"""质检红旗交付链回归：QC 记录落盘 → Excel 两列 / 数据库两列 + 逐行容错 + 数值归一。
这一段全是纯本地文件与假引擎，不碰数据库也不调 API。
"""
import contextlib
import json
import re
import tempfile
from pathlib import Path

import pandas as pd

import config
import db_writer
import export_excel
import hawb2json
import run_batch
import store

FLAGS = ["MAWB_NO 缺失（主单号常与分单号分两格印，需核票确认票面是否有）",
         "L3 未转成 IATA 三字码: TO3X NOMAP（码表缺项，需补 CITY_IATA）"]


@contextlib.contextmanager
def _sandbox():
    """把输出相关目录全挪进临时目录，测试不碰真 output/。"""
    old = (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
           config.TRANSCRIPT_DIR, config.ARCHIVE_DIR)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR = tmp / "raw", tmp / "air"
        config.OUTPUT_QC_DIR = tmp / "qc"
        config.TRANSCRIPT_DIR = tmp / "transcript"
        config.ARCHIVE_DIR = tmp / "archive"
        for p in (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
                  config.TRANSCRIPT_DIR):
            p.mkdir()
        try:
            yield tmp
        finally:
            (config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
             config.TRANSCRIPT_DIR, config.ARCHIVE_DIR) = old


class _Conn:
    def __init__(self, eng):
        self.eng, self.pending = eng, []

    def execute(self, stmt, params):
        if params.get("source_file") == self.eng.fail_on:
            raise RuntimeError("1054 Unknown column PIECES")
        self.pending.append((str(stmt), params))


class _Engine:
    """假事务：begin 正常退出才提交，抛异常就丢弃——用来验证一行失败不牵连整批。"""

    def __init__(self, fail_on=None):
        self.fail_on, self.committed = fail_on, []

    @contextlib.contextmanager
    def begin(self):
        c = _Conn(self)
        yield c
        self.committed.extend(c.pending)


def test_qc_roundtrip_and_missing_dir():
    with _sandbox():
        assert store.load_qc() == {}, "qc 目录还不存在时应返回空表而不是崩"
        store.save_qc("TAO1", {"needs_review": True, "flags": FLAGS})
        got = store.load_qc()
        assert list(got) == ["TAO1"] and got["TAO1"]["flags"] == FLAGS
        (config.OUTPUT_QC_DIR / "坏记录.json").write_text("{坏", encoding="utf-8")
        assert list(store.load_qc()) == ["TAO1"], "单条坏质检记录不能拖垮整表联结"


def test_excel_carries_review_columns():
    with _sandbox() as tmp:
        for stem in ("坏票", "好票", "旧票"):
            (config.OUTPUT_RAW_DIR / f"{stem}.json").write_text(
                json.dumps({"MAWB_NO": "020-19025661", "HAWB_NO": stem, "PIECES": "1"},
                           ensure_ascii=False), encoding="utf-8")
        store.save_qc("坏票", {"needs_review": True, "flags": FLAGS})
        store.save_qc("好票", {"needs_review": False, "flags": []})
        out = tmp / "x.xlsx"
        export_excel.export("raw", out)
        df = pd.read_excel(out)
        assert df.columns[0] == "_来源文件"
        assert list(df.columns[-2:]) == ["_需复核", "_复核提示"], "复核列要排在最后，好筛选"
        row = df.set_index("_来源文件")
        assert row.loc["坏票", "_需复核"] == "是" and "IATA" in row.loc["坏票", "_复核提示"]
        assert row.loc["好票", "_需复核"] == "否" and pd.isna(row.loc["好票", "_复核提示"])
        assert row.loc["旧票", "_需复核"] == "无质检记录", "旧结果不能默认成干净，否则红旗静默消失"


def test_numeric_columns_coerce_or_null():
    assert db_writer._num("12", True) == 12
    assert db_writer._num("12 KGM", True) == 12
    assert db_writer._num("1,200.5", False) == 1200.5
    assert db_writer._num("123.456", False) == 123.456
    assert db_writer._num("", True) is None and db_writer._num(None, False) is None
    assert db_writer._num("N/A", False) is None
    assert db_writer._num("约 12.5", True) is None, "件数不该有小数，给 NULL 让人去查而不是悄悄取整"


def test_db_row_failure_is_isolated_and_flags_join():
    with _sandbox():
        for stem in ("坏票", "好票"):
            (config.OUTPUT_RAW_DIR / f"{stem}.json").write_text(json.dumps(
                {"MAWB_NO": "020-19025661",
                 "PIECES": "12 KGM" if stem == "坏票" else "8",
                 "WEIGHT": "" if stem == "坏票" else "123.5"}, ensure_ascii=False), encoding="utf-8")
        store.save_qc("坏票", {"needs_review": True, "flags": FLAGS})
        store.save_qc("好票", {"needs_review": False, "flags": []})

        eng = _Engine(fail_on="坏票")
        ok, failed = db_writer.write_kind(eng, "raw")
        assert ok == 1 and [n for n, _ in failed] == ["坏票"], "一行报错把整批弄挂了"
        assert failed[0][1].startswith("RuntimeError: 1054")
        inserts = [(s, p) for s, p in eng.committed if "INSERT" in s]
        assert not [p for _, p in inserts if p["source_file"] == "坏票"], "失败的行不该留下半成品"
        good = [p for _, p in inserts if p["source_file"] == "好票"][0]
        assert good["PIECES"] == 8 and good["WEIGHT"] == 123.5
        assert good["needs_review"] == 0 and good["review_flags"] == ""
        assert "needs_review" in inserts[0][0] and "review_flags" in inserts[0][0]

        p = db_writer._params({"source_file": "坏票"}, store.load_qc())
        assert p["needs_review"] == 1 and "IATA" in p["review_flags"]
        q = db_writer._params({"source_file": "没记录"}, {})
        assert q["needs_review"] == 0 and q["review_flags"] is None, "无记录要区别于干净"


def test_schema_declares_review_columns():
    sql = (Path(db_writer.__file__).parent / "schema.sql").read_text(encoding="utf-8")
    ddl = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert re.search(r"^\s+needs_review\s+TINYINT", ddl, re.M), "建表语句里要真的有两列，写在注释里不算"
    assert re.search(r"^\s+review_flags\s+TEXT", ddl, re.M)
    assert "ALTER TABLE hawb_raw ADD COLUMN needs_review" in sql, "老库缺升级语句的话写库会直接报错"


def _write_ticket(stem: str, raw: dict, transcript: dict | None):
    (config.OUTPUT_RAW_DIR / f"{stem}.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    if transcript is not None:
        (config.TRANSCRIPT_DIR / f"{stem}.json").write_text(
            json.dumps(transcript, ensure_ascii=False), encoding="utf-8")


def test_recheck_backfills_flags_without_calling_model():
    """改了校验口径后，老结果要能离线补上红旗——重跑一遍就是又一次额度。"""
    with _sandbox():
        _write_ticket("TAO7268550",
                      {"MAWB_NO": "020-19025661", "HAWB_NO": "TAO7268550",
                       "CREATE_TIME": "2026-09-26"},
                      {"lines": [{"i": 1, "text": "HAWB TAO7268550"},
                                 {"i": 2, "text": "Executed on 20-Sep-26"}],
                       "full_text": "HAWB TAO7268550 Executed on 20-Sep-26"})
        store.save_qc("TAO7268550", {"channel": "excel", "elapsed": 12.5,
                                     "needs_review": False, "flags": []})
        assert run_batch.recheck_all() == 1
        qc = store.load_qc()["TAO7268550"]
        assert qc["needs_review"] and any("签发日期" in f for f in qc["flags"]), qc["flags"]
        # 重检不跑模型，通道与耗时只能沿用旧记录：丢了的话 Excel/库里两列会变空白
        assert qc["channel"] == "excel" and qc["elapsed"] == 12.5, qc


def test_recheck_recomputes_l3_and_backfills_new_columns():
    """字段契约加列后跑 --recheck：老结果补齐新键，L3 由 L2 纯函数重算（把粘在地址上的税号摘出来）。
    不这么办的话老票的税号永远留在地址串里，而重跑一遍又是一次额度。"""
    with _sandbox():
        old = {"MAWB_NO": "999-30825351", "HAWB_NO": "999-30825351",
               "SHIPPER_INFO_COMP_NAME": "BEIJING ORIENTAL SCIENCE & TECHNOLOGY",
               "SHIPPER_INFO_COMP_ADDRESS": "BEI-SI-HUAN ROAD, HAIDIAN DISTRICT, USCI:9111010880211232X4",
               "SHIPPER_INFO_COUNTRY": "CN", "SHIPPER_INFO_CITY": "BEIJING"}   # 37 键时代，根本没有 TAX_ID
        _write_ticket("999_30825351", old,
                      {"lines": [{"i": 1, "text": old["SHIPPER_INFO_COMP_ADDRESS"]}], "full_text": ""})
        run_batch.recheck_all()
        raw = json.loads((config.OUTPUT_RAW_DIR / "999_30825351.json").read_text(encoding="utf-8"))
        air = json.loads((config.OUTPUT_AIR_DIR / "999_30825351.json").read_text(encoding="utf-8"))
        assert len(raw) == len(hawb2json.TARGET_KEYS_OUT) and raw["SHIPPER_INFO_TAX_ID"] == "", \
            "老结果要补齐新列；L2 是照抄口径，号仍在地址串里"
        assert air["SHIPPER_INFO_TAX_ID"] == "9111010880211232X4", air
        assert "USCI" not in air["SHIPPER_INFO_COMP_ADDRESS"], air["SHIPPER_INFO_COMP_ADDRESS"]


def test_recheck_flags_ticket_without_transcript():
    with _sandbox():
        _write_ticket("PEK1642839", {"MAWB_NO": "020-19025661", "HAWB_NO": "PEK1642839"}, None)
        run_batch.recheck_all()
        qc = store.load_qc()["PEK1642839"]
        assert any("缺 L1" in f for f in qc["flags"]), "没做过保真回查要说明，不能当成已通过"

