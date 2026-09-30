# -*- coding: utf-8 -*-
""".env.example 必须跟得上 config.py：运维机重装/换机时只有这一份文件告诉人"还能调什么"。
配置项加了没人写进例子，等于没有——没人会去翻源码猜键名。
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_every_env_key_config_reads_is_documented():
    used = set(re.findall(r'getenv\("([A-Z0-9_]+)"', (HERE / "config.py").read_text(encoding="utf-8")))
    assert len(used) >= 30, f"扫到的配置项太少（{len(used)}），八成是正则没匹配上，而不是真的只有这些"
    doc = (HERE / ".env.example").read_text(encoding="utf-8")
    named = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", doc, re.M))
    missing = sorted(used - named)
    assert not missing, f"这些配置项 config 会读、例子却没写：{missing}"
