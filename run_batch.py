# -*- coding: utf-8 -*-
"""一键批处理入口（形态①）。
用法:
  python run_batch.py                      # 处理输入目录下全部文件
  python run_batch.py D:\\分单目录          # 指定输入目录
  python run_batch.py --resume             # 跳过已处理且原件没变的（断点续跑）
  python run_batch.py --recheck            # 不调模型，给已有结果重算 L3 与质检红旗（字段契约变了也靠它补齐）
扫描件/PDF 与 XLSX/XLS 电子单统一走魔搭 VLM（电子单先由 LibreOffice 转 PDF，L1 直读文字层）；
自动产出第一遍+第二遍 JSON 与汇总。
"""
import json, sys, time, argparse
from pathlib import Path

import config
import codes
import store
import to_air
from fidelity import verify_fidelity
from jobs import build_qc, handle_file
from validator import validate_raw, validate_air


def recheck_all() -> int:
    """不调模型，用已存的 L1 转录 + L2 结果重算 L3 与质检红旗（改了校验/清洗口径后给老结果补记录）。
    L3 是 L2 的纯函数，重算不花一分钱；字段契约加了新列时老结果也在这一步补齐键位。"""
    files = [f for f in sorted(config.OUTPUT_RAW_DIR.glob("*.json")) if not f.name.startswith("all_")]
    if not files:
        raise SystemExit(f"{config.OUTPUT_RAW_DIR} 下没有已产出的结果，无需重检")
    old_qc = store.load_qc()
    flagged = 0
    for f in files:
        stored = json.loads(f.read_text(encoding="utf-8"))
        # 字段契约加列时老结果补齐键位：缺的键填空串，值仍在票面原文里（如税号曾粘在地址上）
        raw = {k: stored.get(k, "") for k in codes.TARGET_KEYS_OUT}
        air = to_air.to_air(raw)                     # L3 是 L2 的纯函数，重算即可，不碰模型
        store.save_result(f.stem, raw, air)
        tr_p = config.TRANSCRIPT_DIR / f.name
        transcript = json.loads(tr_p.read_text(encoding="utf-8")) if tr_p.exists() else None
        man_p = config.ARCHIVE_DIR / f.stem / "manifest.json"
        archive = json.loads(man_p.read_text(encoding="utf-8")) if man_p.exists() else None
        warns = validate_raw(raw) + ["L3 " + w for w in validate_air(air)]
        fid = None
        if transcript is None:
            warns.append("缺 L1 逐字转录，未做保真回查（要重跑该票）")
        else:
            fid = verify_fidelity(raw, transcript)
            fid["warns"] = [f"{x['field']}: {x['reason']}（值={x['value']!r}）" for x in fid["failed"]]
        r = {"stem": f.stem, "channel": "", "elapsed": 0.0, "raw": raw, "air": air,
             "transcript": transcript, "warns": warns, "fidelity": fid,
             "archive": archive, "qc": None, "error": ""}
        qc = r["qc"] = build_qc(Path((archive or {}).get("source_name") or f.stem), r)
        qc["channel"] = (old_qc.get(f.stem) or {}).get("channel", "")
        qc["elapsed"] = (old_qc.get(f.stem) or {}).get("elapsed", 0.0)
        store.save_qc(f.stem, qc)
        flagged += qc["needs_review"]
        print(f"{f.stem:46s} {'⚠ ' + str(len(qc['flags'])) + ' 条红旗' if qc['flags'] else '干净'}")
    print(f"\n重检 {len(files)} 票，需人工复核 {flagged} 票 -> {config.OUTPUT_QC_DIR}")
    return flagged


def main():
    config.fix_console()
    ap = argparse.ArgumentParser(description="HAWB 分单一键批处理")
    ap.add_argument("input", nargs="?", default=str(config.INPUT_DIR), help="输入文件或目录")
    ap.add_argument("--resume", action="store_true",
                    help="跳过已处理且原件 MD5 未变的文件（改过的票会重跑）")
    ap.add_argument("--recheck", action="store_true",
                    help="不调模型，用 output 里的 L2 重算 L3 与质检红旗后退出（改了清洗/校验口径或字段契约后用）")
    args = ap.parse_args()
    if args.recheck:
        config.fix_console()
        recheck_all()
        return

    inp = Path(args.input)
    if not inp.exists():
        raise SystemExit(f"输入不存在: {inp}")
    files = store.list_inputs(inp)
    if not files:
        raise SystemExit(f"目录中没有可处理文件（支持 {sorted(config.ALL_EXTS)}）: {inp}")

    config.OUTPUT_RAW_DIR.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_AIR_DIR.mkdir(parents=True, exist_ok=True)

    print(f"输入: {inp}  共 {len(files)} 个文件")
    print(f"输出: {config.OUTPUT_RAW_DIR}  /  {config.OUTPUT_AIR_DIR}\n")

    ok = fail = skipped = 0
    n_vlm = n_xls = 0
    all_warns = []
    all_fidelity = []
    need_review = []          # 任一红旗（规则/保真/失败）都要在 Excel 与库里跟着走
    fid_total = fid_pass = 0
    t0 = time.time()

    for i, f in enumerate(files, 1):
        if args.resume and store.already_done(f):
            skipped += 1
            print(f"[{i}/{len(files)}] {f.name} ... 跳过(已处理且原件未变)")
            continue
        print(f"[{i}/{len(files)}] {f.name} ...", end=" ", flush=True)
        r = handle_file(f)
        if r["qc"]["needs_review"]:
            need_review.append(f.name)
        if r["error"]:
            print(f"FAIL {r['elapsed']}s {r['error'][:120]}")
            fail += 1
            continue
        n_vlm += r["channel"] == "vlm"
        n_xls += r["channel"] == "excel"
        if r["warns"]:
            all_warns.append((f.name, r["warns"]))
        extra = ""
        if r["fidelity"]:
            fo = r["fidelity"]
            fid_total += fo["checked"]; fid_pass += fo["passed"]
            extra = f" 保真{fo['passed']}/{fo['checked']}"
            if fo["failed"]:
                all_fidelity.append((f.name, fo["failed"]))
        print(f"{r['channel']} {r['elapsed']}s{extra}"
              + (f"  ⚠ {len(r['warns'])} 校验" if r["warns"] else ""))
        ok += 1

    n_raw, n_air = store.rebuild_summary()
    print("\n" + "=" * 60)
    print(f"完成: 成功 {ok}（VLM {n_vlm} / 电子单 {n_xls}），跳过 {skipped}，失败 {fail}，"
          f"需人工复核 {len(need_review)}，总耗时 {time.time()-t0:.0f}s")
    print(f"质检记录: {config.OUTPUT_QC_DIR}  （Excel 的 _需复核 列与数据库 needs_review 列由此而来）")
    print(f"汇总: 第一遍 {n_raw} 条 -> {config.OUTPUT_RAW_DIR / 'all_hawbs.json'}")
    print(f"      第二遍 {n_air} 条 -> {config.OUTPUT_AIR_DIR / 'all_hawbs_air.json'}")
    print(f"      转录层 -> {config.TRANSCRIPT_DIR}    原件归档 -> {config.ARCHIVE_DIR}")
    if fid_total:
        print(f"保真回查: {fid_pass}/{fid_total} 字段在票面转录中找到原文出处"
              + ("  ✅" if fid_pass == fid_total else f"，{fid_total-fid_pass} 个字段需核票"))

    if all_fidelity:
        print("\n🔍 以下字段未通过保真回查（L2 值在 L1 转录中找不到，需对照原件确认）:")
        for name, items in all_fidelity:
            print(f"  {name}:")
            for x in items:
                print(f"    - {x['field']}={x['value']!r}")

    if all_warns:
        print("\n⚠ 规则校验提示（建议用 ask_vision.py 核票面）:")
        for name, warns in all_warns:
            print(f"  {name}:")
            for w in warns:
                print(f"    - {w}")

    # 保真/校验报告落盘，供人工复核留档
    try:
        import json, time as _t
        report = {
            "generated_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
            "input": str(inp),
            "files_total": len(files), "ok": ok, "failed": fail, "skipped": skipped,
            "channel": {"vlm": n_vlm, "excel": n_xls},
            "needs_review_files": need_review,
            "fidelity": {"checked": fid_total, "passed": fid_pass,
                         "needs_review": fid_total - fid_pass},
            "fidelity_failed": [
                {"file": name,
                 "items": [{"field": x["field"], "value": x["value"], "reason": x["reason"]}
                           for x in items]}
                for name, items in all_fidelity],
            "rule_warnings": [{"file": name, "warns": warns} for name, warns in all_warns],
            "layers": {"L0_archive": str(config.ARCHIVE_DIR),
                       "L1_transcript": str(config.TRANSCRIPT_DIR),
                       "L2_raw": str(config.OUTPUT_RAW_DIR),
                       "L3_air": str(config.OUTPUT_AIR_DIR)},
        }
        rp = config.TRANSCRIPT_DIR.parent / "fidelity_report.json"
        rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已保存: {rp}")
    except Exception as e:
        print(f"报告保存失败: {e}")

    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
