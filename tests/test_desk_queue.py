# -*- coding: utf-8 -*-
"""进程内解析队列的回归：限并发、排队位次、失败不带走 worker、塞满就拒。纯本地，不碰模型。"""
import threading
import time

from desk_queue import CANCELLED, DONE, FAILED, QUEUED, RUNNING, QueueFull, DeskQueue


def test_only_two_parsing_and_the_rest_see_their_place_in_line():
    """一张票的最坏路径是 LibreOffice 子进程 + 全页渲染 + 模型调用。
    同时开几张不能由"几个人同时点上传"决定——内存闸撞满杀的是整个服务。"""
    q = DeskQueue(slots=2, max_pending=50)
    live = [0]
    peak = [0]
    lock = threading.Lock()

    def work():
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"ok": True}

    ids = [q.submit(work) for _ in range(5)]
    seen = [q.get(i) for i in ids]
    assert seen[0]["state"] in (QUEUED, "running")
    assert any(j["position"] >= 1 for j in seen), f"排队的要给出位次：{[j['position'] for j in seen]}"
    for _ in range(200):
        if all(q.get(i)["state"] in (DONE, FAILED) for i in ids):
            break
        time.sleep(0.02)
    assert peak[0] <= 2, f"同时最多 2 张在解析，实际峰值 {peak[0]}"
    assert [q.get(i)["state"] for i in ids] == [DONE] * 5


def test_a_failing_ticket_reports_and_does_not_kill_the_worker():
    """解析失败是任务结果。异常要是能把 worker 带走，队列会悄悄越跑越少，
    最后几张票永远停在"排队中"（审核台最恶心的一类卡死）。"""
    q = DeskQueue(slots=1, max_pending=10)
    bad = q.submit(lambda: (_ for _ in ()).throw(RuntimeError("模型无响应")))
    good = q.submit(lambda: {"ok": True})
    for _ in range(200):
        if q.get(bad)["state"] in (DONE, FAILED) and q.get(good)["state"] == DONE:
            break
        time.sleep(0.02)
    assert q.get(bad)["state"] == FAILED
    assert "模型无响应" in q.get(bad)["error"]
    assert q.get(good)["result"] == {"ok": True}, "坏票之后排队的票要照常跑"


def test_a_stuffed_queue_refuses_instead_of_piling_up():
    """已经排到上限说明后端吃不消了，这时候接进来只会让每个人都超时——直接拒，说清原因。"""
    q = DeskQueue(slots=1, max_pending=2)
    gate = threading.Event()
    ids = [q.submit(lambda: gate.wait(2)) for _ in range(2)]
    assert all(i for i in ids)
    try:
        q.submit(lambda: None)
        raise AssertionError("排满了该拒绝")
    except QueueFull as e:
        assert "排队" in str(e)
    gate.set()


def test_a_queued_ticket_can_be_cancelled_and_never_runs():
    """排队中的那张要能摘掉：人传错了文件名、或者一次塞了十张又想先做别的，
    不该只能干等它排到。摘掉的是**还没开始**的那一半——cleanup 负责把临时目录交还，
    否则每掐一张就漏一个目录在盘上。"""
    q = DeskQueue(slots=1, max_pending=10)
    gate = threading.Event()
    first = q.submit(lambda: (gate.wait(2), {"ok": True})[1])
    ran, cleaned = [], []
    doomed = q.submit(lambda: ran.append(1), cleanup=lambda: cleaned.append(1))
    assert q.get(doomed)["state"] == QUEUED, "第二张该还在排队"
    assert q.cancel(doomed) == "cancelled"
    gate.set()
    for _ in range(200):
        if q.get(first)["state"] == DONE and q.get(doomed)["state"] == CANCELLED:
            break
        time.sleep(0.02)
    assert q.get(doomed)["state"] == CANCELLED
    assert ran == [], "掐掉的排队作业不许照样跑一遍模型"
    assert cleaned == [1], "跳过的作业要把自己的临时目录交还"
    assert q.stats()["waiting"] == 0, f"位次表里不该还挂着它：{q.stats()}"


def test_a_running_job_cannot_be_interrupted_and_says_so():
    """已经在跑的那一张掐不掉：模型调用与 LibreOffice 子进程都在别人手里，
    硬杀子进程会把解析到一半的票留在盘上。所以只回一句"在跑"，让界面说实话。"""
    q = DeskQueue(slots=1, max_pending=5)
    gate = threading.Event()
    jid = q.submit(lambda: (gate.wait(2), {"ok": True})[1])
    for _ in range(200):
        if q.get(jid)["state"] == RUNNING:
            break
        time.sleep(0.02)
    assert q.cancel(jid) == "running"
    gate.set()
    for _ in range(200):
        if q.get(jid)["state"] == DONE:
            break
        time.sleep(0.02)
    assert q.get(jid)["state"] == DONE, "说掐不掉的就得照样跑完，别偷偷丢了结果"


def test_cancelling_something_that_is_already_gone_reports_gone():
    q = DeskQueue(slots=1, max_pending=5)
    done = q.submit(lambda: {"ok": True})
    for _ in range(200):
        if q.get(done)["state"] == DONE:
            break
        time.sleep(0.02)
    assert q.cancel(done) == "gone", "已完结的作业不是「能取消」，也不该假装成功"
    assert q.cancel("ffffffffffffffff") == "gone", "没这个作业就说没有"


def test_a_cancelled_job_is_forgotten_like_any_other_finished_one():
    """摘掉的那张也是"已完结"：`_prune` 的清单里少一个档，跑一天就攒一堆没人等的作业
    （每条还带着自己的 cleanup 闭包）。这条与 DONE/FAILED 同一条规则，不该是特例。"""
    q = DeskQueue(slots=1, max_pending=10)
    gate = threading.Event()
    q.submit(lambda: gate.wait(5), who="宛平", name="block.pdf")   # 先占住唯一的 worker
    jid = q.submit(lambda: {"ok": True}, who="宛平", name="x.pdf")
    assert q.cancel(jid) == "cancelled", "这张得先真的停在队列里，否则测的是另一条分支"
    q._jobs[jid]["at"] = time.time() - 99999          # 假装已经过了 KEEP_SECONDS
    q.submit(lambda: {"ok": True})                    # prune 只在 submit 里顺带做
    gate.set()
    assert q.get(jid) is None, "已取消的作业要跟着一起清出去"
