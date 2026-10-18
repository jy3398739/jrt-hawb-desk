# -*- coding: utf-8 -*-
"""重试退避：一处算"这次失败后等多久"，解析链与公司接口都用它。

原来两边各写一份 `sleep(3)`：固定间隔既不给了对端恢复的时间，也让同一秒点提交的 8 个人
重试撞在同一秒上。退避递增 + 抖动，是把"服务在抖"和"我们自己在打它"分开的最小做法。
"""
import random
import time

DELAYS = (2.0, 8.0, 20.0)      # 第 1/2/3 次重试前等多久，之后重复最后一档（不会滚成十分钟）
JITTER = 0.25                  # ±25%


class Hopeless(Exception):
    """答案本身不会再变：被 max_tokens 截断、响应里一条候选都没有。
    重发一次只是再花一次钱，直接失败并把原因说清楚。"""


# 报文自己错了（参数不被接受、鉴权、路径不存在）：再发一次还是同一个 4xx。
# 429 与 5xx 是"对端在抖"，照旧重试；超时那一档另有 VLM_TIMEOUT_TRIES 收口。
_HOPELESS_NAMES = frozenset((
    "BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError",
    "UnprocessableEntityError", "UnsupportedParamsError", "MethodNotAllowedError"))


def hopeless(e) -> bool:
    """这个失败再发一次有没有意义。判据按 HTTP 码走（openai 的异常都带 status_code），
    拿不到码时退回按类名认——认不出的照旧当"可以再试"，宁可多试一次也别把能救的判死。"""
    if isinstance(e, Hopeless):
        return True
    code = getattr(e, "status_code", None)
    if isinstance(code, int):
        return 400 <= code < 500 and code != 429
    return type(e).__name__ in _HOPELESS_NAMES


def delay(attempt: int) -> float:
    """attempt 从 0 起（第一次失败后要等多久）。"""
    base = DELAYS[min(attempt, len(DELAYS) - 1)]
    return round(base * (1 + random.uniform(-JITTER, JITTER)), 3)


def wait(attempt: int) -> float:
    """睡掉这一档，返回实际睡了几秒（调用方要记日志时用得上）。"""
    secs = delay(attempt)
    time.sleep(secs)
    return secs
