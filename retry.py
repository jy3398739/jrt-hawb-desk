# -*- coding: utf-8 -*-
"""重试退避：一处算"这次失败后等多久"，解析链与公司接口都用它。

原来两边各写一份 `sleep(3)`：固定间隔既不给了对端恢复的时间，也让同一秒点提交的 8 个人
重试撞在同一秒上。退避递增 + 抖动，是把"服务在抖"和"我们自己在打它"分开的最小做法。
"""
import random
import time

DELAYS = (2.0, 8.0, 20.0)      # 第 1/2/3 次重试前等多久，之后重复最后一档（不会滚成十分钟）
JITTER = 0.25                  # ±25%


def delay(attempt: int) -> float:
    """attempt 从 0 起（第一次失败后要等多久）。"""
    base = DELAYS[min(attempt, len(DELAYS) - 1)]
    return round(base * (1 + random.uniform(-JITTER, JITTER)), 3)


def wait(attempt: int) -> float:
    """睡掉这一档，返回实际睡了几秒（调用方要记日志时用得上）。"""
    secs = delay(attempt)
    time.sleep(secs)
    return secs
