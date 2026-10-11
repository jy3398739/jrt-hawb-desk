# -*- coding: utf-8 -*-
"""自动清理的入口：算名册 → 逐张删 → 逐张报成败 → 把这一步写在盘上。

为什么不挂在 web 进程里：hawb-desk 是 `Restart=always`，进程内起循环等于每次重启就把计时器
重置一遍，而失败只躺在日志里，谁都不知道上次到底跑没跑。独立入口 + 一份落盘的状态
（`output/purge_last_run.json`），面板顶上那句「上次自动清理」就是读它——定时器最怕静默失败。

**不带 --auto 就什么都不删**（只把名单与每张占多少字节打出来）。给管理员先看一眼这一步
要抹掉哪些票，是整个自动化里唯一的刹车，所以它也是默认值。
"""
import sys

import config
import purge


def _mb(n) -> str:
    n = int(n or 0)
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


def _line(out, text=""):
    out.write(text + "\n")


def report(out, items, days) -> int:
    """名单打印。返回总占用字节，供调用方报总数。"""
    _line(out, "安静期 %d 天：最后一次被碰之后躺满 %d 天的票，共 %d 张" % (days, days, len(items)))
    if not items:
        _line(out, "  现在没有到期可删的票（不是没有票，是没有躺够 %d 天的票）。" % days)
        return 0
    for it in items:
        _line(out, "  %-4s %-24s 主单 %-14s 分单 %-12s 安静 %-4s天 %-9s %s" % (
            it["kind"], (it["stem"] or it["mawb"])[:24], it["mawb"] or "—", it["hawb"] or "—",
            it["idle_days"], _mb(it["bytes"]),
            "已作废" if it["voided"] else "未作废"))
    total = sum(int(it["bytes"] or 0) for it in items)
    _line(out, "  合计 %d 张，本机占用 %s" % (len(items), _mb(total)))
    return total


def main(argv=None, out=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    auto = "--auto" in argv
    if "-h" in argv or "--help" in argv:
        _line(out, __doc__.strip())
        _line(out, "用法：python -X utf8 run_purge.py [--dry-run|--auto]")
        return 0
    days = purge.retention()
    items = purge.candidates()          # 带 bytes：这份是要给人看名单的，不是徽标那条轻路径
    report(out, items, days)
    if not auto:
        _line(out, "（没带 --auto：只列名单，一个字节都没删，也没写「上次自动清理」）")
        return 0
    if not items:
        purge.write_run_state([], days)   # 跑了、0 张，也要留时间：否则分不清"没跑"和"没到期"
        _line(out, "没有要删的票，已记下这次跑过。")
        return 0
    res = purge.purge([{"kind": it["kind"], "key": it["key"]} for it in items],
                      by="auto", auto=True)
    bad = [r for r in res if not r.get("ok")]
    for it, r in zip(items, res):
        name = it["stem"] or it["mawb"]
        if r.get("ok"):
            _line(out, "  删掉 %s（%d 个路径）" % (name, len(r.get("removed") or [])))
        else:
            _line(out, "  没删掉 %s：%s" % (name, r.get("error") or ""))
    purge.write_run_state(res, days)
    _line(out, "自动清理完成：删掉 %d 张，%d 张没删掉。凭据在 %s" % (
        len(res) - len(bad), len(bad), config.PURGED_LEDGER.name))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
