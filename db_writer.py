# -*- coding: utf-8 -*-
"""把识别结果写入数据库（形态④-b）。连接串在 .env 的 DB_URL 配置。
用法:
  python db_writer.py                 # 将 output 下 raw/air 全部 upsert 入库
  python db_writer.py --table air     # 只写第二遍
表结构见 schema.sql（首次需先在数据库执行建表；老库补质检列的 ALTER 也在该文件末尾）。
幂等：按 source_file（=文件名去扩展）主键先删后插，可重复跑。
逐行独立提交：一张坏票只弄坏它自己那一行，不会把整批回滚。
"""
import json, re, argparse
from pathlib import Path

from sqlalchemy import create_engine, text

import config
import hawb2json
import store

COLS = list(hawb2json.TARGET_KEYS_OUT)
INT_COLS = ("PIECES", "SLAC")
FLOAT_COLS = ("WEIGHT",)
QC_COLS = ("needs_review", "review_flags")


def _num(v, integer: bool):
    """数值列归一：空/解析不出 → None（NULL），"12 KGM"、"1,200.5" → 数字。
    直接塞字符串会在 MySQL 严格模式抛错，一行坏数据弄挂整批。"""
    s = str(v if v is not None else "").strip()
    if not s:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", "").replace("，", ""))
    if not m:
        return None
    val = float(m.group())
    if integer:
        return int(val) if val.is_integer() else None
    return val


def _params(rec: dict, qc: dict):
    stem = rec["source_file"]
    p = {c: rec.get(c, "") for c in ["source_file"] + COLS}
    for c in INT_COLS:
        p[c] = _num(p.get(c), True)
    for c in FLOAT_COLS:
        p[c] = _num(p.get(c), False)
    q = qc.get(stem)
    p["needs_review"] = 1 if (q and q.get("needs_review")) else 0
    p["review_flags"] = "；".join(q.get("flags") or []) if q else None
    return p


def write_kind(engine, kind: str):
    table = f"hawb_{kind}"
    rows = _load(kind)
    if not rows:
        print(f"{kind}: 无数据")
        return 0, []
    cols = ["source_file"] + COLS + list(QC_COLS)
    sql_ins = (f"INSERT INTO {table} ({','.join(cols)}) "
               f"VALUES ({','.join(':' + c for c in cols)})")
    sql_del = f"DELETE FROM {table} WHERE source_file = :source_file"
    qc = store.load_qc()
    ok, failed = 0, []
    for rec in rows:
        params = _params(rec, qc)
        try:
            with engine.begin() as conn:          # 每行一个事务：坏票不牵连别人
                conn.execute(text(sql_del), {"source_file": rec["source_file"]})
                conn.execute(text(sql_ins), params)
            ok += 1
        except Exception as e:
            failed.append((rec["source_file"], f"{type(e).__name__}: {e}"))
    print(f"{table}: 成功 {ok} 条，失败 {len(failed)} 条")
    for name, err in failed:
        print(f"   失败 {name}: {err[:180]}")
    return ok, failed


def _load(kind: str):
    d = config.OUTPUT_RAW_DIR if kind == "raw" else config.OUTPUT_AIR_DIR
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("all_"):
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        rec["source_file"] = f.stem
        out.append(rec)
    return out


def main():
    config.fix_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", choices=["raw", "air", "all"], default="all")
    args = ap.parse_args()
    if not config.DB_URL:
        raise SystemExit("未配置 DB_URL：请在 .env 中设置数据库连接串，参考 .env.example")
    engine = create_engine(config.DB_URL, pool_pre_ping=True)
    bad = 0
    if args.table in ("raw", "all"):
        bad += len(write_kind(engine, "raw")[1])
    if args.table in ("air", "all"):
        bad += len(write_kind(engine, "air")[1])
    if bad:
        raise SystemExit(f"有 {bad} 行未入库，见上面的失败清单（修表结构或该票数据后重跑即可）")
    print("完成。")


if __name__ == "__main__":
    main()
