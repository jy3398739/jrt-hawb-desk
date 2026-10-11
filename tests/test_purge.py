# -*- coding: utf-8 -*-
"""自动清理（彻底删除）：判据怎么算、一张票要动哪些路径、执行的顺序、自动那一路的入口。

这一段是全系统里唯一会**不可逆抹掉数据**的地方，所以用例的重点不是"删得掉"，而是那几条
"不许删"：没安静满 N 天的票、盘上时间读不出来的票、名字里带穿越的票、主单下面还挂着别的分单的票、
刚发给公司一次的主单。执行顺序也钉住——先写凭据再删文件最后才动台账，而且**能算出安静时间的那三处
排在最后删**；反过来做，半途崩一次就留下一张"文件还在盘上、但名册再也算不出它安静了多久"的孤儿数据，
那种数据比没删更难对付。
"""
import contextlib
import io
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import company_api
import config
import db_writer
import purge
import server
import store
import web_src

NOW = "2026-10-11T12:00:00"
OLD = (datetime.fromisoformat(NOW) - timedelta(days=40)).isoformat(timespec="seconds")


@contextlib.contextmanager
def _world():
    """一张票散在十几个地方，所以这个临时世界要把每一个根都指到盘外，
    跑完连目录一起掉——绝不能碰真 output/ 与提交台账。"""
    old = (config.INPUT_DIR, config.ARCHIVE_DIR, config.TRANSCRIPT_DIR, config.OUTPUT_RAW_DIR,
           config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR, config.PREVIEW_DIR, config.STAGED_DIR,
           config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR, config.VOID_LEDGER,
           config.MAWB_SOURCE_DIR, config.MASTER_DIR, config.MASTER_LEDGER, config.PURGED_LEDGER,
           config.PURGE_RUN_STATE, config.PURGE_AFTER_DAYS, auth.USERS_FILE, config.ENV_FILE)
    with tempfile.TemporaryDirectory() as d:
        t = Path(d)
        config.INPUT_DIR = t / "input"
        config.ARCHIVE_DIR = t / "output" / "archive"
        config.TRANSCRIPT_DIR = t / "output" / "transcript"
        config.OUTPUT_RAW_DIR = t / "output" / "raw"
        config.OUTPUT_AIR_DIR = t / "output" / "air"
        config.OUTPUT_QC_DIR = t / "output" / "qc"
        config.PREVIEW_DIR = t / "output" / "preview"
        config.STAGED_DIR = t / "output" / "staged"
        config.SUBMIT_LEDGER = t / "output" / "submitted.json"
        config.SUBMIT_GUARD_DIR = t / "output" / "submit_guard"
        config.VOID_LEDGER = t / "output" / "voided.json"
        config.MAWB_SOURCE_DIR = t / "output" / "mawb_source"
        config.MASTER_DIR = t / "output" / "master"
        config.MASTER_LEDGER = t / "output" / "master_submitted.json"
        config.PURGED_LEDGER = t / "output" / "purged.jsonl"
        config.PURGE_RUN_STATE = t / "output" / "purge_last_run.json"
        config.PURGE_AFTER_DAYS = 30
        auth.USERS_FILE = t / "users.json"
        # 安静期写的是 .env，而回归跑完会核对 .env 有没有被改过（config.ENV_FILE 不吃
        # 环境变量，只能在测试里把属性指到盘外）——不然这套用例会真去改运维机上的配置。
        config.ENV_FILE = t / ".env"
        for p in (config.INPUT_DIR, config.ARCHIVE_DIR, config.TRANSCRIPT_DIR,
                  config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
                  config.PREVIEW_DIR, config.STAGED_DIR, config.SUBMIT_GUARD_DIR,
                  config.MAWB_SOURCE_DIR, config.MASTER_DIR):
            p.mkdir(parents=True)
        try:
            yield t
        finally:
            (config.INPUT_DIR, config.ARCHIVE_DIR, config.TRANSCRIPT_DIR, config.OUTPUT_RAW_DIR,
             config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR, config.PREVIEW_DIR, config.STAGED_DIR,
             config.SUBMIT_LEDGER, config.SUBMIT_GUARD_DIR, config.VOID_LEDGER,
             config.MAWB_SOURCE_DIR, config.MASTER_DIR, config.MASTER_LEDGER, config.PURGED_LEDGER,
             config.PURGE_RUN_STATE, config.PURGE_AFTER_DAYS, auth.USERS_FILE,
             config.ENV_FILE) = old


def _backdate(paths, at):
    """把刚写出来的文件与归档目录的 mtime 一起倒到 at。

    不倒不行：夹具里的每一份都是**此刻**写的，而判据看的是"最后一次被碰"——
    不倒就每张票都像是刚被碰过，满期的候选永远是空，整套用例就只在验"什么都不删"。"""
    ts = datetime.fromisoformat(str(at)).timestamp()
    for p in paths:
        try:
            os.utime(p, (ts, ts))
        except OSError:
            pass


def _ticket(t, stem, mawb="999-95764373", hawb="JRT28645", sent=True, master=True,
            touched=OLD, void=False):
    """在盘上摆一张"经手过"的票：原件、L1/L2/L3/红旗、预览、暂存、台账、幂等锁。

    touched 就是它"最后一次被碰"的时刻（写进 qc.processed_at，并把盘上时间一起倒过去）；
    void 才打作废戳——v1.15.0 起自动清理的判据是安静满 N 天，作废只是行上的一个标记。"""
    made = [config.INPUT_DIR / (stem + ".pdf")]
    (config.INPUT_DIR / (stem + ".pdf")).write_bytes(b"RAW-UPLOAD")
    a = config.ARCHIVE_DIR / stem
    a.mkdir(parents=True, exist_ok=True)
    (a / (stem + ".pdf")).write_bytes(b"ORIGINAL")
    (a / "manifest.json").write_text('{"md5":"x"}', encoding="utf-8")
    made.append(a)
    for p in (config.TRANSCRIPT_DIR / (stem + ".json"),):
        p.write_text('{"flags":[]}', encoding="utf-8")
        made.append(p)
    # qc 里要带 source_name：input 那一格是按它去找的，缺了就等于这条路径没被测到。
    # 再带两格票面内容：凭据那条用例是「扫有没有混进票面内容」，夹具里没有内容的话
    # 它就是在扫一份空字符串——把 _row 改成整份抄走也不会红。
    # processed_at 是「安静满 N 天」三个来源里的第一个。
    (config.OUTPUT_QC_DIR / (stem + ".json")).write_text(json.dumps(
        {"flags": [], "source_name": stem + ".pdf", "processed_at": str(touched),
         "GOODS_INFO": "STEEL PARTS", "SHIPPER_INFO_NAME": "SHIPPER CO LTD"},
        ensure_ascii=False), encoding="utf-8")
    made.append(config.OUTPUT_QC_DIR / (stem + ".json"))
    # raw/air 里要真的带上 stem：all_hawbs.json 是从这些文件汇总出来的，
    # 记录里不含 stem 的话「聚合副本删没删干净」那条用例就是空的，永远绿。
    (config.OUTPUT_RAW_DIR / (stem + ".json")).write_text(
        '{"source_file":"%s","HAWB_NO":"%s"}' % (stem, hawb), encoding="utf-8")
    made.append(config.OUTPUT_RAW_DIR / (stem + ".json"))
    (config.OUTPUT_AIR_DIR / (stem + ".json")).write_text(
        '{"source_file":"%s","MAWB_NO":"%s","HAWB_NO":"%s"}' % (stem, mawb, hawb),
        encoding="utf-8")
    made.append(config.OUTPUT_AIR_DIR / (stem + ".json"))
    (config.PREVIEW_DIR / (stem + ".pdf")).write_bytes(b"PREVIEW")
    made.append(config.PREVIEW_DIR / (stem + ".pdf"))
    # 暂存记录里最后一次事件的时间是第二个来源（staged 文件本身也要倒）
    (config.STAGED_DIR / (stem + ".json")).write_text(json.dumps(
        {"stem": stem, "air_final": {}, "staged_at": str(touched),
         "events": [{"by": "宛平", "role": "reviewer", "at": str(touched), "edits": {}}]},
        ensure_ascii=False), encoding="utf-8")
    made.append(config.STAGED_DIR / (stem + ".json"))
    if sent:
        store.mark_submitted(stem, mawb, hawb, "宛平", {"mode": "mock"})
        # 真提交过才会留下幂等锁：不摆上盘，「锁也要删」这一条就是空的
        lock = company_api.guard_file(store.number_key(mawb, hawb))
        lock.write_text('{"key":"k"}', encoding="utf-8")
        made.append(lock)
    if master:
        k = store.norm_no(mawb)
        mc = config.MASTER_DIR / (k + ".json")
        mc.write_text('{"mawb":"%s"}' % k, encoding="utf-8")
        md = config.MAWB_SOURCE_DIR / k
        md.mkdir(parents=True, exist_ok=True)
        (md / "m.pdf").write_bytes(b"MASTER-ORIGINAL")
        made += [mc, md]      # 主单那两处的 mtime 就是主单的「最后被碰」
    if void:
        store.void_row("hawb", stem, "admin")
        _age_void("hawb", stem, touched)
    _backdate(made, touched)
    return stem


def _touch_staged(stem, at):
    """昨天有人改了它并暂存：暂存记录里最后一条事件的时间就变了——三处来源里取最晚的那一个。"""
    p = config.STAGED_DIR / (stem + ".json")
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["events"].append({"by": "admin", "role": "admin", "at": str(at), "edits": {"WEIGHT": {}}})
    rec["staged_at"] = str(at)
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    ts = datetime.fromisoformat(str(at)).timestamp()
    os.utime(p, (ts, ts))


def _touch_archive(stem, at):
    """归档目录被人事后动过（手工放回原件、目录被复制）：目录 mtime 也算一处来源。"""
    a = config.ARCHIVE_DIR / stem
    ts = datetime.fromisoformat(str(at)).timestamp()
    os.utime(a, (ts, ts))


def _paths(p):
    """这张票的落点全集。删的顺序由 remove() 分段管，这里只管"有哪些"。"""
    return {str(x) for x in list(p["files"]) + list(p["dirs"]) + list(p.get("sources") or [])}


# ── 一、唯一判据：最后一次被碰之后安静满 N 天（v1.15.0 起不再看作废）──────────────

def test_a_quiet_ticket_is_a_candidate_even_if_nobody_voided_it():
    """判据从"谁点过作废"换成"在盘上安静躺满 N 天"。制单员不点作废，数据就永远留在服务器上
    越积越多——那正是这次要修的：管理员要能把够日子的票清掉，不管中间有没有人做过判断。"""
    with _world() as t:
        stem = _ticket(t, "S1", touched=OLD)
        assert store.voided_map() == {}, "夹具不该自己动作废戳：作废现在只是个标记"
        got = purge.candidates(days=30, now=NOW)
        assert [c["key"] for c in got] == ["hawb|" + stem], "安静 40 天的票没进候选"
        assert purge.plan("hawb", stem, 30, now=NOW) is not None


def test_a_ticket_touched_yesterday_is_not_a_candidate():
    """安静期就是让人回头查旧票的窗口：昨天刚碰过的，今天不能因为"反正满 30 天的日子早就过了"
    而被抹掉——上一版只看 qc.processed_at 就会这样多删。"""
    with _world() as t:
        stem = _ticket(t, "S1", touched=NOW)
        assert purge.candidates(days=30, now=NOW) == []
        assert purge.plan("hawb", stem, 30, now=NOW) is None, \
            "刚被碰过的票能算出删除计划，等于没有安静期"


def test_the_touch_time_is_the_latest_of_the_three_places():
    """三处取最晚：qc.processed_at（解析）、staged 里最后一条事件（人改过并暂存）、
    archive/<stem>/ 的目录 mtime（事后被动过）。只看其中一处都会多删。"""
    with _world() as t:
        stem = _ticket(t, "S1", touched=OLD)
        assert purge.candidates(days=30, now=NOW), "先确认这张本身是候选，否则下面的断言是空的"
        _touch_staged(stem, NOW)
        assert purge.candidates(days=30, now=NOW) == [], "上周有人复核并暂存过它，就不算安静"
        a = config.ARCHIVE_DIR / stem
        _touch_archive(stem, OLD)                       # 先把目录收回去，单独验第三处
        ts = datetime.fromisoformat(NOW).timestamp()
        os.utime(a / "manifest.json", (ts, ts))
        assert purge.candidates(days=30, now=NOW) == [], "归档目录里的文件刚被换过"


def test_a_master_just_sent_to_the_company_is_not_quiet():
    """主单的安静时间要把「发给公司那一次」算进去：提交只动 master_submitted.json，
    缓存与原件目录的 mtime 一个都不会变——只看那两处就会把一张昨天刚发给公司的主单当"躺够了"删掉。"""
    with _world() as t:
        m = "176-62400004"
        _ticket(t, "S1", mawb=m, hawb="JRT7", sent=False, master=True, touched=OLD)
        k = store.norm_no(m)
        assert purge.plan("mawb", k, 30, now=NOW) is not None, \
            "先确认这张主单本来就该在名册上，否则下面的断言是空的"
        store.mark_master_submitted(k, "宛平", {"mode": "mock"})
        assert purge.plan("mawb", k, 30, now=NOW) is None, "刚发给公司的主单被当成安静够了"
        assert "还没安静满" in purge.refuse("mawb", k, 30, now=NOW), \
            "拒的是安静期这一道闸门；名下的分单那条闸门别替它顶罪"


def test_an_unreadable_touch_is_never_aged():
    """时刻读不出来（有人手工改过 json、或将来换了时间格式），一律当"还没满期"。
    这条判据只能往"少删"一侧错：猜成"早就满了"就等于给一张没人核对过的票开了门。"""
    with _world() as t:
        stem = _ticket(t, "S1", touched=OLD)
        p = config.OUTPUT_QC_DIR / (stem + ".json")
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["processed_at"] = "不是个时刻"
        p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        st = config.STAGED_DIR / (stem + ".json")
        st.unlink()                                     # 没暂存过：这处来源也空
        ts = datetime.fromisoformat(NOW).timestamp()
        os.utime(config.ARCHIVE_DIR / stem, (ts, ts))   # 目录此刻被动过 → 读得出且不旧
        assert purge.candidates(days=30, now=NOW) == []
        assert purge.plan("hawb", stem, 30, now=NOW) is None


def test_the_candidate_row_says_how_long_it_has_been_quiet_and_whether_it_was_voided():
    """面板要答"这张为什么在这儿"：安静了多少天（判据）、最后一次被碰的时刻，
    外加一个作废标记（信息，不是准入）。"""
    with _world() as t:
        stem = _ticket(t, "S1", touched=OLD, void=True)
        got = purge.candidates(days=30, now=NOW)
        assert [c["key"] for c in got] == ["hawb|" + stem]
        c = got[0]
        assert c["quiet_at"] == OLD and c["idle_days"] == 40, \
            "满期的理由得写在行上：管理员看的是「为什么这张能删」"
        assert c["voided"] is True and c["void_by"] == "admin" and c["void_at"] == OLD
        assert c["in_ledger"] is True, "在不在提交台账要如实报——自动抹掉的也可能是有证据的那批"
        assert c["mawb"] == "999-95764373" and c["hawb"] == "JRT28645"
        assert c["bytes"] > 0, "面板那一列本机占用得有数，不是摆个空位"


def test_a_never_voided_ticket_still_shows_up_as_unvoided():
    """没作废的票进名单时，那一格要写"没作废"而不是把空串糊成一格——
    管理员要能一眼看出这张是"没人判断过、纯粹躺够了"，还是"谁收起来过"。"""
    with _world() as t:
        stem = _ticket(t, "S1", touched=OLD)
        c = purge.candidates(days=30, now=NOW)[0]
        assert c["voided"] is False and c["void_by"] == "" and c["void_at"] == ""


def test_the_window_comes_from_config_not_from_the_request():
    """POST 里带 days=0 就能删掉昨天的票的话，安静期就成了摆设。
    执行侧只读服务端那一份，天数由调用方（服务端）传进来，请求体里没有这个口。"""
    with _world() as t:
        stem = _ticket(t, "S1", touched=NOW)
        assert purge.plan("hawb", stem, days=0, now=NOW) is not None, "days=0 时应当立刻可算（这是预览用的能力）"
        r = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        assert r[0]["ok"] is False, "执行侧采信了请求带来的天数"
        assert r[0]["ok"] is False and "安静" in r[0]["error"]
        assert (config.ARCHIVE_DIR / stem).exists(), "拒绝之外还得多手：文件一个都不许动"


# ── 二、计划要覆盖"删干净"的每一个落点 ────────────────────────────────────────────

def test_the_hawb_plan_lists_every_place_that_ticket_has_data():
    with _world() as t:
        stem = _ticket(t, "S1")
        p = purge.plan("hawb", stem, 30, now=NOW)
        f, dr = _paths(p), {str(x) for x in p["dirs"]}
        for want in (str(config.INPUT_DIR / (stem + ".pdf")),
                     str(config.TRANSCRIPT_DIR / (stem + ".json")),
                     str(config.OUTPUT_RAW_DIR / (stem + ".json")),
                     str(config.OUTPUT_AIR_DIR / (stem + ".json")),
                     str(config.OUTPUT_QC_DIR / (stem + ".json")),
                     str(config.PREVIEW_DIR / (stem + ".pdf")),
                     str(config.STAGED_DIR / (stem + ".json")),
                     str(company_api.guard_file("99995764373|JRT28645")),
                     str(config.ARCHIVE_DIR / stem)):
            assert want in f or want in dr, "这个落点没进删除清单：" + want
        assert p["rebuild"] is True, "L2/L3 单删了不算删干净——all_hawbs*.json 里还整份留着那张票的值"


def test_the_master_files_are_left_alone_while_siblings_still_reference_them():
    """主单原件与主单缓存是一张主单下所有分单共享的。清一张分单就把它们删掉，
    同主单下还没清的票立刻没有票面可对——这是最容易顺手做错的一处。"""
    with _world() as t:
        a = _ticket(t, "S1")
        store.unvoid_row("hawb", a)
        b = _ticket(t, "S2")
        p = purge.plan("hawb", b, 30, now=NOW)
        assert not any("mawb_source" in x for x in _paths(p)), "删分单却把主单原件列进来了"
        assert not any(str(config.MASTER_DIR) in x for x in _paths(p)), "删分单却把主单缓存列进来了"


def test_purging_the_master_takes_its_files_only_when_no_submitted_hawb_still_points_at_it():
    with _world() as t:
        m = "999-95764373"
        _ticket(t, "S1", mawb=m)
        store.void_row("mawb", m, "admin")
        # 先把这张主单的作废戳摆满保留期：不然下面拒的是"没满期"，共享那道闸门等于没测到
        # （一次变异把 submitted_by_mawb 整条判据改成恒假，这套用例照样全绿，就是这么漏的）。
        _age_void("mawb", m, OLD)
        assert purge.plan("mawb", m, 30, now=NOW) is None, \
            "这张主单下还挂着已提交的分单，先删主单会把分单的票面来源抽走"
        # S1 已经被 _ticket 作废在 NOW 上；这里别再 void_row 一次——那会把时刻刷成机器当前
        # 时间（比 NOW 晚），"刚作废"就变成"负龄"，第一次删除被拒、主单就一直删不掉。
        purge.purge([{"kind": "hawb", "key": "hawb|S1"}], by="admin", days=0, now=NOW)
        p = purge.plan("mawb", m, 30, now=NOW)
        assert p is not None and str(config.MASTER_DIR / "99995764373.json") in _paths(p)
        assert str(config.MAWB_SOURCE_DIR / "99995764373") in {str(x) for x in p["dirs"]}


def test_a_bad_name_is_refused_and_produces_no_plan_at_all():
    """stem 与主单号都要拼进路径。这里漏一次校验，../../ 就变成"删服务器上任何地方"。"""
    with _world() as t:
        _ticket(t, "S1")
        for bad in ("../../etc", "a/b", "..\\x", "", "..", "S1/../S2", "x" * 300):
            assert purge.plan("hawb", bad, 30, now=NOW) is None, "这个 stem 没被挡掉：" + repr(bad)
        for bad in ("99../1", "999 957 64373/../x", "", "small", "99995764373;rm"):
            assert purge.plan("mawb", bad, 30, now=NOW) is None, "这个主单号没被挡掉：" + repr(bad)
        # 上面那条"x"*300 在本机是"碰巧"过的（Windows 的 is_file() 对超长路径只回 False），
        # Linux 单段上限 255 字节，stat 直接抛 ENAMETOOLONG——v1.15.0 在 VM 上第一次跑回归就是这么炸的。
        # 所以这一格要在校验那一步就挡掉（上传那边本来就限 120 字，超过 120 的 stem 不是这台系统写出来的），
        # 而"读不出的路径"也必须等于"不在盘上"，不许把整次删除崩掉。
        assert store.safe_stem("x" * 300) is None, "safe_stem 只管分隔符不管长度：" + repr("x" * 8) + "…要判非法"
        assert purge._json(config.OUTPUT_QC_DIR / ("x" * 300 + ".json")) == {}, \
            "一个读不出来的路径要把这张票当不在盘上，不是抛出去"


def test_a_key_written_into_the_void_ledger_by_hand_still_gets_no_plan():
    """上面那条只试了"请求里带个坏名"——那种键在作废本里查不到，plan() 走到第一道
    就返回 None，把安全校验整段删掉也不会红。真要看住的是**盘上那本 voided.json 自己**：
    它可以是有人手工编辑过的。删除计划的每一条路径都要重新过一遍校验，不认账本本身。"""
    with _world() as t:
        _ticket(t, "S1")
        store.void_row("hawb", "../../escaped", "admin")
        _age_void("hawb", "../../escaped", OLD)
        assert purge.plan("hawb", "../../escaped", 30, now=NOW) is None, \
            "作废本里躺着一个带穿越的键，plan() 就照着它拼路径了"
        got = purge.candidates(days=30, now=NOW)
        assert not any("escaped" in str(x) for x in got), "候选名单把这条也收进去了"


def test_a_never_submitted_ticket_still_carries_its_numbers():
    """只暂存过、没提交过的票在台账里没有条目，于是老写法给出的 mawb/hawb 是两格空串——
    浏览器里那一行就是「分单 · — · —」，管理员对着四张长得一样的票挑哪一张？删除凭据里同样
    只剩一个 stem。号要从 air 那份取回来（它本来就是这张票的 L3 结果）。"""
    with _world() as t:
        stem = _ticket(t, "S1", mawb="074-55430340", hawb="VC54380", sent=False)
        p = purge.plan("hawb", stem, 30, now=NOW)
        assert p["in_ledger"] is False
        assert p["mawb"] == "074-55430340" and p["hawb"] == "VC54380", \
            "没提交过的票在候选里连号都没有，等于让人盲选"
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        line = json.loads(config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()[-1])
        assert line["mawb"] == "074-55430340" and line["hawb"] == "VC54380", \
            "删除凭据里也要认得出这两格号"


def test_the_loading_state_never_wipes_the_controls_it_is_about_to_fill():
    """浏览器里点出来的：loadPurge 第一行 `$("#purgeBox").innerHTML = 在算哪些票到期了…`，
    而 #purgeBox 正是搜索框、总数、名单、全选、删除键那一排的容器——第一行就把自己要用的
    控件全删了，随后 renderPurge 去取 #purgeSum 拿到 null，面板当场死掉（40 多条静态用例
    一条都没红，因为它们查的是源码里的字符串，不是 DOM）。今日台账与统计那两个 box 之所以
    能这么写，是因为那两个 box 在 html 里是空的渲染区；这张面板的 box 装着控件。"""
    js, page = web_src.part("js/desk.js"), web_src.part("index.html")
    body = _fn(js, "loadPurge")
    assert '$("#purgeBox")' not in body, "加载态/失败态不许写进装着控件的那颗盒子"
    assert '$("#purgeList")' in body, "加载态与失败态都要落在名单那一格"
    # 面板用到的每个控件都要在 html 里真存在：renderPurge 里 #purgeSum 拿 null 就是这么来的
    for i in sorted(set(re.findall(r'\$\("#(purge[A-Za-z]+)"\)', js))):
        assert 'id="%s"' % i in page, "desk.js 用到面板里没有的控件：#" + i


def test_no_planned_path_escapes_the_known_roots():
    with _world() as t:
        stem = _ticket(t, "S1")
        p = purge.plan("hawb", stem, 30, now=NOW)
        roots = [config.INPUT_DIR, config.ARCHIVE_DIR, config.TRANSCRIPT_DIR,
                 config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_QC_DIR,
                 config.PREVIEW_DIR, config.STAGED_DIR, config.SUBMIT_GUARD_DIR,
                 config.MAWB_SOURCE_DIR, config.MASTER_DIR]
        for x in list(p["files"]) + list(p["dirs"]):
            assert any(Path(x) == r or r in Path(x).parents for r in roots), \
                "这个删除目标不在任何允许的根目录下：" + str(x)


def test_the_upload_copy_is_skipped_when_its_recorded_name_is_not_a_single_segment():
    """source_name 当年是用户传上来的文件名，进过台账也进过 qc。名字里带斜杠的，
    宁可这一格不删，也不能拿它拼路径。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        q = config.OUTPUT_QC_DIR / (stem + ".json")
        q.write_text('{"source_name":"../../etc/passwd"}', encoding="utf-8")
        p = purge.plan("hawb", stem, 30, now=NOW)
        assert not any("passwd" in x for x in _paths(p))


# ── 三、执行：顺序、幂等、逐张成败 ────────────────────────────────────────────────

def test_the_time_sources_are_deleted_last():
    """判据换成"安静了多久"之后多出来的一条硬要求：三处时间来源里，qc 与 staged 要排在
    删除清单的最后、归档目录排在它们之后（remove 是先 files 后 dirs）。顺序反过来的话，
    中途崩一次就可能把 qc 与 staged 都删了而 preview 还留着——那时安静时间再也算不出来，
    _aged 只能判"没满"，这张票就从名册上消失了，剩下那半截文件永远没人再来收。
    （孤儿数据这条不变式原本盯的是台账索引，现在同样的坑会出在"时间"上。）"""
    with _world() as t:
        stem = _ticket(t, "S1")
        p = purge.plan("hawb", stem, 30, now=NOW)
        derived = [str(x) for x in p["files"]]
        assert len(derived) >= 6, "派生物没铺满，这条就成了空断言：" + " ".join(derived)
        qc = str(config.OUTPUT_QC_DIR / (stem + ".json"))
        st = str(config.STAGED_DIR / (stem + ".json"))
        assert qc not in derived and st not in derived, \
            "qc 是名册的扫描键、staged 是时间来源，两个都不许排在派生物那一段里"
        second = [str(x) for x in list(p["dirs"]) + list(p["sources"])]
        assert second[-2:] == [st, qc], "最后两个动的必须是 staged 再 qc（qc 撑到最后）：" + " ".join(second)
        assert str(config.ARCHIVE_DIR / stem) in second, "归档目录也是时间来源之一，要在第二段里"
        # 主单同理：名册是按 master/<key>.json 扫的，它必须最后一个动
        # （另起一张没提交过的：同主单下只要有已提交的分单，共享那道闸门就会先把主单挡掉，
        #   那时的 None 是闸门给的，不是这一段在验的东西）
        other = _ticket(t, "S9", mawb="888-12345678", hawb="M9", sent=False)
        k = store.norm_no("888-12345678")
        m = purge.plan("mawb", k, 0, now=NOW)
        assert m is not None and [str(x) for x in m["sources"]] == [str(config.MASTER_DIR / (k + ".json"))], \
            "主单缓存要排在第二段最后，否则它一没、剩下的原件目录就再也没人来收了"


def test_a_half_deleted_ticket_stays_on_the_roster():
    """半途崩掉的那张，下一轮必须还在名册里——否则它就以"算不出安静时间"的方式永久漏掉。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        real = Path.unlink
        boom = [str(config.PREVIEW_DIR / (stem + ".pdf"))]

        def flaky(self, *a, **k):
            if str(self) in boom:
                raise OSError("模拟盘坏了")
            return real(self, *a, **k)

        Path.unlink = flaky
        try:
            r = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        finally:
            Path.unlink = real
        assert r[0]["ok"] is False
        assert [c["key"] for c in purge.candidates(days=30, now=NOW)] == ["hawb|" + stem], \
            "删了一半的票不再出现在名册上：剩下的文件永远没人来收了"
        assert purge.plan("hawb", stem, 30, now=NOW) is not None


def test_the_tombstone_lands_before_a_single_byte_is_deleted():
    """中途崩掉时，凭据必须已经在盘上、台账条目必须还没动——
    这样那张票还在候选里，能被重新挑出来删完，而不是变成查无此票的孤儿数据。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        real = Path.unlink
        boom = [str(config.PREVIEW_DIR / (stem + ".pdf"))]

        def flaky(self, *a, **k):
            if str(self) in boom:
                raise OSError("模拟盘坏了")
            return real(self, *a, **k)

        Path.unlink = flaky
        try:
            r = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        finally:
            Path.unlink = real
        assert r[0]["ok"] is False, "明明有一步失败了，却报成功"
        lines = [json.loads(x) for x in config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines() if x]
        assert len(lines) == 1 and lines[0]["by"] == "admin" and lines[0]["days"] == 30, \
            "失败的那次没有留下删除凭据"
        assert "S1" in store.ledger(), "半途失败却已经把台账条目删了：这张票从此查无此票"
        assert (config.ARCHIVE_DIR / stem).exists() or "S1" in [c["key"].split("|")[1]
                                                                for c in purge.candidates(days=30, now=NOW)]


def test_an_auto_run_is_labelled_as_auto_and_never_fakes_a_void_stamp():
    """自动档要能一眼看出来（by 是 "auto"、who 留空、auto 为真），而且它**绝不替人补一个作废戳**：
    voided.json 记的是"谁把这一行收起来了"，让定时器往里写一笔就等于把没人做过的判断
    写成有人做过——那本台账之后还要拿去解释正确率与台账计数的。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        r = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="auto", days=30,
                        now=NOW, auto=True)
        assert r[0]["ok"] is True
        line = json.loads(config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()[-1])
        assert line["auto"] is True and line["by"] == "auto" and line["who"] == ""
        assert store.voided_map() == {}, "自动档往作废本里写了一笔：那是伪造出来的判断"
        # 自动档不许把账算到某个人头上：调用方哪怕传进来一个名字，by 也必须是 "auto"
        # （光传 by="auto" 去验，`"by": "auto" if auto else who` 改成 `"by": who` 一样绿）
        stem2 = _ticket(t, "S2", hawb="JRT8")
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem2}], by="宛平", days=30,
                    now=NOW, auto=True)
        line2 = json.loads(config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()[-1])
        assert line2["by"] == "auto" and line2["who"] == "", "自动那一跑被记成了某个人的手笔"


def test_a_manual_click_keeps_the_login_name_in_the_credential():
    """人工档反过来要留得下人名：管理员挑出来按下去的，和日子到了自己走的，
    事后翻这本要分得清——这两件事的问责人是不同的。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="宛平", days=30, now=NOW)
        line = json.loads(config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()[-1])
        assert line["auto"] is False and line["by"] == "宛平" and line["who"] == "宛平"


def test_the_credential_records_whether_this_ticket_had_been_voided():
    """作废降级成凭据里的一个标记：躺够日子被自动清掉的票里，会混着"谁收起来过"的那些——
    对账时这两类是不同的故事（一类是有人判过不作数，一类只是再没人动过）。"""
    with _world() as t:
        a = _ticket(t, "S1", void=True)
        b = _ticket(t, "S2", hawb="JRT8", void=False)
        purge.purge([{"kind": "hawb", "key": "hawb|" + a}, {"kind": "hawb", "key": "hawb|" + b}],
                    by="auto", days=30, now=NOW, auto=True)
        lines = [json.loads(x) for x in config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()]
        by_key = {x["stem"]: x for x in lines}
        assert by_key[a]["voided"] is True and by_key[a]["void_by"] == "admin"
        assert by_key[b]["voided"] is False and by_key[b]["void_by"] == ""
        assert by_key[a]["quiet_at"] == OLD and by_key[a]["idle_days"] == 40, \
            "删的理由（安静了多少天）也要留在凭据里，不然过半年没人知道当时为什么删它"


def test_a_refused_item_says_why_in_terms_of_the_quiet_window():
    """拒绝的理由要说人话并且说对接的是哪一道闸门：面板与定时器都靠这句话告诉管理员
    "为什么这张不能删"，只回"不在候选里"等于什么都没答。"""
    with _world() as t:
        fresh = _ticket(t, "S1", touched=NOW)
        r = purge.purge([{"kind": "hawb", "key": "hawb|" + fresh}], by="admin", days=30, now=NOW)
        assert "安静" in r[0]["error"] and "30" in r[0]["error"], r[0]["error"]
        r = purge.purge([{"kind": "hawb", "key": "hawb|../escape"}], by="admin", days=30, now=NOW)
        assert "不合法" in r[0]["error"], "带穿越的键要有自己的那句，不能混进「没满期」"
        r = purge.purge([{"kind": "hawb", "key": "hawb|ghost"}], by="admin", days=30, now=NOW)
        assert "查无" in r[0]["error"], "盘上根本没有的票要说查无，不是「安静不够」"


def test_the_tombstone_file_is_not_readable_by_the_whole_machine():
    """purged.jsonl 里虽然没有票面内容，但有 void_by 与 by（两个人名）和"哪些票被抹了"。
    这台机器上凡是带人名的账本都收 600（submitted.json 含复核人姓名、users.json 含口令哈希、
    .env 含令牌），新建这一本凭什么按 umask 落成 644？efreight-vm 是多服务共用的机器。"""
    if os.name == "nt":
        return                       # Windows 的 chmod 只管只读位（这条由 VM 回归覆盖）
    with _world() as t:
        a = _ticket(t, "S1")
        purge.purge([{"kind": "hawb", "key": "hawb|" + a}], by="admin", days=30, now=NOW)
        assert config.PURGED_LEDGER.stat().st_mode & 0o777 == 0o600, "删除凭据这本要 600"
        b = _ticket(t, "S2", hawb="JRT9")
        purge.purge([{"kind": "hawb", "key": "hawb|" + b}], by="宛平", days=30, now=NOW)
        assert config.PURGED_LEDGER.stat().st_mode & 0o777 == 0o600, "第二次追加不许把权限改回去"


def test_a_fresh_tombstone_file_asks_for_600_when_it_is_opened():
    """上一条在 Windows 上是空跳（st_mode 看不出 chmod），所以这条查"要权限的方式"本身，两台机器都咬得住：
    权限要在打开新建那一次就给（os.open 的 mode 只在真的新建时生效，已存在则被内核忽略），
    而不是先按 umask 落成 644、再补一次 chmod——补的那一段时间里 output/ 是同机其它服务读得到的目录，
    而这个名字与人名已经写进去了。"""
    with _world() as t:
        seen = []
        real_open = os.open
        want = config.PURGED_LEDGER.name

        def spy(path, flags, mode=None, **kw):
            if Path(path).name == want:
                seen.append(mode)
            return (real_open(path, flags, mode, **kw) if mode is not None
                    else real_open(path, flags, **kw))

        purge.os.open = spy
        try:
            a = _ticket(t, "S1")
            purge.purge([{"kind": "hawb", "key": "hawb|" + a}], by="admin", days=30, now=NOW)
            assert seen and all(m == 0o600 for m in seen), \
                "打开 purged.jsonl 那一次就要 600（这台机器上有人名），不能靠默认 umask"
            before = len(seen)
            b = _ticket(t, "S2", hawb="JRT9")
            purge.purge([{"kind": "hawb", "key": "hawb|" + b}], by="宛平", days=30, now=NOW)
            assert len(seen) > before, "第二张也要走同一句（文件已存在时那个 mode 会被内核忽略）"
            assert all(m == 0o600 for m in seen)
        finally:
            purge.os.open = real_open


def test_the_tombstone_keeps_the_numbers_but_never_the_ticket_content():
    with _world() as t:
        stem = _ticket(t, "S1")
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        line = json.loads(config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()[-1])
        assert line["mawb"] == "999-95764373" and line["hawb"] == "JRT28645"
        assert line["kind"] == "hawb" and line["stem"] == stem and line["auto"] is False
        assert line["files"], "凭据要能回答删了哪几个文件"
        for secret in ("STEEL", "SHIPPER", "GOODS_INFO"):
            assert secret not in json.dumps(line), "凭据里混进了票面内容：" + secret


def test_a_deleted_ticket_leaves_no_dangling_key_behind():
    """票没了，两本索引里的条目得跟着没。`voided.json` 留着 `hawb|S1` 就是一个指向
    已经不存在的东西的键：统计与「只看作废」还可能把它翻出来，而它再也进不了任何一轮清理。"""
    with _world() as t:
        stem = _ticket(t, "S1", void=True)
        assert store.ledger() and store.voided_map(), "夹具没把这张票摆进两本索引，下面的断言是空的"
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        assert stem not in store.ledger(), "台账条目还留着：文件没了、账上还有一张"
        assert store.voided_map() == {}, "作废本里留了个悬空键"


def test_purging_twice_is_a_no_op_and_does_not_raise():
    with _world() as t:
        stem = _ticket(t, "S1")
        first = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        again = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        assert first[0]["ok"] is True
        assert again[0]["ok"] is False, "第二次应当说清楚「这张票已经不在了」，不能静默报成功"
        assert len(config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()) == 1, \
            "同一张票留下两条凭据，事后没法知道到底删了几次"


def test_a_batch_reports_each_ticket_rather_than_failing_the_whole_run():
    with _world() as t:
        a = _ticket(t, "S1")
        b = _ticket(t, "S2", hawb="JRT9", touched=NOW)     # 昨天刚被碰过
        r = purge.purge([{"kind": "hawb", "key": "hawb|" + a},
                         {"kind": "hawb", "key": "hawb|" + b}], by="admin", days=30, now=NOW)
        assert [x["ok"] for x in r] == [True, False], "第二张没满安静期，不能跟着第一张一起被删"
        assert (config.ARCHIVE_DIR / b).exists(), "同批里没过检查的票也被删了"


def test_the_database_is_asked_to_drop_the_same_ticket():
    """配了 DB_URL 时 hawb_raw/hawb_air 里各多一份副本：不点这个名，"服务器上删干净了"
    就是假话。回归一律离线，所以这里不连库——只看那一句调用真发了、报的是这张票。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        seen = []
        real = db_writer.delete_ticket
        db_writer.delete_ticket = lambda sf: seen.append(sf)
        try:
            purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        finally:
            db_writer.delete_ticket = real
        assert seen == [stem], "没让数据库删这份副本，或者删的不是这张票"


def test_the_aggregates_and_the_render_cache_do_not_keep_the_ticket_alive():
    with _world() as t:
        stem = _ticket(t, "S1")
        store.rebuild_summary()
        (config.OUTPUT_RAW_DIR / "all_hawbs.json").read_text(encoding="utf-8")
        server._RENDER_CACHE[stem, 1, 2.0] = b"PNG"
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        assert not any(stem in json.dumps(v) for v in _jsons(config.OUTPUT_RAW_DIR / "all_hawbs.json")), \
            "all_hawbs.json 里还留着这张票"
        assert not any(stem in json.dumps(v) for v in _jsons(config.OUTPUT_AIR_DIR / "all_hawbs_air.json"))
        assert not [k for k in server._RENDER_CACHE if k[0] == stem], \
            "页图缓存没清：删完还能从缓存翻出这张票的票面"


def _age_void(kind, key, at):
    """把作废戳改成指定时刻。判据改了之后它不再是"到期时间"的输入，
    但行上的标记与凭据里那一格记的就是这个时刻，测试里不能拿"机器现在"当历史。"""
    data = store.voided_map()
    data[store.void_key(kind, key)]["at"] = at
    store._write_ledger(data, config.VOID_LEDGER)


# ── 四、保留期配置 ───────────────────────────────────────────────────────────────

def test_retention_days_are_clamped_and_survive_a_restart():
    assert config.PURGE_DAYS_RANGE == (1, 3650)
    with _world() as t:
        config.PURGE_AFTER_DAYS = 30
        assert purge.retention() == 30
        assert purge.set_retention(7) == 7 and purge.retention() == 7
        # 越界与非数字一律"没收下"（None）：静默保留原值最坏——管理员填了 0、界面不报错、
        # 他以为改成了，而盘上还是 7 天。
        for bad in (0, -5, 99999, "abc", "", None, "12天"):
            assert purge.set_retention(bad) is None, "这个值不该被收下：" + repr(bad)
            assert purge.retention() == 7, "被拒的值把原值改掉了"
        # 写了就要活过重启：落在 .env 那一行，不是只改进进程内存
        assert "PURGE_AFTER_DAYS=7" in config.ENV_FILE.read_text(encoding="utf-8")


# ── 五、服务端：三个口都只认管理员会话 ────────────────────────────────────────────

def _client(t):
    auth.ensure_seed()
    c = TestClient(server.app)
    r = c.post("/login", json={"name": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return c


def test_the_three_purge_endpoints_need_an_admin_session():
    with _world() as t:
        c = _client(t)
        assert c.get("/purge/candidates").status_code == 200
        assert c.post("/purge", json={"items": []}).status_code == 200
        anon = TestClient(server.app)
        assert anon.get("/purge/candidates").status_code == 401
        assert anon.post("/purge", json={"items": []}).status_code == 401
        # 种子里的制单员/录入员口令是空的（还没下发），所以先由管理员把口令设上再验门禁
        for name, role in (("马殿齐", "reviewer"), ("刘明", "inputter")):
            assert c.post("/admin/users", json={"name": name, "password": "pw123456",
                                                "role": role}).status_code == 200
            u = TestClient(server.app)
            assert u.post("/login", json={"name": name, "password": "pw123456"}).status_code == 200
            assert u.get("/purge/candidates").status_code == 403, role + " 读到了清理候选"
            assert u.post("/purge", json={"items": []}).status_code == 403, role + " 能发起彻底删除"
            assert u.post("/purge/config", json={"days": 1}).status_code == 403


def test_post_purge_ignores_any_days_the_request_brings_and_refuses_off_list_items():
    with _world() as t:
        c = _client(t)
        stem = _ticket(t, "S1")
        keep = _ticket(t, "S2", hawb="JRT9", touched=NOW)
        got = c.post("/purge", json={"days": 0, "items": [{"kind": "hawb", "key": "hawb|" + stem},
                                                          {"kind": "hawb", "key": "hawb|" + keep},
                                                          {"kind": "hawb", "key": "hawb|不存在"}]})
        r = got.json()["results"]
        assert [x["ok"] for x in r] == [True, False, False], \
            "请求里带 days=0 就把没满安静期的第二张也删了——安静期只认服务端那一份"
        assert (config.ARCHIVE_DIR / keep).exists()
        assert c.post("/purge", json={"items": [{"kind": "hawb", "key": "hawb|../../etc"}]}).json()["results"][0]["ok"] is False


def test_the_panel_endpoints_answer_in_the_shape_the_front_end_draws_from():
    with _world() as t:
        c = _client(t)
        stem = _ticket(t, "S1")
        j = c.get("/purge/candidates").json()
        assert j["days"] == 30 and len(j["items"]) == 1
        assert j["total_bytes"] >= j["items"][0]["bytes"] > 0
        assert {"kind", "key", "mawb", "hawb", "void_by", "void_at", "in_ledger", "bytes"} <= set(j["items"][0])
        assert {"voided", "quiet_at", "idle_days"} <= set(j["items"][0]), \
            "判据改了：行上要能答「这张安静了多少天、有没有人动作废过」"
        assert j["last_run"] == {}, "从没跑过自动清理时要是空的，不能是一个像跑过的默认时间"
        assert c.post("/purge/config", json={"days": 15}).json()["days"] == 15
        assert c.post("/purge/config", json={"days": 0}).status_code == 400
        assert c.get("/purge/config").json()["days"] == 15


def test_the_server_never_hands_the_browser_absolute_paths_or_delete_plans():
    """候选接口是给人看的，不是把「要删哪些路径」发给浏览器。绝对路径既没用途，
    又把服务器目录结构原样摊在一个只读页面上。"""
    with _world() as t:
        c = _client(t)
        stem = _ticket(t, "S1")
        raw = c.get("/purge/candidates").text
        assert str(t) not in raw, "响应里带了服务器绝对路径"
        for leak in ("files", "dirs", "sources", "drop"):
            assert '"%s"' % leak not in raw, "%s 这种执行细节不该上界面" % leak


# ── 六、前端：清理面板 ────────────────────────────────────────────────────────────

def test_the_panel_is_an_admin_entry_carrying_the_candidate_count():
    page, js = web_src.part("index.html"), web_src.part("js/desk.js")
    for i in ("btnPurge", "purgeWin", "purgeBox", "purgeDays", "purgeQuery", "purgeList"):
        assert 'id="%s"' % i in page, "面板缺控件：" + i
    assert '$("#btnPurge").hidden = !admin' in js, \
        "入口只对管理员可见（与 /purge 的服务端门禁同一口径）"
    # 徽标现在挂在顶栏「设置」那颗按钮上（v1.17.0 把管理员的六个入口收进一处）：
    # 入口能收，"开门就看见有几张到期"这条不能收，否则它只能靠人记得点进设置。
    assert "btnSetCnt" in js and '$("#btnSetCnt")' in js, "入口上要有候选数徽标：到期该清的得在开门时就看见"
    assert "if (admin) purgeBadgeLoad();" in js, \
        "徽标要在管理员登录时拉一次：只钉「purgeBadgeLoad()」这几个字的话，" \
        "把它挂到 `if (false)` 上也算过"
    assert 'id="viewPurge"' not in page, "这不是第四个页签，是一颗浮层入口"


def test_search_only_narrows_the_list_and_never_widens_it():
    """搜索框那句"输个号就能删"是这页最危险的误读：它只能从候选里往下筛。
    判据写成纯函数由 node 断输入输出，渲染里再写一遍迟早不一致。"""
    items = [{"key": "hawb|S1", "mawb": "999-95764373", "hawb": "JRT28645"},
             {"key": "hawb|S2", "mawb": "074-55430340", "hawb": "VC54380"},
             {"key": "mawb|99995764373", "mawb": "999-95764373", "hawb": ""}]
    assert [x["key"] for x in web_src.logic("L.purgeFilter(it, q)", it=items, q="VC54")] == ["hawb|S2"]
    assert [x["key"] for x in web_src.logic("L.purgeFilter(it, q)", it=items, q="99995764373")] == \
        ["hawb|S1", "mawb|99995764373"], "归一化之后同一种写法要能一起命中"
    assert web_src.logic("L.purgeFilter(it, q)", it=items, q="") == items
    assert web_src.logic("L.purgeFilter(it, q).length", it=items, q="没有这张") == 0
    n = web_src.logic("L.purgeFilter(it, q).length", it=items, q="S1/../S2")
    assert n <= len(items), "带穿越的查询串不该产出任何一条新东西"
    assert web_src.logic("L.purgeFilter(null, 'x')") == []


def test_the_totals_the_panel_shows_come_from_one_place():
    with_bytes = [{"bytes": 1200}, {"bytes": 3400}]
    assert web_src.logic("L.purgeSum(it)", it=with_bytes) == {"n": 2, "bytes": 4600}
    assert web_src.logic("L.purgeSum(null)") == {"n": 0, "bytes": 0}
    # 徽标那条路径不算 bytes（stat 归档目录比读两份 JSON 贵），所以它得能只报条数
    assert web_src.logic("L.purgeSum(it).n", it=with_bytes) == 2
    # 光测纯函数不够：面板自己再 reduce 一遍，上面这些照样全绿，而「勾朝几张 / 一共多少」
    # 迟早分家（一条把 sum 改成就地累加的变异，当时零条用例变红）。
    js = web_src.part("js/desk.js")
    assert "sum = L.purgeSum(picked)" in js, "面板那行总数要由 L.purgeSum 一处算"
    assert "L.purgeBytes(sum.bytes)" in js, "显示的要就是 purgeSum 算出来的那一份"


def test_byte_sizes_are_read_the_way_a_human_reads_them():
    g = lambda n: web_src.logic("L.purgeBytes(n)", n=n)
    assert g(0) == "0 B" and g(999) == "999 B"
    assert g(1024) == "1.0 KB" and g(4600) == "4.5 KB"
    assert g(4 * 1024 * 1024) == "4.0 MB"
    assert g(3 * 1024 ** 3) == "3.0 GB"
    assert g(None) == "—" and g(-5) == "—", "负数是算坏了，别照着一个坏数显示"


def test_the_first_click_never_fires_the_delete():
    """不可逆操作要两步：第一下只把按钮变成"确认删这几张"，不发请求。
    与台账上「作废」那一下同一个规矩——区别只在这里删完没有反悔的按钮。"""
    js = web_src.part("js/desk.js")
    assert '$("#purgeGo")' in js, "确认那颗按钮没接上"
    assert "PURGE.armed" in js, "两步状态要记在面板状态上：重画一次就忘了自己点过第一下，等于一步确认"
    seg = _fn(js, "purgeArm")
    assert "fetch(" not in seg, "第一下就发请求了"
    fire = _fn(js, "purgeFire")
    assert 'fetch(BASE + "/purge"' in fire, "第二下才该真的打删除"
    assert "purgeArm(" not in fire, "第二次点又退回第一步：那这页永远删不掉东西"


def _jsCode():
    """剥掉块注释再读 desk.js（保住行数）：钉"界面会说出这几个字"的断言，
    不能被自己写在源码里的注释喂饱——上一轮就是这么漏的两条。"""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  web_src.part("js/desk.js"), flags=re.S)


def _fn(js, name):
    """按函数名切一段源码。切不到就给一条干净的断言失败，不是 ValueError。
    边界要找 function 也要找 async function：只找前者的话，purgeArm 的切片会把下一个
    async function purgeFire 的 fetch 一起圈进来，"第一下不许发请求"这条就永远红。"""
    i = js.find("function " + name + "(")
    assert i >= 0, "desk.js 里还没有 %s 这个函数" % name
    m = re.compile(r"\n(?:async\s+)?function ").search(js, i + 1)
    return js[i:m.start()] if m else js[i:i + 3000]


def test_each_row_says_which_ticket_it_is():
    """每一行都要说得出"这是哪张票"：号之外还得有票名（stem 就是归档目录名，
    运维拿它去盘上找东西），而五个勾选框共用一句 aria-label 时，读屏器根本分不出勾的哪一张。"""
    js = _jsCode()
    body = _fn(js, "renderPurge")
    assert 'class="pks"' in body and "esc(name)" in body, "候选行里没有票名"
    assert "const name = it.stem || it.mawb" in body, "票名要从 stem 取：没提交过的票两格号都是空的"
    assert 'aria-label="选择这一张"' not in body, "每行一样的 aria-label，读屏器分不出勾的是哪一张"
    assert 'aria-label="选择 ${' in body, "勾选框的标签要带上这一行是谁"


def test_a_refused_retention_leaves_no_doubt_about_what_is_in_force():
    """保存被服务端拒了（0 天越界），界面必须回到"实际生效的那一个"：
    框里留着人刚填的 0 而盘上还是 30，下一次他就以为满 0 天能删。
    浏览器里点出来的——失败那一路只 toast，没把 #purgeDays 收回去。"""
    js = _jsCode()
    body = _fn(js, "purgeSaveDays")
    assert "catch" in body
    tail = body[body.index("catch"):]
    assert '$("#purgeDays").value = PURGE.days' in tail, \
        "失败要把框里的值退回服务端那一份（PURGE.days 只从服务端响应里来）"
    assert '"bad")' in tail, "被拒也要说够 9 秒：那是要人改完再点一次的"


def test_the_company_side_limit_is_written_into_the_panel_not_flashed_once():
    """公司 j9 那一侧删不掉。这句话只在 toast 里闪一下，等于没写——
    它会常驻在候选列表上方，因为这是点删除之前该看见的唯一一句。"""
    js, page = web_src.part("js/desk.js"), web_src.part("index.html")
    assert "公司" in page and "j9" in page, "面板里没有那句边界说明"
    i = js.index("function renderPurge(")
    body = js[i:js.index("\nfunction ", i + 1)]
    assert "purgeNote" in body or "公司" in body


def test_the_evidence_column_survives_to_the_panel():
    """「是否在提交台账」这一列必须一路带到界面上：要抹的就是有回传证据的那批，
    管理员得在点下去之前看清这张票到底发过没有。"""
    js = web_src.part("js/desk.js")
    i = js.index("function renderPurge")
    body = js[i:i + 3000]
    assert "in_ledger" in body or "回传" in body, "证据那一列没到界面"
    for frag in ("void_by", "void_at"):
        assert frag in body or frag.replace("_", "") in body.replace("_", ""), "%s 没摆出来" % frag


def test_the_retention_control_is_in_the_panel_and_saves_to_the_server():
    js = web_src.part("js/desk.js")
    assert 'BASE + "/purge/config"' in js, "保留期要真的写回服务端那一份，不是记在浏览器里"
    seg = _after(js, "function purgeSaveDays(")
    assert "purge/config" in seg and "loadPurge" in seg, "保存后要重算候选：保留期一变，名单就变"
    assert "/purge/config" not in _after(js, "function purgeFire(", 400), \
        "删除那一下不该顺手把天数带过去——服务端只认自己那一份"


def test_a_batch_reports_each_ticket_to_the_operator():
    """逐张成败：一批里有一张卡住，不许把其余的报成就功（提交那圈已经踩过这个）。"""
    js = web_src.part("js/desk.js")
    i = js.index("function purgeDone(")
    body = js[i:js.index("\nfunction ", i + 1)]
    assert "ok" in body and ("失败" in body or "没删掉" in body), "只报了总数，没报是哪几张"
    assert "filter(r => r.ok).length" not in body or "r.ok" not in body.split("失败")[0], \
        "只报个成功数就够了：那正是把失败那张糊过去的那写法"


def test_nothing_on_the_roster_says_why_and_what_would_change_it():
    """空名单不能是一句"没有票"：这一页现在说的是"没有躺够日子的票"，
    管理员要能看出下一步是什么（等，还是把安静期改短），而不是以为系统坏了。"""
    js = _jsCode()
    body = _fn(js, "renderPurge")
    # 只切空态那一段：整函数里"安静/天"到处都有，扫全文的那句断言等于没断（变异验证抓出来的）
    head = body.split("if (!PURGE.items.length){", 1)
    assert len(head) == 2, "renderPurge 里那段空态没了"
    seg = head[1].split("purgeDrawGo();", 1)[0]
    assert "安静" in seg and "天" in seg, "空态要说清是「没躺够日子」，不是「没有票」"
    assert "PURGE.days" in seg, "空态要把当下那个窗口报出来：天数是从服务端来的那一个，不是抄死的"
    for banned in ("要先作废", "先点作废", "已作废且满"):
        assert banned not in js, "判据改了，面板里还挂着旧口径的那句话：" + banned


def test_the_panel_says_when_the_last_auto_run_happened():
    """定时器是另一个进程；面板不显示"上次跑成什么样"，失败就只有日志里有——
    而 systemd 日志不是管理员会去翻的地方。"""
    page = web_src.part("index.html")
    js = _jsCode()
    assert 'id="purgeRun"' in page, "面板上要有那句「上次自动清理」的位置"
    assert "PURGE.run" in js, "服务端给的那份状态要存在面板状态里，别只拼一次字符串"
    assert "从没跑过" in js, "没有状态时要说「从没跑过」，不能留一个空日期看着像跑过"
    # 光钉字符串不够：`PURGE.run = null` 照样留着那句「从没跑过」，而它永远显示的是没跑过
    assert "j.last_run" in _fn(js, "loadPurge"), "那句状态没接候选接口返回的 last_run"


def test_the_row_names_the_quiet_days_that_put_it_on_the_roster():
    """判据换成了"安静了多久"，那一格就必须上屏：管理员看名单要能答"这张为什么能删"，
    而作废只是标记——没人点过作废的那张得写出来，不能糊成一个空格。"""
    body = _fn(_jsCode(), "renderPurge")
    assert "idle_days" in body, "安静了多少天没上屏：删的理由看不见"
    assert "voided" in body, "作废标记没按事实写"


def test_the_roster_is_not_pre_selected():
    """不可逆操作不"开门就全选、点两下抹光"：默认一张都不勾，
    要提前删的人必须自己逐张勾一遍（自动那一轮本来就不经过这个面板）。"""
    js = _jsCode()
    assert "PURGE.sel[it.key] === true" in js, \
        "默认必须是未勾选（旧写法 !== false 等于开门就全选）"
    assert "PURGE.sel[it.key] !== false" not in js, "这句还是默认全选"
    assert 'if (allOn) { PURGE.sel[it.key] = true; continue; }' not in js   # 全选按钮自己判


# ── 五、自动那一路：run_purge.py 与它留下的状态 ──────────────────────────────────

def _run(cap, argv):
    import run_purge
    buf = io.StringIO()
    rc = run_purge.main(argv, out=buf)
    cap.append(buf.getvalue())
    return rc


def test_run_purge_dry_run_names_the_tickets_and_deletes_nothing():
    """先给用户看名单，再决定要不要动手——这一条是整个自动化的刹车：
    不带 --auto 时它**什么都不许删**，也不写"跑过"的状态（没跑过就别报时间）。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        out = []
        rc = _run(out, [])
        text = out[0]
        assert rc == 0
        assert stem in text and "999-95764373" in text, "dry-run 要说清是哪张票、号是多少"
        assert "420" in text or "KB" in text or "B" in text, "每张要占多少字节得报出来"
        assert (config.ARCHIVE_DIR / stem).exists(), "没带 --auto 就把票删了"
        assert not config.PURGED_LEDGER.exists(), "dry-run 不该留删除凭据"
        assert not config.PURGE_RUN_STATE.exists(), "dry-run 没跑过，别写一个假的上次运行时间"


def test_run_purge_auto_clears_the_roster_and_reports_each_ticket():
    with _world() as t:
        # 不带主单那两份：否则两张分单删完后，它们共用的主单会**新**进名册（那是下一条用例）
        a = _ticket(t, "S1", master=False)
        b = _ticket(t, "S2", hawb="JRT8", master=False)
        out = []
        rc = _run(out, ["--auto"])
        assert rc == 0, "全成都得退 0"
        assert not (config.ARCHIVE_DIR / a).exists() and not (config.ARCHIVE_DIR / b).exists()
        state = json.loads(config.PURGE_RUN_STATE.read_text(encoding="utf-8"))
        assert state["checked"] == 2 and state["removed"] == 2 and state["failed"] == 0
        assert state["days"] == 30 and state["at"]
        assert purge.candidates(days=30, now=NOW) == []
        lines = [json.loads(x) for x in config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()]
        assert all(x["auto"] is True and x["by"] == "auto" for x in lines)


def test_the_master_joins_the_roster_only_after_its_siblings_are_gone():
    """共享那道闸门的另一半：删完同主单下所有已提交的分单之后，主单自己才进名单——
    自动清理工信它不该在这一轮顺手把主单原件抽走（那是同主单下别的票还要对账的场景）。"""
    with _world() as t:
        m = "235-96146363"
        _ticket(t, "SIB-A", mawb=m, hawb="S1A")
        _ticket(t, "SIB-B", mawb=m, hawb="S1B")
        keys = [c["key"] for c in purge.candidates(days=30, now=NOW)]
        assert "mawb|" + store.norm_no(m) not in keys, "名下还有已提交分单，主单不该在名单上"
        out = []
        _run(out, ["--auto"])
        got = purge.candidates(days=30, now=NOW)
        assert [c["key"] for c in got] == ["mawb|" + store.norm_no(m)], \
            "兄弟票都清完了，主单要自己进名单，下一轮由它把原件与缓存收走"
        assert (config.MAWB_SOURCE_DIR / store.norm_no(m)).is_dir(), "上一轮就把主单原件删了"
        _run(out, ["--auto"])
        assert purge.candidates(days=30, now=NOW) == []
        assert not (config.MAWB_SOURCE_DIR / store.norm_no(m)).exists()


def test_run_purge_reports_a_failure_instead_of_swallowing_it():
    """定时器最怕的就是"失败了只躺在日志里"：退出码要非 0，状态里要写着几张没删掉、为什么，
    面板顶上那句「上次自动清理」才看得见它。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        real = Path.unlink
        boom = [str(config.PREVIEW_DIR / (stem + ".pdf"))]

        def flaky(self, *a, **k):
            if str(self) in boom:
                raise OSError("模拟盘坏了")
            return real(self, *a, **k)

        Path.unlink = flaky
        out = []
        try:
            rc = _run(out, ["--auto"])
        finally:
            Path.unlink = real
        assert rc != 0, "有一张没删掉还退 0，timer 与运维都看不见"
        state = json.loads(config.PURGE_RUN_STATE.read_text(encoding="utf-8"))
        assert state["failed"] == 1 and state["removed"] == 0
        assert state["errors"] and stem in state["errors"][0], "状态里要点名是哪张、为什么"


def test_run_purge_on_an_empty_roster_says_so_and_still_stamps_the_run():
    """名册空着也要留一行"跑过、0 张"：管理员要能分清"定时器没跑"与"跑了但没到期"——
    这两件事的处理动作完全相反。"""
    with _world() as t:
        _ticket(t, "S1", touched=NOW)
        out = []
        rc = _run(out, ["--auto"])
        assert rc == 0
        assert "没有" in out[0] or "0 张" in out[0]
        state = json.loads(config.PURGE_RUN_STATE.read_text(encoding="utf-8"))
        assert state["checked"] == 0 and state["removed"] == 0 and state["failed"] == 0


def test_the_candidates_endpoint_carries_the_last_auto_run():
    """面板那句「上次自动清理」不能前端自己猜：它来自服务端读的那份状态。"""
    with _world() as t:
        c = _client(t)
        assert c.get("/purge/candidates").json()["last_run"] == {}
        purge.write_run_state([{"ok": True, "key": "hawb|S1", "removed": [], "error": ""}], 30)
        j = c.get("/purge/candidates").json()
        assert j["last_run"]["removed"] == 1 and j["last_run"]["failed"] == 0
        assert j["last_run"]["at"], "跑过的时间要一起给，不然那句说明没头没尾"


def test_the_timer_unit_is_shipped_but_not_enabled():
    """定时这件事交给 systemd 而不是 web 进程（Restart=always 会把进程内循环每次重置，
    而失败只有日志看得见）。但装 timer 是**改这台共用机的系统配置**，得用户点头：
    文件与说明跟着部署走，脚本一行都不许替它 enable/start。"""
    svc = (config.BASE_DIR / "deploy" / "hawb-purge.service").read_text(encoding="utf-8")
    tm = (config.BASE_DIR / "deploy" / "hawb-purge.timer").read_text(encoding="utf-8")
    assert "run_purge.py --auto" in svc, "oneshot 要跑的就是那个入口，不是前端那个口"
    assert "Type=oneshot" in svc
    assert "OnCalendar=" in tm and "Persistent=true" in tm, "机器关机错过一夜，开机要能补跑一次"
    for f in ("sync.sh", "release.sh"):
        body = (config.BASE_DIR / "deploy" / f).read_text(encoding="utf-8")
        assert not re.search(r"systemctl\s+(enable|start)[^\n]*purge", body), \
            f + " 自作主张把 timer 装上了：那是需要人明确同意的一次系统改动"
    readme = (config.BASE_DIR / "deploy" / "README-ubuntu.md").read_text(encoding="utf-8")
    assert "hawb-purge.timer" in readme and "未启用" in readme, \
        "说明里要写清这两份文件是备好的、没装的——否则下一个人会以为自动清理已经在跑"


def test_the_roster_and_the_credential_never_carry_ticket_content():
    """自动清票会绕过"人先看过这张票"这一步，所以那句老规矩更要钉死：
    名册与凭据都只准带号、时间与文件名，一格里都不许出现收发货人或货物内容。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        # 名册按接口给出去的那一份算（绝对路径本来就被剥掉了，那不该算泄漏）
        raw = json.dumps([{k: v for k, v in i.items()
                           if k not in ("files", "dirs", "sources", "drop")}
                          for i in purge.candidates(days=30, now=NOW)], ensure_ascii=False)
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="auto", days=30,
                    now=NOW, auto=True)
        raw += config.PURGED_LEDGER.read_text(encoding="utf-8")
        for secret in ("STEEL", "SHIPPER", "GOODS_INFO"):
            assert secret not in raw, "名单或凭据里混进了票面内容：" + secret


# ── 切片小工具：切不到就给一条干净的断言，不是一句 ValueError ──────────────────────

def _after(js, needle, span=700):
    i = js.find(needle)
    assert i >= 0, "desk.js 里找不到 %r 这一段" % needle
    return js[i:i + span]


def _jsons(p):
    return [json.loads(p.read_text(encoding="utf-8"))] if p.exists() else []
