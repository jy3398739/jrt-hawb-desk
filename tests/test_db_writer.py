# -*- coding: utf-8 -*-
"""db_writer 的连接纪律：删票路径从前每张 create_engine 一次——每日清理 100 张就是
100 次建连（2026-10-17 效率审查 G2）。池化 engine 全模块共用一个，pool_pre_ping 防空闲断连。
DB_URL 没配时一切照旧返回 0——本机与 VM 只出 Excel 是常态，那不是错误。"""
import config
import db_writer


def test_delete_ticket_reuses_one_engine():
    old = config.DB_URL
    config.DB_URL = "sqlite://"
    calls = []
    real = db_writer.create_engine

    def counting(url, *a, **k):
        calls.append(1)
        return real(url, *a, **k)

    db_writer.create_engine = counting
    db_writer._ENG = None
    try:
        for _ in range(4):
            try:
                db_writer.delete_ticket("NOPE")
            except Exception:
                pass                      # sqlite 里没这两张表：删 0 行也行，数的是建连
    finally:
        db_writer.create_engine = real
        config.DB_URL = old
        db_writer._ENG = None
    assert len(calls) == 1, f"四次删票建了 {len(calls)} 次 engine：该共用一个连接池"


def test_delete_ticket_without_db_url_is_a_polite_zero():
    old = config.DB_URL
    config.DB_URL = ""
    try:
        assert db_writer.delete_ticket("X") == 0, "没配 DB_URL 要安静返回 0，不是报错"
    finally:
        config.DB_URL = old
