# -*- coding: utf-8 -*-
"""把 fieldspec.py 的字段面写进 web/js/desk.js 的标记区。

    python deploy/gen_fields.py            # 重新生成
    python deploy/gen_fields.py --check    # 只检查是否已是最新（回归测试跑这个）

只动 `/* ==FIELDS== */ ... /* ==/FIELDS== */` 之间，其余代码不碰。
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fieldspec        # noqa: E402

OPEN, CLOSE = "/* ==FIELDS== */", "/* ==/FIELDS== */"
JS = ROOT / "web" / "js" / "desk.js"


def rendered():
    return OPEN + "\n" + fieldspec.render_js() + "\n" + CLOSE


def patch():
    src = io.open(JS, encoding="utf-8").read()
    if OPEN in src:
        a, b = src.index(OPEN), src.index(CLOSE) + len(CLOSE)
        new = src[:a] + rendered() + src[b:]
    else:                       # 第一次：把 legacy 手写块整体换掉（到 EXTS 之前为止）
        head = src.index("/* 字段清单")
        tail = src.index("const EXTS")
        block = src[head:tail].rstrip("\n;") + ";"
        new = src[:head] + rendered() + "\n" + src[tail:]
        assert "const FIELDS" in block, "没找到手写的 FIELDS 块，不敢乱改"
    return new, src


def main():
    new, src = patch()
    if "--check" in sys.argv:
        if new == src:
            print("desk.js 的字段块与 fieldspec.py 一致")
            return 0
        print("desk.js 的字段块已经落后于 fieldspec.py：跑 python deploy/gen_fields.py 重新生成",
              file=sys.stderr)
        return 1
    io.open(JS, "w", encoding="utf-8", newline="\n").write(new)
    print("已写入 %d 个字段" % len(fieldspec.desk_keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
