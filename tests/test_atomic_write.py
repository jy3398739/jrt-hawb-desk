# -*- coding: utf-8 -*-
"""落盘只该有一把原子写：tmp + os.replace + 保住原权限。
从前四处各写一份（store/auth/config/master_pipeline），另两处（结果 JSON、归档 manifest、
汇总 all_hawbs）干脆直接 write_text——掉电或磁盘满就留下一份截断的台账，
下次读回来要么炸要么当空表，制单员的提交记录就这么没了。
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import atomic

HERE = Path(__file__).resolve().parent.parent


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="hawb_atomic_"))


def test_write_json_lands_and_leaves_no_temp():
    d = _tmp()
    try:
        p = d / "ledger.json"
        atomic.write_json(p, {"a": 1, "名": "值"})
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "名": "值"}
        assert [f.name for f in d.iterdir()] == ["ledger.json"], "临时文件没被换掉或没清走"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_failed_render_leaves_the_old_file_intact():
    """写坏不能波及线上那份：内容组不出来（不可序列化）时目标文件必须还是老样子。
    直接 write_text 的写法会先把老文件截断在这里。"""
    d = _tmp()
    try:
        p = d / "x.json"
        atomic.write_json(p, {"v": 1})
        try:
            atomic.write_json(p, {"bad": object()})
            raise AssertionError("不可序列化的内容居然写成功了")
        except TypeError:
            pass
        assert json.loads(p.read_text(encoding="utf-8")) == {"v": 1}, "老文件被动过：这不是原子写"
        assert [f.name for f in d.iterdir()] == ["x.json"], "失败的写法留下了一半的临时文件"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_existing_mode_is_kept_and_sensitive_new_file_can_be_tightened():
    if os.name == "nt":
        return                       # Windows 的 chmod 只管只读位（这条由 VM 回归覆盖）
    d = _tmp()
    try:
        p = d / "u.json"
        atomic.write_json(p, {"v": 1}, mode=0o600)
        assert p.stat().st_mode & 0o7777 == 0o600, "新建的账号表/令牌文件要按调用方要求的权限落盘"
        p.chmod(0o644)
        atomic.write_json(p, {"v": 2})
        assert p.stat().st_mode & 0o7777 == 0o644, "沿用原权限：否则每写一次就被放宽或收紧一档"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_write_text_writes_verbatim():
    """.env 的换行风格归调用方决定：这里不能再被 Windows 的 write_text 翻成 CRLF。"""
    d = _tmp()
    try:
        p = d / ".env"
        atomic.write_text(p, "A=1\r\nB=2\r\n")
        assert p.read_bytes() == b"A=1\r\nB=2\r\n", p.read_bytes()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_only_one_atomic_write_implementation_left():
    """四份抄本会变成四份各改各的 bug——合并之后要有个门看着它别又长回去。"""
    sites = [n for n in ("store.py", "auth.py", "config.py", "master_pipeline.py")
             if "os.replace(" in (HERE / n).read_text(encoding="utf-8")]
    assert not sites, f"这些模块里还自己写了一份原子替换：{sites}（该走 atomic.write_json / write_text）"
    # 台账、汇总这类 JSON 落盘也不能再裸写：截断一次就少一段提交记录
    for name in ("store.py", "master_pipeline.py"):
        n = len(re.findall(r"\.write_text\(", (HERE / name).read_text(encoding="utf-8")))
        assert n == 0, f"{name} 还有 {n} 处裸 write_text：掉电就是半份文件"
