# -*- coding: utf-8 -*-
"""模型调用的错误分类（2026-10-18 效率审查 M1）。

`except Exception` 一律重试的代价是直接的：报文自己错了（400/422 参数不被接受、401 鉴权、
404 路径不存在）也照样把整包 base64 再发两遍——每一次都是真金白银，而结果必然一样。
超时那一档早就单独收过口（VLM_TIMEOUT_TRIES），这次把"再发也没用"的其余几种也分开。

同时钉住"答案本身不会再变"的那两种：被 max_tokens 截断（finish_reason=length）、
响应里一条候选都没有。重发一次还是同一个结果，纯白花钱。
"""
import contextlib
import tempfile
from pathlib import Path
from types import SimpleNamespace

import config
import retry
import transcribe
import vlm_extract


class _ApiError(Exception):
    """像 openai 的 APIStatusError 那样带一个 status_code（分类判据看的就是它）。"""

    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def _client(*, exc=None, content="{}", choices=True, finish=""):
    """假客户端：每次调用要么抛 exc，要么回一条 content。"""
    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        if exc is not None:
            raise exc
        cs = [SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)]
        return SimpleNamespace(choices=cs if choices else [], usage=None)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


@contextlib.contextmanager
def _no_sleep():
    """退避是真的会睡的（2s 起步）：用例里换掉，不然一次断言要等十几秒。"""
    old = retry.wait
    retry.wait = lambda attempt: 0.0
    try:
        yield
    finally:
        retry.wait = old


def _l2(client):
    return vlm_extract._chat_json("提示", [], ["PIECES"], "某票", client=client)


def _l1(client):
    """L1 走 transcribe_one：模型要有视觉、客户端要能注进去、页图直接给免得渲染。"""
    old_vision, old_get = config.MODEL_VISION, transcribe._get_client
    config.MODEL_VISION, transcribe._get_client = True, lambda: client
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "扫描件.png"
            p.write_bytes(b"\x89PNG fake")
            return transcribe.transcribe_one(p, urls=["data:image/png;base64,AAAA"])
    finally:
        config.MODEL_VISION, transcribe._get_client = old_vision, old_get


def test_a_rejected_request_is_not_sent_again():
    """400：报文本身不被接受。再发两遍还是同一个 400，白等十几秒、白付两包图的钱。"""
    fake, calls = _client(exc=_ApiError("参数不合法", 400))
    with _no_sleep():
        try:
            _l2(fake)
            raise AssertionError("400 居然没失败")
        except RuntimeError as e:
            assert "参数不合法" in str(e), e
    assert calls["n"] == 1, f"400 被重发了 {calls['n']} 次：这份钱花得毫无意义"


def test_an_authentication_error_is_not_sent_again():
    fake, calls = _client(exc=_ApiError("鉴权失败", 401))
    with _no_sleep():
        try:
            _l2(fake)
            raise AssertionError("401 居然没失败")
        except RuntimeError:
            pass
    assert calls["n"] == 1, f"401 被重发了 {calls['n']} 次"


def test_a_missing_endpoint_is_not_sent_again():
    fake, calls = _client(exc=_ApiError("路径不存在", 404))
    with _no_sleep():
        try:
            _l2(fake)
            raise AssertionError("404 居然没失败")
        except RuntimeError:
            pass
    assert calls["n"] == 1, f"404 被重发了 {calls['n']} 次"


def test_a_rate_limit_is_still_retried():
    """429 是对端在抖，不是我们报文错了：照旧重试到 VLM_RETRIES。"""
    fake, calls = _client(exc=_ApiError("太频繁", 429))
    with _no_sleep():
        try:
            _l2(fake)
            raise AssertionError("429 一直失败居然算成功了")
        except RuntimeError:
            pass
    assert calls["n"] == config.VLM_RETRIES, f"429 该重试到底，实际只发了 {calls['n']} 次"


def test_a_server_error_is_still_retried():
    fake, calls = _client(exc=_ApiError("网关错误", 503))
    with _no_sleep():
        try:
            _l2(fake)
            raise AssertionError("503 一直失败居然算成功了")
        except RuntimeError:
            pass
    assert calls["n"] == config.VLM_RETRIES, f"5xx 该重试到底，实际只发了 {calls['n']} 次"


def test_a_truncated_answer_is_not_sent_again():
    """被 max_tokens 截断：同一个请求再发一次还是同一个长度。
    真正该做的是把 max_tokens 抬上去，而这句话必须出现在错误里，不然现场只会看到"模型老是失败"。"""
    fake, calls = _client(content="{\"PIECES\": 1", finish="length")
    with _no_sleep():
        try:
            _l2(fake)
            raise AssertionError("被截断的响应居然算成功了")
        except RuntimeError as e:
            assert "max_tokens" in str(e), f"没说清是被截断了：{e}"
    assert calls["n"] == 1, f"截断的响应被重发了 {calls['n']} 次"


def test_an_empty_choice_list_is_not_sent_again():
    fake, calls = _client(choices=False)
    with _no_sleep():
        try:
            _l2(fake)
            raise AssertionError("空候选居然算成功了")
        except RuntimeError as e:
            assert "候选" in str(e), f"没说清是响应里没东西：{e}"
    assert calls["n"] == 1, f"空候选被重发了 {calls['n']} 次"


def test_a_broken_json_answer_is_still_retried():
    """答的不是 JSON：这个还有救——同一个提示词再发一次确实可能换个说法（temperature=0
    也不保证逐字节相同），所以它照旧走满重试，不跟 4xx 混成一档。"""
    fake, calls = _client(content="我看不清这张票")
    with _no_sleep():
        try:
            _l2(fake)
            raise AssertionError("非 JSON 居然算成功了")
        except RuntimeError:
            pass
    assert calls["n"] == config.VLM_RETRIES, f"非 JSON 该重试到底，实际只发了 {calls['n']} 次"


def test_the_transcription_layer_follows_the_same_rule():
    """L1 与 L2 是两次独立收费：这条口径不落到 L1，扫描件的钱只省了一半。"""
    fake, calls = _client(exc=_ApiError("参数不合法", 400))
    with _no_sleep():
        try:
            _l1(fake)
            raise AssertionError("L1 的 400 居然没失败")
        except RuntimeError as e:
            assert "参数不合法" in str(e), e
    assert calls["n"] == 1, f"L1 的 400 被重发了 {calls['n']} 次"


def test_the_transcription_layer_still_retries_a_server_error():
    fake, calls = _client(exc=_ApiError("网关错误", 503))
    with _no_sleep():
        try:
            _l1(fake)
            raise AssertionError("L1 的 503 一直失败居然算成功了")
        except RuntimeError:
            pass
    assert calls["n"] == config.VLM_RETRIES, f"L1 的 5xx 该重试到底，实际只发了 {calls['n']} 次"
