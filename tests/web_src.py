# -*- coding: utf-8 -*-
"""前端契约断言读的是"这一套前端"：页面骨架 + 两份样式 + 两份脚本（logic 纯逻辑、desk 接线）。
拆文件不该让契约测试变瞎——挂载前缀、转义、队列轮询这些约束照样要能扫到。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "web"     # 真实前端目录：有些用例会临时把
                                                          # server.WEB_DIR 指到空目录去测缺失场景
PARTS = ("index.html", "css/tokens.css", "css/desk.css", "js/logic.js", "js/desk.js")


def desk() -> str:
    root = ROOT
    missing = [p for p in PARTS if not (root / p).exists()]
    assert not missing, f"前端文件少了：{missing}（拆文件后契约测试扫的是整套，缺一块就是瞎的）"
    return "\n".join((root / p).read_text(encoding="utf-8") for p in PARTS)


def css() -> str:
    """两份样式当成一份看：布局断言关心的是"规则在不在"，不是它在哪个文件。"""
    return "\n".join([part("css/tokens.css"), part("css/desk.css")])


def part(rel: str) -> str:
    """rel 可写成 "js/desk.js" 或 "web/js/desk.js"（调用处两种手感都有，别为此各写一份）。"""
    p = ROOT / rel.removeprefix("web/")
    return p.read_text(encoding="utf-8") if p.exists() else ""
