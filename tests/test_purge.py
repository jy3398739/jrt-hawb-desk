# -*- coding: utf-8 -*-
"""彻底删除（数据清理）：候选、计划、执行。

这一段是全系统里唯一会**不可逆抹掉数据**的地方，所以用例的重点不是"删得掉"，而是那几条
"不许删"：没作废过的票、作废了但没满保留期的票、名字里带穿越的票、主单下面还挂着别的分单的票。
执行顺序也钉住——先写凭据再删文件最后才动台账；反过来做，半途崩一次就留下一张
"文件还在盘上、但台账里已经查无此票"的孤儿数据，那种数据比没删更难对付。
"""
import contextlib
import json
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
           config.PURGE_AFTER_DAYS, auth.USERS_FILE, config.ENV_FILE)
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
        config.PURGE_AFTER_DAYS = 30
        auth.USERS_FILE = t / "users.json"
        # 保留期写的是 .env，而回归跑完会核对 .env 有没有被改过（config.ENV_FILE 不吃
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
             config.PURGE_AFTER_DAYS, auth.USERS_FILE, config.ENV_FILE) = old


def _ticket(t, stem, mawb="999-95764373", hawb="JRT28645", sent=True, master=True):
    """在盘上摆一张"经手过"的票：原件、L1/L2/L3/红旗、预览、暂存、台账、幂等锁。"""
    (config.INPUT_DIR / (stem + ".pdf")).write_bytes(b"RAW-UPLOAD")
    a = config.ARCHIVE_DIR / stem
    a.mkdir(parents=True, exist_ok=True)
    (a / (stem + ".pdf")).write_bytes(b"ORIGINAL")
    (a / "manifest.json").write_text('{"md5":"x"}', encoding="utf-8")
    for p in (config.TRANSCRIPT_DIR / (stem + ".json"),):
        p.write_text('{"flags":[]}', encoding="utf-8")
    # qc 里要带 source_name：input 那一格是按它去找的，缺了就等于这条路径没被测到。
    # 再带两格票面内容：凭据那条用例是「扫有没有混进票面内容」，夹具里没有内容的话
    # 它就是在扫一份空字符串——把 _row 改成整份抄走也不会红。
    (config.OUTPUT_QC_DIR / (stem + ".json")).write_text(
        '{"flags":[],"source_name":"%s.pdf","GOODS_INFO":"STEEL PARTS",'
        '"SHIPPER_INFO_NAME":"SHIPPER CO LTD"}' % stem, encoding="utf-8")
    # raw/air 里要真的带上 stem：all_hawbs.json 是从这些文件汇总出来的，
    # 记录里不含 stem 的话「聚合副本删没删干净」那条用例就是空的，永远绿。
    (config.OUTPUT_RAW_DIR / (stem + ".json")).write_text(
        '{"source_file":"%s","HAWB_NO":"%s"}' % (stem, hawb), encoding="utf-8")
    (config.OUTPUT_AIR_DIR / (stem + ".json")).write_text(
        '{"source_file":"%s","MAWB_NO":"%s","HAWB_NO":"%s"}' % (stem, mawb, hawb),
        encoding="utf-8")
    (config.PREVIEW_DIR / (stem + ".pdf")).write_bytes(b"PREVIEW")
    (config.STAGED_DIR / (stem + ".json")).write_text('{"air_final":{}}', encoding="utf-8")
    if sent:
        store.mark_submitted(stem, mawb, hawb, "宛平", {"mode": "mock"})
        # 真提交过才会留下幂等锁：不摆上盘，「锁也要删」这一条就是空的
        company_api.guard_file(store.number_key(mawb, hawb)).write_text(
            '{"key":"k"}', encoding="utf-8")
    if master:
        k = store.norm_no(mawb)
        (config.MASTER_DIR / (k + ".json")).write_text('{"mawb":"%s"}' % k, encoding="utf-8")
        md = config.MAWB_SOURCE_DIR / k
        md.mkdir(parents=True, exist_ok=True)
        (md / "m.pdf").write_bytes(b"MASTER-ORIGINAL")
    store.void_row("hawb", stem, "admin")
    # 作废戳默认落在 NOW 上（=「刚作废」）。void_row 打的是机器当前时刻，而本机时钟可以
    # 以比 NOW 晚好几个小时——不覆写一次，"负龄"会让刚作废的票直接算成够老，
    # 保留期那条不变式就悄悄没被测到。
    _age_void("hawb", stem, NOW)
    return stem


def _paths(p):
    return {str(x) for x in list(p["files"]) + list(p["dirs"])}


# ── 一、唯一不变式：没作废过的票删不了 ────────────────────────────────────────────

def test_a_ticket_that_was_never_voided_is_never_a_candidate():
    with _world() as t:
        stem = _ticket(t, "S1")
        store.unvoid_row("hawb", stem)
        assert purge.candidates(days=30, now=NOW) == [], "没打作废戳的票进了候选"
        assert purge.plan("hawb", stem, 30, now=NOW) is None, "没作废的票居然能算出删除计划"


def test_a_voided_ticket_younger_than_the_window_is_not_a_candidate():
    with _world() as t:
        stem = _ticket(t, "S1")
        store.void_row("hawb", stem, "admin")          # 刚作废（时刻就是现在）
        assert purge.candidates(days=30, now=NOW) == []
        assert purge.plan("hawb", stem, 30, now=NOW) is None, \
            "保留期是给人反悔用的，刚作废就允许删等于没有保留期"


def test_an_aged_voided_ticket_is_a_candidate_with_the_columns_the_panel_shows():
    with _world() as t:
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, OLD)
        got = purge.candidates(days=30, now=NOW)
        assert [c["key"] for c in got] == ["hawb|" + stem]
        c = got[0]
        assert c["in_ledger"] is True, "在不在提交台账要如实报——管理员要抹的就是有证据的那批"
        assert c["void_by"] == "admin" and c["void_at"] == OLD
        assert c["mawb"] == "999-95764373" and c["hawb"] == "JRT28645"
        assert c["bytes"] > 0, "面板那一列本机占用得有数，不是摆个空位"


def test_the_retention_window_comes_from_config_not_from_the_request():
    """POST 里带 days=0 就能删掉刚作废的票的话，保留期就成了摆设。
    执行侧只读服务端那一份，天数由调用方（服务端）传进来，请求体里没有这个口。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        assert purge.plan("hawb", stem, days=0, now=NOW) is not None, "days=0 时应当立刻可算（这是预览用的能力）"
        r = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        assert r[0]["ok"] is False and "保留" in r[0]["error"]
        assert (config.ARCHIVE_DIR / stem).exists(), "拒绝之外还得多手：文件一个都不许动"


# ── 二、计划要覆盖"删干净"的每一个落点 ────────────────────────────────────────────

def test_an_unreadable_void_stamp_never_counts_as_aged():
    """作废本里的时刻读不出来（有人手工改过、或将来换了时间格式），一律当"还没满期"。
    这条判据只能往"少删"一侧错：猜成"早就满了"就等于给一张没人核对过的票开了门。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, "2026/09/01 不是个 ISO 时刻")
        assert purge.plan("hawb", stem, 30, now=NOW) is None
        assert purge.candidates(days=30, now=NOW) == []


def test_the_hawb_plan_lists_every_place_that_ticket_has_data():
    with _world() as t:
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, OLD)
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
        _age_void("hawb", b, OLD)
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
        _age_void("hawb", stem, OLD)
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
        _age_void("hawb", stem, OLD)
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
        _age_void("hawb", stem, OLD)
        p = purge.plan("hawb", stem, 30, now=NOW)
        assert not any("passwd" in x for x in _paths(p))


# ── 三、执行：顺序、幂等、逐张成败 ────────────────────────────────────────────────

def test_the_tombstone_lands_before_a_single_byte_is_deleted():
    """中途崩掉时，凭据必须已经在盘上、台账条目必须还没动——
    这样那张票还在候选里，能被重新挑出来删完，而不是变成查无此票的孤儿数据。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, OLD)
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


def test_the_tombstone_keeps_the_numbers_but_never_the_ticket_content():
    with _world() as t:
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, OLD)
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        line = json.loads(config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()[-1])
        assert line["mawb"] == "999-95764373" and line["hawb"] == "JRT28645"
        assert line["kind"] == "hawb" and line["stem"] == stem and line["auto"] is False
        assert line["files"], "凭据要能回答删了哪几个文件"
        for secret in ("STEEL", "SHIPPER", "GOODS_INFO"):
            assert secret not in json.dumps(line), "凭据里混进了票面内容：" + secret


def test_purging_twice_is_a_no_op_and_does_not_raise():
    with _world() as t:
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, OLD)
        first = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        again = purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        assert first[0]["ok"] is True
        assert again[0]["ok"] is False, "第二次应当说清楚「这张票已经不在了」，不能静默报成功"
        assert len(config.PURGED_LEDGER.read_text(encoding="utf-8").splitlines()) == 1, \
            "同一张票留下两条凭据，事后没法知道到底删了几次"


def test_a_batch_reports_each_ticket_rather_than_failing_the_whole_run():
    with _world() as t:
        a = _ticket(t, "S1")
        b = _ticket(t, "S2", hawb="JRT9")
        _age_void("hawb", a, OLD)
        r = purge.purge([{"kind": "hawb", "key": "hawb|" + a},
                         {"kind": "hawb", "key": "hawb|" + b}], by="admin", days=30, now=NOW)
        assert [x["ok"] for x in r] == [True, False], "第二张没满保留期，不能跟着第一张一起被删"
        assert (config.ARCHIVE_DIR / b).exists(), "同批里没过检查的票也被删了"


def test_the_database_is_asked_to_drop_the_same_ticket():
    """配了 DB_URL 时 hawb_raw/hawb_air 里各多一份副本：不点这个名，"服务器上删干净了"
    就是假话。回归一律离线，所以这里不连库——只看那一句调用真发了、报的是这张票。"""
    with _world() as t:
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, OLD)
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
        _age_void("hawb", stem, OLD)
        server._RENDER_CACHE[stem, 1, 2.0] = b"PNG"
        purge.purge([{"kind": "hawb", "key": "hawb|" + stem}], by="admin", days=30, now=NOW)
        assert not any(stem in json.dumps(v) for v in _jsons(config.OUTPUT_RAW_DIR / "all_hawbs.json")), \
            "all_hawbs.json 里还留着这张票"
        assert not any(stem in json.dumps(v) for v in _jsons(config.OUTPUT_AIR_DIR / "all_hawbs_air.json"))
        assert not [k for k in server._RENDER_CACHE if k[0] == stem], \
            "页图缓存没清：删完还能从缓存翻出这张票的票面"


def _age_void(kind, key, at):
    """把作废戳改成指定时刻——保留期按它算，测试里不能拿"机器现在"当历史。"""
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
        _age_void("hawb", stem, OLD)
        keep = _ticket(t, "S2", hawb="JRT9")
        got = c.post("/purge", json={"days": 0, "items": [{"kind": "hawb", "key": "hawb|" + stem},
                                                          {"kind": "hawb", "key": "hawb|" + keep},
                                                          {"kind": "hawb", "key": "hawb|不存在"}]})
        r = got.json()["results"]
        assert [x["ok"] for x in r] == [True, False, False], \
            "请求里带 days=0 就把没满保留期的第二张也删了——保留期只认服务端那一份"
        assert (config.ARCHIVE_DIR / keep).exists()
        assert c.post("/purge", json={"items": [{"kind": "hawb", "key": "hawb|../../etc"}]}).json()["results"][0]["ok"] is False


def test_the_panel_endpoints_answer_in_the_shape_the_front_end_draws_from():
    with _world() as t:
        c = _client(t)
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, OLD)
        j = c.get("/purge/candidates").json()
        assert j["days"] == 30 and len(j["items"]) == 1
        assert j["total_bytes"] >= j["items"][0]["bytes"] > 0
        assert {"kind", "key", "mawb", "hawb", "void_by", "void_at", "in_ledger", "bytes"} <= set(j["items"][0])
        assert c.post("/purge/config", json={"days": 15}).json()["days"] == 15
        assert c.post("/purge/config", json={"days": 0}).status_code == 400
        assert c.get("/purge/config").json()["days"] == 15


def test_the_server_never_hands_the_browser_absolute_paths_or_delete_plans():
    """候选接口是给人看的，不是把「要删哪些路径」发给浏览器。绝对路径既没用途，
    又把服务器目录结构原样摊在一个只读页面上。"""
    with _world() as t:
        c = _client(t)
        stem = _ticket(t, "S1")
        _age_void("hawb", stem, OLD)
        raw = c.get("/purge/candidates").text
        assert str(t) not in raw, "响应里带了服务器绝对路径"
        for leak in ("files", "dirs", "drop"):
            assert '"%s"' % leak not in raw, "%s 这种执行细节不该上界面" % leak


# ── 六、前端：清理面板 ────────────────────────────────────────────────────────────

def test_the_panel_is_an_admin_entry_carrying_the_candidate_count():
    page, js = web_src.part("index.html"), web_src.part("js/desk.js")
    for i in ("btnPurge", "purgeWin", "purgeBox", "purgeDays", "purgeQuery", "purgeList"):
        assert 'id="%s"' % i in page, "面板缺控件：" + i
    assert '$("#btnPurge").hidden = !admin' in js, \
        "入口只对管理员可见（与 /purge 的服务端门禁同一口径）"
    assert "btnPurgeCnt" in js, "入口上要有候选数徽标：到期该清的得在开门时就看见"
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


def test_nothing_to_purge_says_why_and_where_to_create_a_candidate():
    js = web_src.part("js/desk.js")
    i = js.index("function renderPurge(")
    body = js[i:js.index("\nfunction ", i + 1)]
    assert "还没作废" in body or "先作废" in body, \
        "空列表要说明为什么是空的：这个功能只收已作废满保留期的票，不写清楚就会被当成系统坏了"


# ── 切片小工具：切不到就给一条干净的断言，不是一句 ValueError ──────────────────────

def _after(js, needle, span=700):
    i = js.find(needle)
    assert i >= 0, "desk.js 里找不到 %r 这一段" % needle
    return js[i:i + span]


def _jsons(p):
    return [json.loads(p.read_text(encoding="utf-8"))] if p.exists() else []
