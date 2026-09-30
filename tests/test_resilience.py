# -*- coding: utf-8 -*-
"""P1-① 超时与退避：慢模型不能再一拖 90 分钟，j9 抖动不能一次就失败、业务错又不能瞎重试。
全部打桩，不出网、不真睡。
"""
import io
import json
import tempfile
import time
import urllib.error
from pathlib import Path

import company_api
import config
import retry


class _Sleeps:
    """把 time.sleep 换成记录器，顺便让退避不真花时间。"""

    def __init__(self):
        self.secs = []
        self.real_sleep, self.real_mono = retry.time.sleep, retry.time.monotonic
        self._t = 1000.0

    def __enter__(self):
        retry.time.sleep = lambda s: self.secs.append(s)
        company_api.time.sleep = lambda s: self.secs.append(s)
        retry.time.monotonic = lambda: self._t
        company_api.time.monotonic = lambda: self._t
        return self

    def __exit__(self, *exc):
        retry.time.sleep = self.real_sleep
        company_api.time.sleep = self.real_sleep
        retry.time.monotonic = self.real_mono
        company_api.time.monotonic = self.real_mono
        return False


def test_backoff_grows_instead_of_flat_three_seconds():
    """原来三次重试都是 sleep(3)：服务在恢复中的那几秒里被反复撞，既不错峰也不退让。"""
    d = [retry.delay(i) for i in range(5)]
    assert retry.delay(0) < retry.delay(1) < retry.delay(2), d
    assert d[0] >= 1.0 and d[2] >= 8.0, d
    assert all(14.0 <= x <= 26.0 for x in d[3:]), f"再往后封顶在最后一档，不能滚成十分钟：{d}"


def test_backoff_jitters_so_parallel_tickets_do_not_resync():
    """同秒重试会撞成一串（8 个人同时点提交最明显），退避必须带抖动。"""
    seen = {round(retry.delay(1), 4) for _ in range(25)}
    assert len(seen) > 5, f"退避是固定的 {seen}，没有抖动"


def test_a_single_vlm_stage_is_bounded_and_timeouts_are_not_retried_three_times():
    """超时 600s × 重试 3 次 = 单档 30 分钟，叠 L1+L2 就是审计里那句"最坏静默等 90 分钟"。
    现在：单次超时 300s（慢模型实测 90-260s 留余量），而**超时这种失败最多试 2 次**——
    慢模型重跑一遍还是五分钟，第三遍只是把人钉在等待上。
    nginx 的 900s 要等 /extract 改成队列之后再收紧，这里只保证单档最坏 < 12 分钟。"""
    worst = config.VLM_TIMEOUT * config.VLM_TIMEOUT_TRIES + sum(
        retry.delay(i) for i in range(config.VLM_TIMEOUT_TRIES - 1))
    assert config.VLM_TIMEOUT <= 300, f"单档超时没收紧：{config.VLM_TIMEOUT}"
    assert config.VLM_TIMEOUT_TRIES <= 2, f"超时不该连撞三次：{config.VLM_TIMEOUT_TRIES}"
    assert worst < 700, f"单档最坏 {worst:.0f}s，还是太久"


class _FakeStdin(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError("http://j9.test", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


def _stub_urlopen(errors, ok_body=None, calls=None):
    """按顺序抛 errors（None 表示成功返回 ok_body），记录每次调用。"""
    calls = calls if calls is not None else []
    seq = list(errors)

    def fake(req, timeout=None):
        calls.append(timeout)
        item = seq.pop(0) if seq else None
        if isinstance(item, Exception):
            raise item
        return _FakeStdin(json.dumps(item if item else ok_body).encode())

    company_api.urllib.request.urlopen = fake
    return calls


def test_pdf_pages_do_not_all_sit_in_memory_at_once():
    """原来一页一张 PIL 图先攒成 list，再整批 base64：一张 6 页扫描票能同时握住
    pixmap + PIL + base64 三份，撞的是整服务的内存上限（MemoryMax 一到是 SIGKILL，不只杀这张票）。"""
    import gc

    import vlm_extract

    live = [0]

    class _FakeImg:
        def __init__(self):
            live[0] += 1

        def __del__(self):
            live[0] -= 1

    peak = []

    def fake_encode(img):
        peak.append(live[0])       # 编码这一页时，内存里同时活着几页
        return "data:image/png;base64,AA"

    real_from, real_enc = vlm_extract.Image.frombytes, vlm_extract._encode
    vlm_extract.Image.frombytes = lambda mode, size, samples: _FakeImg()
    vlm_extract._encode = fake_encode
    try:
        import pymupdf
        tmp = Path(tempfile.mkdtemp(prefix="hawb_pages_")) / "p.pdf"
        doc = pymupdf.open()
        for _ in range(4):
            doc.new_page(width=200, height=300)
        doc.save(str(tmp))
        doc.close()
        urls = vlm_extract.img_to_data_urls(tmp)
    finally:
        vlm_extract.Image.frombytes, vlm_extract._encode = real_from, real_enc
        gc.collect()
    assert len(urls) == 4, f"4 页要出 4 张图：{len(urls)}"
    assert max(peak) == 1, f"内存里同一时刻最多该有 1 页：峰值 {max(peak)}，逐页释放没做到"


def test_soffice_conversions_are_serialized():
    """LibreOffice 一个子进程约 300MB。并发上限不该由"同时上传了几张电子单"决定——
    内存闸撞满时 systemd 杀的是整个服务，不是那一张票。"""
    import threading

    import xlsx2pdf

    live = [0]
    peak = [0]
    lock = threading.Lock()

    class _Proc:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        src, out_dir = Path(cmd[-1]), Path(cmd[-2])
        (out_dir / (src.stem + ".pdf")).write_bytes(b"%PDF-1.4\n")   # soffice 的产物
        return _Proc()

    real_run, real_find = xlsx2pdf.subprocess.run, xlsx2pdf.find_soffice
    xlsx2pdf.subprocess.run = fake_run
    xlsx2pdf.find_soffice = lambda: "/usr/bin/soffice"
    tmp = Path(tempfile.mkdtemp(prefix="hawb_lo_"))
    try:
        (tmp / "out").mkdir()
        srcs = []
        for i in range(3):
            p = tmp / f"t{i}.xlsx"
            p.write_bytes(b"PK")
            srcs.append(p)
        ts = [threading.Thread(target=xlsx2pdf.convert_excel_to_pdf, args=(p,),
                               kwargs={"out_dir": tmp / "out"}) for p in srcs]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        xlsx2pdf.subprocess.run, xlsx2pdf.find_soffice = real_run, real_find
    assert peak[0] == 1, f"soffice 同一时刻最多该跑 1 个：峰值 {peak[0]}"


def test_j9_retries_a_429_then_succeeds():
    """j9 文档写了限流 10 次/秒，我们却零重试：撞上 429 直接变成"回传失败"让人再点一次。"""
    real = company_api.urllib.request.urlopen
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY = "live", "http://j9.test", "kh"
    ok = {"code": 0, "data": [], "success": True, "action": "insert"}
    try:
        with _Sleeps() as s:
            company_api._RATE["hits"] = []
            calls = _stub_urlopen([_http_error(429, {"detail": "too many requests"}),
                                   _http_error(503, {"detail": "busy"}), ok])
            out = company_api._j9_post("/api/v1/j9/hawb2", {"HAWB_RECORD": {}}, "kh")
        assert out["code"] == 0 and len(calls) == 3, "429/5xx 该退避重试"
        assert calls[0] == config.J9_TIMEOUT_WRITE, f"写接口超时该单列：{calls[0]}"
        assert len(s.secs) == 2 and s.secs[0] < s.secs[1], f"两次重试要退避递增：{s.secs}"
    finally:
        company_api.urllib.request.urlopen = real
        (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY) = old


def test_j9_does_not_retry_a_business_error():
    """400 是我们报文自己的问题（限长、单号格式），重试只会再撞三次并把人等更久。"""
    real = company_api.urllib.request.urlopen
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY = "live", "http://j9.test", "kh"
    try:
        with _Sleeps() as s:
            company_api._RATE["hits"] = []
            calls = _stub_urlopen([_http_error(400, {"detail": "SHIPPER_INFO 超长"})])
            try:
                company_api._j9_post("/api/v1/j9/hawb2", {"HAWB_RECORD": {}}, "kh")
                raise AssertionError("400 该抛错")
            except company_api.CompanyApiError as e:
                assert "超长" in str(e), "要把公司的原话带出来，不然人不知道该改哪"
        assert len(calls) == 1 and not s.secs, f"业务错不该重试：calls={len(calls)} sleeps={s.secs}"
    finally:
        company_api.urllib.request.urlopen = real
        (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY) = old


def test_j9_rate_limiter_waits_before_exceeding_the_documented_rate():
    """文档每把 key 10 次/秒，留两成余量按 8/s 走：一秒内点第 9 次要先等，而不是让公司回 429。"""
    real = company_api.urllib.request.urlopen
    old = (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY)
    config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY = "live", "http://j9.test", "kh"
    ok = {"code": 0, "data": []}
    try:
        with _Sleeps() as s:
            company_api._RATE["hits"] = []
            calls = _stub_urlopen([ok] * 12)
            for _ in range(int(config.J9_RATE_PER_SEC)):
                company_api._j9_post("/api/v1/j9/hawb", {"master_no": "x"}, "kh")
            assert not s.secs, "没到上限不该等"
            company_api._j9_post("/api/v1/j9/hawb", {"master_no": "x"}, "kh")
            assert s.secs and s.secs[0] > 0, f"第 {int(config.J9_RATE_PER_SEC) + 1} 次该先等：{s.secs}"
            assert calls[0] == config.J9_TIMEOUT_READ, f"读接口用更短的超时：{calls[0]}"
    finally:
        company_api.urllib.request.urlopen = real
        (config.COMPANY_API_MODE, config.COMPANY_API_URL, config.COMPANY_HAWB_KEY) = old
