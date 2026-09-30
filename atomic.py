# -*- coding: utf-8 -*-
"""落盘的唯一入口：先写同目录临时文件，再 os.replace 原子换名。

两个细节都是踩过坑的：
  ① os.replace 会把临时文件的权限一起带过去，所以要先按原文件权限 chmod 临时文件——
     .env 与 users.json 都曾因为按 umask 新建的临时文件被放宽成 664，而那两个文件里放着令牌和口令哈希。
  ② 文本按原样写出（newline=""）：换行风格由调用方决定。Windows 上 write_text 会把一份 LF 的
     .env 整篇翻成 CRLF， diff 起来看不出改了哪一行。
"""
import json
import os
from pathlib import Path


def write_text(path, text: str, mode: int = None) -> None:
    """写 text 到 path，原子替换。mode 只在文件是新建时用来收紧权限（已存在一律沿用原权限）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        keep = (path.stat().st_mode & 0o7777) if path.exists() else mode
        if keep is not None:
            try:
                os.chmod(tmp, keep)
            except OSError:
                pass                      # Windows 的 chmod 只管只读位，失败不该拦住落盘
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()                  # 写坏别把半份临时文件留在归档目录里


def write_json(path, data, mode: int = None) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2), mode=mode)
