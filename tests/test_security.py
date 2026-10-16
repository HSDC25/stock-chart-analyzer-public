"""Smoke tests for common.security pure helpers (no Flask needed)."""
import time

from common.security import RateLimiter, hmac_compare


def test_hmac_compare_equal():
    assert hmac_compare("abc", "abc") is True
    assert hmac_compare("", "") is True


def test_hmac_compare_diff():
    assert hmac_compare("abc", "abd") is False
    assert hmac_compare("abc", "") is False


def test_rate_limiter_burst():
    rl = RateLimiter(rate=10.0, burst=2)
    # 前两个令牌来自 burst，立即获得
    assert rl.acquire() == 0
    assert rl.acquire() == 0
    # 第三个需要等待（返回等待秒数 > 0）
    wait = rl.acquire()
    assert wait > 0


def test_rate_limiter_refill():
    rl = RateLimiter(rate=100.0, burst=1)
    assert rl.acquire() == 0          # 用掉唯一令牌
    assert rl.acquire() > 0           # 暂无令牌
    time.sleep(0.05)
    # rate=100/s => 0.05s 后应补充约 5 个令牌
    assert rl.acquire() == 0
