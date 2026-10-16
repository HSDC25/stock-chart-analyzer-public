"""
Thread-safe caching utilities for stock chart analyzer.

Provides single-value and dict-keyed caches with TTL expiration,
safe for use across Flask request threads.

Python 3.8 compatible.

简介：进程内线程安全缓存（带 TTL 过期），供行情抓取等模块缓存
秒级网络请求结果使用。关键设计：fetch 回调在锁外执行，避免慢请求
阻塞其他线程的读缓存操作。
"""

import threading
from datetime import datetime
from typing import Any, Callable, Dict, Optional


class ThreadSafeCache:
    """Thread-safe cache with TTL expiration.

    中文说明：单值缓存，缓存未过期直接返回，过期后调用 fetch_fn 重新取数。
    """

    def __init__(self, ttl_seconds: int = 300):
        """单值缓存：同一时刻只保存一份数据（如股票列表），*ttl_seconds* 为过期秒数。"""
        self._lock = threading.Lock()
        self._data: Any = None
        self._timestamp: Optional[datetime] = None
        self._ttl = ttl_seconds

    def get(self, fetch_fn: Callable, *args, **kwargs) -> Any:
        """Get cached value or fetch and cache a new one.

        fetch_fn（通常是秒级网络请求）在锁外执行：若持锁调用，所有并发线程
        都会串行阻塞在同一把锁上。代价是缓存过期瞬间可能有多个线程同时
        发起 fetch（thundering herd），对只读行情数据可接受。
        """
        with self._lock:
            if self._is_fresh():
                return self._data

        result = fetch_fn(*args, **kwargs)

        with self._lock:
            self._data = result
            self._timestamp = datetime.now()
            return self._data

    def _is_fresh(self) -> bool:
        """调用方必须已持有 _lock。"""
        now = datetime.now()
        return (self._data is not None
                and self._timestamp is not None
                and (now - self._timestamp).total_seconds() < self._ttl)

    def set(self, value: Any) -> None:
        """直接写入缓存并刷新时间戳（供绕过 fetch_fn 的调用方使用）。"""
        with self._lock:
            self._data = value
            self._timestamp = datetime.now()

    def invalidate(self) -> None:
        """Clear the cached value."""
        with self._lock:
            self._data = None
            self._timestamp = None

    @property
    def is_valid(self) -> bool:
        """Check whether the cached value is still within its TTL."""
        with self._lock:
            return self._is_fresh()


class ThreadSafeDictCache:
    """Thread-safe dict cache with per-key TTL."""

    def __init__(self, ttl_seconds: int = 300):
        """按键缓存的字典版：每个 key 独立记录时间戳、独立判过期。"""
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._timestamps: Dict[str, datetime] = {}
        self._ttl = ttl_seconds

    def get(self, key: str, fetch_fn: Callable, *args, **kwargs) -> Any:
        """Get cached value for *key*, or call *fetch_fn* to populate it.

        与 ThreadSafeCache.get 相同：fetch_fn 在锁外执行，避免慢请求把
        其它 key 的读取也一并阻塞。
        """
        with self._lock:
            now = datetime.now()
            if (key in self._data
                    and key in self._timestamps
                    and (now - self._timestamps[key]).total_seconds() < self._ttl):
                return self._data[key]

        result = fetch_fn(*args, **kwargs)

        with self._lock:
            self._data[key] = result
            self._timestamps[key] = datetime.now()
            return result

    def set(self, key: str, value: Any) -> None:
        """直接写入某个 key（供绕过 fetch_fn 的调用方使用）。"""
        with self._lock:
            self._data[key] = value
            self._timestamps[key] = datetime.now()

    def invalidate(self, key: Optional[str] = None) -> None:
        """Invalidate a single key, or the entire cache when *key* is None."""
        with self._lock:
            if key is None:
                self._data.clear()
                self._timestamps.clear()
            else:
                self._data.pop(key, None)
                self._timestamps.pop(key, None)
