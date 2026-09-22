# -*- coding: utf-8 -*-
"""文件夹监控自动处理（形态②）。轮询方式，跨平台、无需驱动。
用法:
  python watch_folder.py                      # 监控 config.INPUT_DIR，启动时先处理存量
  python watch_folder.py D:\\待处理 --interval 10
  python watch_folder.py --skip-existing      # 只监控启动后新放入的文件
新文件复制进目录、大小连续两次扫描不变（默认10s）后自动识别，结果写入 output/。
Ctrl+C 退出。
"""
import time, argparse
from pathlib import Path

import config
import store
from jobs import handle_file


def _log_failure(name: str, err: str):
    d = config.TRANSCRIPT_DIR.parent / "_失败"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{name}.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {err}" + "\n")


def main():
    config.fix_console()
    ap = argparse.ArgumentParser(description="监控文件夹自动处理 HAWB")
    ap.add_argument("input", nargs="?", default=str(config.INPUT_DIR))
    ap.add_argument("--interval", type=int, default=10, help="轮询间隔秒，默认10")
    ap.add_argument("--skip-existing", action="store_true", help="启动时不处理已有文件")
    args = ap.parse_args()

    watch_dir = Path(args.input)
    watch_dir.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_RAW_DIR.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_AIR_DIR.mkdir(parents=True, exist_ok=True)

    # name -> (size, mtime, 稳定计数)
    pending = {}
    processed = set()
    failed = {}          # name -> 已失败次数，超限不再重试（坏票反复调模型纯烧额度）
    max_tries = 3

    def scan():
        return {f.name: (f.stat().st_size, f.stat().st_mtime, f)
                for f in store.list_inputs(watch_dir)}

    if args.skip_existing:
        for name in scan():
            processed.add(name)
        print(f"已忽略目录内现有 {len(processed)} 个文件，只处理新文件。")

    print(f"监控目录: {watch_dir}")
    print(f"轮询间隔: {args.interval}s   输出: {config.OUTPUT_RAW_DIR.parent}"
      f"   （已有结果且原件 MD5 未变的文件会自动跳过）")
    print("把分单文件放进该目录即可自动处理，Ctrl+C 退出。\n")

    try:
        while True:
            current = scan()
            # 发现/变化的文件重置稳定计数
            for name, (size, mtime, fpath) in current.items():
                if name in processed:
                    continue
                if store.already_done(fpath):      # 重启监控不再把整目录重跑一遍
                    processed.add(name)
                    continue
                prev = pending.get(name)
                if prev is None or (prev[0], prev[1]) != (size, mtime):
                    pending[name] = (size, mtime, 1, fpath)
                else:
                    size2, mtime2, cnt, fp = pending[name]
                    pending[name] = (size2, mtime2, cnt + 1, fp)
                    if cnt + 1 >= 2:  # 连续两轮大小/时间不变 → 传输完成
                        print(f"发现新文件 {name} ...", end=" ", flush=True)
                        r = handle_file(fp)
                        if r["error"]:
                            failed[name] = failed.get(name, 0) + 1
                            print(f"FAIL({failed[name]}/{max_tries}) {r['elapsed']}s {r['error'][:120]}")
                            # 失败先记日志；重试到上限就放弃，等修好文件后重启监控会再来
                            _log_failure(name, r["error"])
                            pending.pop(name, None)
                            if failed[name] >= max_tries:
                                processed.add(name)
                        else:
                            w = f"  ⚠ {len(r['warns'])} 条提示" if r["warns"] else ""
                            print(f"{r['channel']} {r['elapsed']}s 完成{w}")
                            processed.add(name)
                            store.rebuild_summary()
            # 删除已不存在的待处理项
            for name in list(pending):
                if name not in current and name in processed:
                    pending.pop(name, None)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止监控。")


if __name__ == "__main__":
    main()
