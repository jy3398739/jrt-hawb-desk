# -*- coding: utf-8 -*-
"""把识别结果导出为 Excel（形态④-a）。
用法:
  python export_excel.py                 # 同时导出第一遍 raw.xlsx 和第二遍 air.xlsx
  python export_excel.py air             # 只导第二遍（航空口径）
  python export_excel.py raw -o D:\\x.xlsx
"""
import json, sys, argparse
from pathlib import Path
import pandas as pd

import config
import hawb2json
import store

COLS = list(hawb2json.TARGET_KEYS_OUT)
QC_COLS = ["_需复核", "_复核提示"]
CN_HEADERS = {
    "MAWB_NO": "主单号", "HAWB_NO": "分单号", "SHIPPER_INFO": "发货人信息",
    "CONSIGNEE_INFO": "收货人信息", "ORIGIN_NAME": "起运港", "TO1": "航路1",
    "TO2": "航路2", "TO3": "航路3", "DEST_NAME": "目的港", "GOODS_INFO": "货物信息",
    "PIECES": "件数", "WEIGHT": "毛重", "SLAC": "SLAC", "CREATE_TIME": "签发日期",
    "SEND_STATUS": "状态",
}


def _qc_cells(qc: dict, stem: str):
    """联结质检记录。没有记录（本次改造前跑的旧结果）标成"无质检记录"，
    不能默认成"干净"——那样红旗会静悄悄地消失。"""
    q = qc.get(stem)
    if not q:
        return "无质检记录", ""
    return ("是" if q.get("needs_review") else "否"), "；".join(q.get("flags") or [])


def export(kind: str, out_path: Path):
    d = config.OUTPUT_RAW_DIR if kind == "raw" else config.OUTPUT_AIR_DIR
    qc = store.load_qc()
    rows, no_qc = [], 0
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("all_"):
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        rec["_来源文件"] = f.stem
        rec["_需复核"], rec["_复核提示"] = _qc_cells(qc, f.stem)
        no_qc += rec["_需复核"] == "无质检记录"
        rows.append(rec)
    if not rows:
        print(f"{d} 下没有可导出的 JSON")
        return
    df = pd.DataFrame(rows)
    # 列顺序：来源 + 39 字段 + 质检两列（复核的人按 _需复核 筛选）
    cols = ["_来源文件"] + [c for c in COLS if c in df.columns] + QC_COLS
    df = df.reindex(columns=[c for c in cols if c in df.columns])
    df.to_excel(out_path, index=False)
    flagged = int((df["_需复核"] == "是").sum())
    print(f"{kind}: {len(df)} 行 -> {out_path}")
    print(f"   需人工复核 {flagged} 行" + (f"，另有 {no_qc} 行无质检记录（重跑一遍即有）" if no_qc else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", nargs="?", default="all", choices=["raw", "air", "all"])
    ap.add_argument("-o", "--output", help="单文件模式的输出路径")
    args = ap.parse_args()

    out_dir = config.BASE_DIR / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.kind in ("raw", "all"):
        export("raw", Path(args.output) if args.kind == "raw" and args.output else out_dir / "hawb_raw.xlsx")
    if args.kind in ("air", "all"):
        export("air", Path(args.output) if args.kind == "air" and args.output else out_dir / "hawb_air.xlsx")


if __name__ == "__main__":
    main()
