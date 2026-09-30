# -*- coding: utf-8 -*-
"""进程内解析队列的回归：限并发、排队位次、失败不带走 worker、塞满就拒。纯本地，不碰模型。"""
import threading
import time

from desk_queue import DONE, FAILED, QUEUED, QueueFull, DeskQueue


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
