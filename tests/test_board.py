# -*- coding: utf-8 -*-
"""规则留言板：所有人能提、只有管理员能定性、落成仍在代码里（设计见 docs/留言板-design.md）。

这本台账唯一值钱的东西是「谁在什么时候说了什么、后来怎么定的」，所以用例几乎全在钉同一件事：
**不销毁**（改判留两次的话、撤回单列一档不混进待办、删要管理员且只在误发时用）与
**不撒谎**（署名只认会话、计数与筛选同一个判据、开关没开就当没这接口）。
"""
import contextlib
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import board
import config
import server
import web_src

EMPTY = {"all": 0, "open": 0, "accepted": 0, "rejected": 0, "parked": 0, "gone": 0, "mine": 0}


@contextlib.contextmanager
def _world():
    """留言板只碰这一本文件；夹具连 purge 的落点一起指开，好让"清理不碰它"那条能真跑一遍。

    `PREVIEW_DIR` 与 `INPUT_DIR` 也要指开：那一条用例现在会摆一张真到期的票进去删，
    少指一个就往真 `output/` 里删文件了（回归末尾那份"这台机器的 output 清单"会当场抓住，
    但那是事后——指开才是事前）。"""
    old = (config.BOARD_FILE, config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR,
           config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR, config.SUBMIT_LEDGER, config.ARCHIVE_DIR,
           config.VOID_LEDGER, config.MASTER_DIR, config.MASTER_LEDGER, config.PURGED_LEDGER,
           config.PURGE_RUN_STATE, config.PREVIEW_DIR, config.INPUT_DIR,
           auth.USERS_FILE, config.BOARD)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for name in ("archive", "transcript", "raw", "air", "qc", "preview", "staged",
                     "submit_guard", "master", "mawb_source", "input"):
            (tmp / name).mkdir()
        config.BOARD_FILE = tmp / "board.json"
        config.STAGED_DIR, config.OUTPUT_AIR_DIR = tmp / "staged", tmp / "air"
        config.OUTPUT_RAW_DIR, config.OUTPUT_QC_DIR = tmp / "raw", tmp / "qc"
        config.TRANSCRIPT_DIR, config.ARCHIVE_DIR = tmp / "transcript", tmp / "archive"
        config.SUBMIT_LEDGER, config.VOID_LEDGER = tmp / "submitted.json", tmp / "voided.json"
        config.MASTER_DIR, config.MASTER_LEDGER = tmp / "master", tmp / "master_submitted.json"
        config.PURGED_LEDGER = tmp / "purged.jsonl"
        config.PURGE_RUN_STATE = tmp / "purge_last_run.json"
        config.PREVIEW_DIR, config.INPUT_DIR = tmp / "preview", tmp / "input"
        auth.USERS_FILE = tmp / "users.json"
        config.BOARD = True
        try:
            yield tmp
        finally:
            (config.BOARD_FILE, config.STAGED_DIR, config.OUTPUT_AIR_DIR, config.OUTPUT_RAW_DIR,
             config.OUTPUT_QC_DIR, config.TRANSCRIPT_DIR, config.SUBMIT_LEDGER, config.ARCHIVE_DIR,
             config.VOID_LEDGER, config.MASTER_DIR, config.MASTER_LEDGER, config.PURGED_LEDGER,
             config.PURGE_RUN_STATE, config.PREVIEW_DIR, config.INPUT_DIR,
             auth.USERS_FILE, config.BOARD) = old


def _login(name, role, pw="pw-1"):
    auth.set_password(name, pw, role)
    c = TestClient(server.app)
    r = c.post("/login", json={"name": name, "password": pw})
    assert r.status_code == 200, r.text
    return c


_add = board.add


def _note(text, **kw):
    """存储层用例都署同一个名：署名必填是真不变式（没人说的规则不能进这本台账），
    但每条用例都重写一遍 `by` 会把读的人带偏——他要看的不是署名是谁。"""
    kw.setdefault("by", "马殿齐")
    return _add(text, **kw)


# ── 一、存储层：不销毁 ────────────────────────────────────────────────────────

def test_a_new_note_starts_open_and_shows_up_in_the_counts():
    with _world():
        item = _note("巴西收货人邮编都印成 8 位不带横杠", by="郭健康", role="inputter")
        got = board.listing("郭健康")
        assert got["items"][0]["id"] == item["id"]
        assert got["items"][0]["status"] == "open", "新提的要落在「待看」那一档，别默认已处理"
        assert got["counts"] == {"all": 1, "open": 1, "accepted": 0, "rejected": 0,
                                 "parked": 0, "gone": 0, "mine": 1}, got["counts"]


def test_a_decision_requires_a_word_and_keeps_who_and_when():
    with _world():
        it = _note("SE 邮编是 3+2 带空格")
        try:
            board.decide(it["id"], "accepted", "")
            raise AssertionError("没写为什么的判定不许成立：这句话是同一件事明年不再被提一遍的唯一凭据")
        except ValueError:
            pass
        out = board.decide(it["id"], "accepted", "已进表", by="admin", ref="polish.POSTAL")
        assert out["status"] == "accepted" and out["ref"] == "polish.POSTAL", out
        assert out["decided_by"] == "admin" and out["decided_at"], out


def test_redeciding_keeps_both_words_and_the_last_one_wins():
    """先标「已收」、后来发现做错了改回去：两次的话都得留着，否则「当初为什么收」查不回。"""
    with _world():
        it = _note("MX 的州有两种写法")
        board.decide(it["id"], "accepted", "先收，等 IT 回话", by="admin")
        out = board.decide(it["id"], "parked", "IT 说这一格收全称，先不动", by="admin")
        assert out["status"] == "parked", "当前判定取最后一次"
        assert [d["status"] for d in out["decisions"]] == ["accepted", "parked"], out
        assert out["decisions"][0]["note"] == "先收，等 IT 回话", "上一次的话被覆盖了"
        raw = json.loads(config.BOARD_FILE.read_text(encoding="utf-8"))["items"][0]
        assert "status" not in raw and "decided_by" not in raw, \
            "当前判定只由服务器从 decisions 派生；写进文件就是存两份，迟早对不上"


def test_retract_leaves_the_line_in_its_own_bucket_and_blocks_edits():
    with _world():
        it = _note("这条发错地方了")
        out = board.retract(it["id"], by="郭健康")
        assert out["retracted"]["by"] == "郭健康" and out["retracted"]["at"], out
        assert board.listing()["items"], "撤回不是删：行要还在，只是不进待办"
        for label, fn in (("改文本", lambda: board.revise(it["id"], "换一句话", by="郭健康")),
                          ("改判", lambda: board.decide(it["id"], "rejected", "不采纳", by="admin"))):
            try:
                fn()
                raise AssertionError(label + "还能作用在已撤回的条目上，等于撤回没发生过")
            except ValueError:
                pass
        again = board.retract(it["id"], by="郭健康")
        assert again["retracted"]["at"] == out["retracted"]["at"], "重复撤回要幂等：不许覆盖成第二次"


def test_retracted_lines_are_out_of_every_count_and_in_their_own_filter():
    """计数说的是「要你处理的有几条」，把撤回的算进去就永远清不到零；
    但它们得还能翻出来（发错了什么也是信息），所以自成「已撤回」那一档，而不是混在列表里。"""
    with _world():
        _note("要处理", by="马殿齐")
        gone = _note("发错了", by="马殿齐")
        board.retract(gone["id"], by="马殿齐")
        got = board.listing("马殿齐")
        assert got["counts"] == {"all": 1, "open": 1, "accepted": 0, "rejected": 0,
                                 "parked": 0, "gone": 1, "mine": 1}, got["counts"]
        assert len(got["items"]) == 2, "一次取全表，筛是前端的事；两处的判据必须是同一个（logic 里那三个函数）"


def test_an_empty_or_overlong_note_is_rejected():
    with _world():
        for bad in ("", "   ", "\n\t"):
            try:
                _note(bad)
                raise AssertionError("空白正文的留言没有意义，不许收")
            except ValueError:
                pass
        try:
            _note("字" * 2001)
            raise AssertionError("长度上限要挡住一条长文把这本台账撑爆")
        except ValueError:
            pass
        assert len(_note("字" * 2000)["text"]) == 2000, "2000 以内要收得下"


def test_the_column_tag_only_accepts_a_real_column_name():
    """列名挡，票号不挡：列名写错会带歪规则，而「当时挂的是哪张票」是历史——
    票哪天被清掉都不该反过来让这条留言存不下。"""
    with _world():
        assert _note("邮编要带横杠", tags={"column": "CONSIGNEE_INFO_POSTAL"})["tags"]["column"]
        for bad in ("CONSIGNEE_INFO_ZIP", "随便写的列名", "consignee_info_postal"):
            try:
                _note("x", tags={"column": bad})
                raise AssertionError("列名「" + bad + "」不在列面里，不该收下")
            except ValueError:
                pass
        it = _note("这张票收货人邮编票面就缺", ticket={"stem": "已被清掉的票", "mawb": "",
                                                          "hawb": "X1"})
        assert it["ticket"]["stem"] == "已被清掉的票"
        assert it["ticket"]["hawb"] == "X1"


def test_a_master_only_column_is_still_a_real_column():
    """通知人那几列只在主单列面里。留言板收的是"这一格该怎么填"，两条链都要能挂上名。"""
    with _world():
        assert _note("通知人税号也进 NOTIFY_INFO_EORI")["tags"] == {}
        it = _note("通知人邮编", tags={"column": "NOTIFY_INFO_POSTAL"})
        assert it["tags"]["column"] == "NOTIFY_INFO_POSTAL"


def test_ids_are_never_reused_even_after_a_hard_delete():
    with _world():
        a = _note("第一条")
        assert board.remove(a["id"]) is True
        assert board.remove(a["id"]) is False, "本来就没有要说实话：别把「没删成」报成「删过了」"
        b = _note("第二条")
        assert b["id"] != a["id"], "删过的号不能给新留言：日志与「复制这条」里的 board#id 会指错"


def test_two_threads_do_not_lose_each_other():
    import threading
    with _world():
        errs = []

        def go(i):
            try:
                _note("并发第 " + str(i) + " 条")
            except Exception as e:          # noqa: BLE001  线程里抛出来看不见，先收着
                errs.append(str(e))

        ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errs, errs
        assert len(board.listing()["items"]) == 6, "一本台账一把锁：读改写不能交叉"


def test_a_half_written_board_file_reads_as_empty_instead_of_crashing():
    with _world():
        config.BOARD_FILE.write_text('{"seq": 3, "items": [{"id": 1"', encoding="utf-8")
        assert board.listing() == {"items": [], "counts": dict(EMPTY), "me": ""}, \
            "读不出就当没有：界面要还能开，服务不能因为一本文件坏了整个起不来"
        _note("还能写")
        assert len(board.listing()["items"]) == 1


def test_revising_keeps_the_old_words_not_just_a_mark_that_something_changed():
    """只存一条"改过"没有意义：这本台账要的正是"他当时提的其实是另一件事"。
    光断言 `revised` 非空会被一个空壳骗过去（v1.33.0 变异 M24 就是这么露出来的）。"""
    with _world():
        it = _note("巴西邮编印成 8 位", by="郭健康")
        out = board.revise(it["id"], "补一句：公司那边要带横杠", by="郭健康")
        assert out["text"] == "补一句：公司那边要带横杠", out
        assert [v["text"] for v in out["revised"]] == ["巴西邮编印成 8 位"], "旧正文没留下来=覆盖了"
        assert out["revised"][0]["by"] == "郭健康" and out["revised"][0]["at"], out
        again = board.revise(it["id"], "第三次说", by="郭健康")
        assert len(again["revised"]) == 2, "改两次只留一条：中间那句话没了"


def test_the_purge_never_touches_the_board_file():
    """自动清理的射程是票的落点。留言板混进那个集合就会"像正常工作一样"被删光，
    而那是唯一记着「谁跟我们说过什么」的一本。

    这一条**要真有一张到期的票被删掉**再验：名册空着的时候 `--auto` 什么都不动，
    那种"通过"连"清理跑过一遍"都没证明。（v1.33.0 第一轮变异就是被这条蒙过去的：
    把 `board.json` 塞进删除清单，用例照样绿——因为那一台世界里根本没有票可删。）"""
    import datetime
    import purge
    import run_purge
    with _world():
        it = _note("BR 的 CPF 也要按 11 位分三段")
        old = config.BOARD_FILE.read_text(encoding="utf-8")
        stem = "board-guard-ticket"
        quiet = (datetime.datetime.now() - datetime.timedelta(days=400)).isoformat(timespec="seconds")
        (config.OUTPUT_QC_DIR / (stem + ".json")).write_text(
            json.dumps({"flags": [], "source_name": stem + ".pdf", "processed_at": quiet}),
            encoding="utf-8")
        (config.OUTPUT_AIR_DIR / (stem + ".json")).write_text('{"HAWB_NO":"JRT9"}', encoding="utf-8")
        arch = config.ARCHIVE_DIR / stem
        arch.mkdir()
        # 安静时间取三处最晚，归档目录的 mtime 是其中一处：夹具刚建出来的目录是"此刻"，
        # 不倒回去这张票就永远算"刚被碰过"，名册就是空的（上面那句 assert 就是抓这个的）。
        ts = datetime.datetime.fromisoformat(quiet).timestamp()
        os.utime(arch, (ts, ts))
        assert purge.candidates(), "夹具没摆出一张到期票：那这条守卫什么都没验"
        assert run_purge.main(["--auto"]) == 0
        assert not (config.OUTPUT_QC_DIR / (stem + ".json")).exists(), \
            "这一跑连那张到期的票都没删：下面那句「留言板没被碰到」就是白说的"
        assert config.BOARD_FILE.exists(), "留言板被清理射程扫到了"
        assert config.BOARD_FILE.read_text(encoding="utf-8") == old
        assert board.listing()["items"][0]["id"] == it["id"]


# ── 二、门禁与开关：HTTP 行为 ─────────────────────────────────────────────────

def test_every_route_needs_a_login():
    with _world():
        anon = TestClient(server.app)
        assert anon.get("/board").status_code == 401
        assert anon.post("/board", json={"text": "x"}).status_code == 401


def test_all_three_roles_can_post_and_the_signature_comes_from_the_session():
    with _world():
        for name, role in (("admin", "admin"), ("宛平", "reviewer"), ("郭健康", "inputter")):
            r = _login(name, role).post("/board", json={"text": name + "提的",
                                                        "by": "冒充", "role": "admin"})
            assert r.status_code == 200, r.text
        items = board.listing()["items"]
        assert sorted(i["by"] for i in items) == sorted(["admin", "宛平", "郭健康"]), items
        assert [i["role"] for i in items if i["by"] == "郭健康"] == ["inputter"], \
            "请求里带 by/role 改不动留痕：能改署名就等于能伪造是谁提的规则"


def test_editing_someone_elses_note_is_forbidden_even_for_an_admin():
    """管理员能定性、能删，但不能替人改正文——改了就说不清是谁的话。"""
    with _world():
        c = _login("郭健康", "inputter")
        it = c.post("/board", json={"text": "原始那句"}).json()["item"]
        adm = _login("admin", "admin")
        assert adm.patch("/board/" + str(it["id"]), json={"text": "管理员改了"}).status_code == 403
        mine = c.patch("/board/" + str(it["id"]), json={"text": "我补一句"})
        assert mine.status_code == 200 and mine.json()["item"]["revised"], mine.text
        assert c.post("/board/" + str(it["id"]) + "/retract").status_code == 200
        other = _login("宛平", "reviewer")
        assert other.post("/board/" + str(it["id"]) + "/retract").status_code == 403, \
            "撤回是本人的动作，管理员也不能替他撤"


def test_only_an_admin_can_decide_and_a_reviewer_sees_why():
    with _world():
        c = _login("宛平", "reviewer")
        it = c.post("/board", json={"text": "KR 邮编新旧两制"}).json()["item"]
        r = c.patch("/board/" + str(it["id"]), json={"status": "rejected", "note": "我自己定"})
        assert r.status_code == 403
        assert "管理员" in r.json()["detail"], "要说清为什么不行，别只甩一个权限不足"
        adm = _login("admin", "admin")
        assert adm.patch("/board/" + str(it["id"]),
                         json={"status": "rejected", "note": ""}).status_code == 400
        ok = adm.patch("/board/" + str(it["id"]), json={"status": "rejected",
                                                        "note": "新旧并存，补横杠会改错"})
        assert ok.status_code == 200 and ok.json()["item"]["status"] == "rejected", ok.text
        assert c.get("/board").json()["items"][0]["decided_by"] == "admin"


def test_a_bad_status_value_is_a_400_not_a_silent_open():
    with _world():
        adm = _login("admin", "admin")
        it = adm.post("/board", json={"text": "试试状态写错"}).json()["item"]
        assert adm.patch("/board/" + str(it["id"]),
                         json={"status": "done", "note": "收"}).status_code == 400
        r = adm.get("/board/" + str(it["id"]))
        assert r.status_code in (404, 405), "没有单条读口，别开第二份真相"


def test_only_an_admin_can_hard_delete():
    with _world():
        c = _login("宛平", "reviewer")
        it = c.post("/board", json={"text": "把客户邮箱写进来了"}).json()["item"]
        r = c.delete("/board/" + str(it["id"]))
        assert r.status_code == 403
        assert "不采纳" in r.json()["detail"], \
            "硬删挡下时要说清该走哪一档：回一句「判定由管理员来定」是答非所问（隔离浏览器里量到的）"
        assert _login("admin", "admin").delete("/board/" + str(it["id"])).status_code == 200
        assert board.listing()["items"] == []


def test_switched_off_means_the_feature_does_not_exist():
    """VM 那台「不做」要做得像没这功能：一律 404，而不是 403——「没权限」会让人以为有，去磨入口。"""
    with _world():
        c = _login("admin", "admin")
        config.BOARD = False
        calls = [lambda: c.get("/board"), lambda: c.post("/board", json={"text": "x"}),
                 lambda: c.patch("/board/1", json={"text": "x"}),
                 lambda: c.post("/board/1/retract"), lambda: c.delete("/board/1")]
        for call in calls:
            assert call().status_code == 404, "开关没开还答 401/403，等于告诉别人这功能只是藏着"
        assert c.get("/health").json()["board"] is False, "/health 要报得出这台开没开"
        config.BOARD = True
        assert c.get("/health").json()["board"] is True
        assert c.get("/board").status_code == 200


def test_the_listing_gives_counts_so_the_front_end_never_recounts():
    with _world():
        adm = _login("admin", "admin")
        it = adm.post("/board", json={"text": "一条", "tags": {"country": "BR"}}).json()["item"]
        adm.patch("/board/" + str(it["id"]), json={"status": "accepted", "note": "进了",
                                                   "ref": "polish.POSTAL"})
        j = adm.get("/board").json()
        assert j["counts"] == {"all": 1, "open": 0, "accepted": 1, "rejected": 0,
                               "parked": 0, "gone": 0, "mine": 1}, j["counts"]
        assert j["me"] == "admin", "「我发的」是谁，要由服务端会话说了算"


# ── 三、界面：接线与不重算 ────────────────────────────────────────────────────

def _logic(expr, **consts):
    return web_src.logic(expr, **consts)


def test_the_board_entry_is_visible_to_every_role():
    """入口一旦被 applyRole 藏起来，两个角色就永远不知道有这条路——而手上长着规则的正是他们。"""
    import re
    js = web_src.desk()
    assert 'id="btnBoard"' in web_src.part("index.html"), "顶栏没有那颗入口"
    role = re.search(r"function applyRole\(\)(.*?)\n}", js, re.S)
    assert role and "btnBoard" not in role.group(1), \
        "入口被 applyRole 藏了：非管理员看不见留言板，采集通道就只剩管理员自己"


def test_the_board_popover_uses_the_gate_frame_and_lets_esc_out():
    """Esc 那份清单从 DOM 现捞（多一张窗口自动管上），但**焦点归还**要 GATE_BACK 里有它，
    否则关掉之后下一次 Tab 得从页面顶上重走。"""
    import re
    page, js = web_src.part("index.html"), web_src.desk()
    assert re.search(r'<div class="gate" id="boardWin"', page), "留言板浮层没走 .gate 骨架"
    assert 'boardWin: "#btnBoard"' in js, "关掉浮层后焦点没还给入口那颗按钮"
    assert 'openGate("boardWin")' in js, "入口没走 openGate：它会与设置那层叠着开"


def test_the_status_labels_are_plain_words():
    assert _logic("L.boardLabel('accepted')") == "已收"
    assert _logic("L.boardLabel('parked')") == "先放一放"
    assert _logic("L.boardLabel('gone')") == "已撤回"
    assert _logic("L.boardLabel('nope')") == "待看", "认不出的值落回待看，不许显示 undefined"


def test_the_filter_and_the_counts_share_one_rule():
    """计数与筛选各写一遍就会出现「待看 1」旁边列 2 行——这种不一致比数字错更难被发现。"""
    rows = [{"id": 1, "status": "open", "retracted": None, "by": "a"},
            {"id": 2, "status": "open", "retracted": {"at": "x", "by": "a"}, "by": "a"},
            {"id": 3, "status": "accepted", "retracted": None, "by": "b"},
            {"id": 4, "status": "parked", "retracted": None, "by": "a"}]
    assert _logic("L.boardSeg(rows[1])", rows=rows) == "gone", "撤回的行自成一档，不跟着原来的状态走"
    assert _logic("L.boardCounts(rows, 'a')", rows=rows) == {"all": 3, "open": 1, "accepted": 1,
                                                             "rejected": 0, "parked": 1,
                                                             "gone": 1, "mine": 2}, "计数不许算上已撤回"
    for f, n in (("all", 3), ("open", 1), ("parked", 1), ("mine", 2), ("gone", 1), ("nope", 4)):
        assert _logic("L.boardFilter(rows, f, v).length", rows=rows, f=f, v="a") == n, \
            "%s 档筛出的行数与计数对不上" % f
    segs = _logic("[4,1,3,2].map(i => L.boardSeg(rows[i-1]))", rows=rows)
    assert sum(1 for s in segs if s != "gone") == _logic("L.boardCounts(rows,'a').all", rows=rows), \
        "分档不穷尽：有行既不在任何一档、也不算已撤回"


def test_the_render_reads_the_servers_counts_and_the_copy_line_carries_history():
    import re
    js = web_src.desk()
    body = web_src.fn(js, "function renderBoard")
    assert "B.counts" in body, "计数要取服务器给的那份，别在渲染里自己数"
    assert not re.search(r"\.filter\([^)]*\)\.length", body), "渲染里重算计数：两份迟早不一致"
    assert "esc(" in body, "正文与人名都是同事打的字，直接进 innerHTML 就是自我 XSS"
    line = web_src.fn(js, "function boardCopyText")
    assert "decisions" in line, "复制这条要带上历次判定的话，否则拿回去的是一半信息"
    assert "esc(" not in line, "那是剪贴板文本不是 HTML，转义会把 &quot; 原样带进正文"


def test_the_request_body_cannot_smuggle_extra_keys_into_the_ledger():
    """前端传什么都不认，也不许顺手多存一个键：`by`/`role` 混进 tags 或 ticket 里就想落盘，
    这本台账从此说不清是谁提的。三个键之外的一律丢掉，`_write` 只写白名单里的那几个。"""
    with _world():
        c = _login("郭健康", "inputter")
        r = c.post("/board", json={"text": " smuggle", "tags": {"country": "BR", "by": "冒充",
                                                                 "role": "admin", "status": "accepted"},
                                                     "ticket": {"stem": "HAWB-X", "by": "冒充",
                                                                "decisions": [{"status": "accepted"}]}})
        assert r.status_code == 200, r.text
        it = r.json()["item"]
        assert it["by"] == "郭健康" and it["role"] == "inputter", "署名与角色只认会话"
        assert it["tags"] == {"country": "BR"}, it
        assert it["ticket"] == {"stem": "HAWB-X"}, it
        raw = json.loads(config.BOARD_FILE.read_text(encoding="utf-8"))["items"][0]
        assert raw["tags"] == {"country": "BR"} and raw["ticket"] == {"stem": "HAWB-X"}, \
            "白名单外的键落进了文件：这本台账里长出了没人管的东西"


def test_esc_out_resets_the_entry_state():
    """`closeTopGate` 是直接把窗口 `hidden` 一设就走的，不走 `boardClose`：
    入口那颗的 `aria-expanded` 会留在 true——读屏器从此一直报"这个按钮是展开的"。
    （隔离浏览器里量到的：Esc 之后焦点回到 #btnBoard 是对的，expanded 却还是 true。）"""
    js = web_src.fn(web_src.desk(), "function closeTopGate")
    assert 'if (top.id === "boardWin") $("#btnBoard").setAttribute("aria-expanded", "false");' in js, \
        "关掉那一层没顺手把入口那颗的 aria-expanded 改回来"


def test_the_column_dropdown_is_the_same_list_the_server_whitelists():
    """下拉那份必须与 `POST` 挡人那份是同一个函数算出来的。抄两份的结果一定是
    "界面选得出来、提交报 400"（或反过来：明明能挂上名却选不出来），而那最难查。"""
    with _world():
        j = _login("郭健康", "inputter").get("/board").json()
        keys = [k for k, _lab in j["columns"]]
        assert len(keys) == len(set(keys)), "同一列在下拉里出现两次"
        assert set(keys) == board.columns_allowed(), "下拉与白名单不是同一份了"
        lab = dict(j["columns"])["CONSIGNEE_INFO_POSTAL"]
        assert lab and lab != "CONSIGNEE_INFO_POSTAL", "给同事看的名单要用中文名，别让他认列名缩写"


def test_the_entry_follows_the_switch_on_this_machine():
    """开关没开的那台连入口都不给——点了才吃一句 404 等于把人领到一个不存在的功能前。
    判据取 /health 的 board（每台自己知道自己开没开），不按机器名/端口猜那台是谁。"""
    js = web_src.desk()
    assert "boardVisible(r.board === true)" in js, "入口没跟着 /health 的 board 走"
    vis = web_src.fn(js, "function boardVisible")
    assert '$("#btnBoard").hidden = !on' in vis, "入口的显隐只有一处判据"
    badge = web_src.fn(js, "function boardVisible")
    assert "boardBadge()" in badge, "关掉这台留言板时徽标要跟着清：留着就是上一台的数"


def test_a_retracted_note_is_still_reachable_from_the_panel():
    """撤回不销毁要说得出口：灰行得还在列表里翻得到，所以「已撤回」自成一档。
    只在「全部」里混着画不行——那样计数（3）与列表（4 行）两个数摆在一起，
    而这一版的全部口径都靠"数字与看到的行数一致"才可信。"""
    rows = [{"id": 1, "status": "open", "retracted": None, "by": "a"},
            {"id": 2, "status": "open", "retracted": {"at": "x", "by": "a"}, "by": "a"}]
    assert _logic("L.boardFilter(rows,'all','a').length", rows=rows) == 1
    assert _logic("L.boardFilter(rows,'gone','a').length", rows=rows) == 1, "已撤回那档要筛得出来"
    chips = web_src.fn(web_src.desk(), "function boardSegBtns")
    assert '"gone", "已撤回"' in chips, "面板上没有「已撤回」那颗，灰行就翻不到了"
