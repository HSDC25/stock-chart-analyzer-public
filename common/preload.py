"""
Background preloading infrastructure for stock K-line and realtime data.

Provides the ``PreloadState`` class for thread-safe progress tracking, state
persistence helpers for checkpoint/resume, and the ``background_preload``
pipeline that runs in a low-priority daemon thread.

Extracted from win11/app.py.  Python 3.8 compatible.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from common.config import DB_PATH, PRELOAD_STATE_FILE
from common.db import save_kline_to_db
from common.models import is_allowed_stock_code

logger = logging.getLogger("stockchart")


# ============================================================================
# Thread priority helper
# ============================================================================

def set_thread_low_priority():
    # type: () -> None
    """Lower the current thread's priority on Windows.

    Uses ``SetThreadPriority`` with ``THREAD_PRIORITY_LOWEST`` (1).
    Silently ignored on non-Windows platforms or when permissions are
    insufficient.
    """
    try:
        ctypes.windll.kernel32.SetThreadPriority(
            ctypes.windll.kernel32.GetCurrentThread(), 1
        )
    except Exception as e:
        logger.debug("set_thread_low_priority failed (non-Windows or permission): %s", e)


# ============================================================================
# PreloadState — encapsulates all preload progress / control state
# ============================================================================

class PreloadState:
    """Thread-safe container for background preload progress and control.

    Owns its lock so callers never need to manage locking externally.
    """

    def __init__(self):
        # type: () -> None
        self._lock = threading.Lock()
        self._progress = {
            "running": False,
            "total": 0,
            "loaded": 0,
            "failed": 0,
            "current_code": "",
            "message": "",
        }  # type: dict
        self._thread = None  # type: Optional[threading.Thread]
        self._stop_event = threading.Event()

    # -- public API ----------------------------------------------------------

    @property
    def is_running(self):
        # type: () -> bool
        """Whether a preload is currently active."""
        with self._lock:
            return self._progress["running"]

    def get_status(self):
        # type: () -> dict
        """Return a snapshot of the current progress dict.

        ``_consec_fail`` 是内部重连计数，不属于对外 API，快照时剔除，
        避免通过 /api/preload/status 泄露内部状态。
        """
        with self._lock:
            return {k: v for k, v in self._progress.items()
                    if not k.startswith("_")}

    def start(self, target):
        # type: (...) -> None
        """Spawn a new daemon thread running *target*.

        Raises ``RuntimeError`` if a preload is already running.

        “标记 running”和“创建线程”必须在同一临界区内完成：若先检查后释放
        锁再建线程，两个并发请求可能同时通过检查，各自起一个 preload 线程。
        """
        with self._lock:
            if self._progress["running"]:
                raise RuntimeError("preload already running")
            # 内联标记 running（不能调 _mark_running()：非重入锁，会自锁）
            self._progress["running"] = True
            self._progress["loaded"] = 0
            self._progress["failed"] = 0
            self._progress["message"] = "Starting preload..."
            self._stop_event.clear()
            self._thread = threading.Thread(target=target, daemon=True)
            self._thread.start()

    def stop(self):
        # type: () -> None
        """Signal the preload thread to stop (cooperative)."""
        self._stop_event.set()

    # -- internal helpers used by background_preload -------------------------

    def _mark_running(self):
        # type: () -> None
        with self._lock:
            self._progress["running"] = True
            self._progress["loaded"] = 0
            self._progress["failed"] = 0
            self._progress["message"] = "Starting preload..."

    def _mark_finished(self, message="Preload complete"):
        # type: (str) -> None
        with self._lock:
            self._progress["running"] = False
            self._progress["message"] = message

    def _set_message(self, msg):
        # type: (str) -> None
        with self._lock:
            self._progress["message"] = msg

    def _set_total(self, total):
        # type: (int) -> None
        with self._lock:
            self._progress["total"] = total

    def _record_success(self):
        # type: () -> None
        with self._lock:
            self._progress["loaded"] += 1
            self._progress["_consec_fail"] = 0
            loaded = self._progress["loaded"]
            total = self._progress["total"]
            failed = self._progress["failed"]
            msg = "Preloading ({}/{})".format(loaded, total)
            if failed:
                msg += ", failed {}".format(failed)
            msg += "..."
            self._progress["message"] = msg

    def _record_skip(self):
        # type: () -> None
        """Record a skipped stock (data already fresh)."""
        with self._lock:
            self._progress["loaded"] += 1
            self._progress["_consec_fail"] = 0
            loaded = self._progress["loaded"]
            total = self._progress["total"]
            self._progress["message"] = "Preloading ({}/{})...".format(loaded, total)

    def _record_failure(self):
        # type: () -> None
        with self._lock:
            self._progress["loaded"] += 1
            self._progress["failed"] += 1
            self._progress["_consec_fail"] = self._progress.get("_consec_fail", 0) + 1
            loaded = self._progress["loaded"]
            total = self._progress["total"]
            failed = self._progress["failed"]
            msg = "Preloading ({}/{})".format(loaded, total)
            if failed:
                msg += ", failed {}".format(failed)
            msg += "..."
            self._progress["message"] = msg

    @property
    def stop_event(self):
        # type: () -> threading.Event
        return self._stop_event


# ============================================================================
# Preload state persistence (checkpoint / resume)
# ============================================================================

def load_preload_state():
    # type: () -> dict
    """Read preload checkpoint from disk. Returns empty dict on failure."""
    try:
        if PRELOAD_STATE_FILE.exists():
            with open(str(PRELOAD_STATE_FILE), "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.info("[preload] read checkpoint failed: %s", e)
    return {}


_preload_state_lock = threading.Lock()
# 完成水位线：codes 前缀 [0, watermark) 已全部处理完。线程池乱序完成，
# 不能拿"最后完成的那只"当断点（会跳过它前面还没跑完的）；只在水位线连续
# 推进时落盘，恢复时从水位线之后继续，保证不重不漏。
_checkpoint_watermark = 0
_checkpoint_codes = []  # type: list
_checkpoint_done = set()  # type: set  # 已完成但位于水位线之后的下标


def _reset_checkpoint(codes):
    # type: (list) -> None
    global _checkpoint_watermark, _checkpoint_codes, _checkpoint_done
    with _preload_state_lock:
        _checkpoint_codes = codes
        _checkpoint_watermark = 0
        _checkpoint_done = set()


def _advance_checkpoint(idx):
    # type: (int) -> None
    """标记第 *idx* 只（相对本轮 codes）处理完，推进连续水位线并按需落盘。

    乱序完成时先把下标记入集合；只有水位线处完成才连续前移，把集合里
    紧挨着的已完成下标一并吃掉。落盘的是水位线前最后一只代码，恢复时
    ``codes.index(last_code)+1`` 正好等于水位线，不重不漏。
    """
    global _checkpoint_watermark
    with _preload_state_lock:
        _checkpoint_done.add(idx)
        codes = _checkpoint_codes
        wm = _checkpoint_watermark
        while wm in _checkpoint_done:
            _checkpoint_done.discard(wm)
            wm += 1
        if wm == _checkpoint_watermark:
            return  # 水位线没动，无需落盘
        _checkpoint_watermark = wm
        if not codes or wm > len(codes):
            return
        payload = {
            "last_code": codes[wm - 1],
            "last_time": datetime.now().isoformat(),
        }
    save_preload_state(payload)


def save_preload_state(state):
    # type: (dict) -> None
    """Persist preload checkpoint to disk.

    可能被预加载线程池的多个 worker 并发调用：先写临时文件再原子替换，
    外层加模块级锁串行化，避免并发写产生损坏的 JSON。
    """
    try:
        with _preload_state_lock:
            tmp_path = str(PRELOAD_STATE_FILE) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp_path, str(PRELOAD_STATE_FILE))
    except Exception as e:
        logger.info("[preload] write checkpoint failed: %s", e)


def clear_preload_state():
    # type: () -> None
    """Remove the checkpoint file after a successful full preload."""
    try:
        if PRELOAD_STATE_FILE.exists():
            PRELOAD_STATE_FILE.unlink()
    except Exception as e:
        logger.warning("clear preload checkpoint failed: %s", e)


# ============================================================================
# background_preload — main pipeline
# ============================================================================

def background_preload(state=None):
    # type: (Optional[PreloadState]) -> None
    """Low-priority background preload pipeline.

    1. Fetches the stock list via ``common.fetcher.fetcher.get_stock_list()``.
    2. Iterates over all allowed stock codes, fetching K-line data with
       multi-source failover and persisting to SQLite.
    3. Batch-fetches realtime quotes from Tencent and writes to
       ``stock_metadata``.

    Supports checkpoint/resume: if interrupted, the next run picks up where
    it left off.

    Parameters
    ----------
    state : PreloadState, optional
        Shared state object.  If *None*, a default module-level instance is
        **not** created — callers should always pass one.
    """
    if state is None:
        logger.error("[preload] background_preload called without PreloadState")
        return

    # Import here to avoid circular imports at module load time
    from common.fetcher import (
        fetch_kline,
        fetch_realtime_tencent_to_db,
        fetcher,
    )

    set_thread_low_priority()
    # running 标记已由 state.start() 在锁内置位，这里只重置计数器

    try:
        state._set_message("Fetching stock list...")
        stock_list = fetcher.get_stock_list()
        codes = stock_list["code"].tolist()

        # Filter: keep only Shanghai/Shenzhen A-shares
        codes = [c for c in codes if is_allowed_stock_code(c)]

        # --- Checkpoint resume ---
        checkpoint = load_preload_state()
        last_code = checkpoint.get("last_code")
        start_idx = 0
        if last_code and last_code in codes:
            start_idx = codes.index(last_code) + 1
        codes = codes[start_idx:]
        _reset_checkpoint(codes)
        state._set_total(len(codes))
        state._set_message(
            "Preloading (0/{}), resuming from offset {}...".format(len(codes), start_idx)
        )

        def _preload_one(item):
            idx, code = item
            if state.stop_event.is_set():
                return
            try:
                # --- Incremental update strategy ---
                # 按本地库最新日期距今天数决定抓取量：越新抓越少，
                # 大幅减少全量预加载的网络请求量
                fetch_days = 30
                try:
                    conn = sqlite3.connect(DB_PATH, timeout=5)
                    cur = conn.cursor()
                    cur.execute("SELECT MAX(date) FROM kline_data WHERE code = ?", (code,))
                    row = cur.fetchone()
                    conn.close()
                    if row and row[0]:
                        db_latest = datetime.strptime(row[0], "%Y-%m-%d").date()
                        today = datetime.now().date()
                        gap = (today - db_latest).days
                        if gap <= 1:
                            state._record_skip()
                            return
                        elif gap <= 7:
                            fetch_days = 3
                        elif gap <= 30:
                            fetch_days = 10
                except Exception as e:
                    logger.debug("freshness check failed, defaulting to 30 days: %s", e)

                df = fetch_kline(code, days=fetch_days)
                if df is not None and not df.empty:
                    save_kline_to_db(code, df)
                state._record_success()
            except Exception as e:
                logger.info("[preload] %s failed: %s: %s", code, type(e).__name__, e)
                state._record_failure()
            finally:
                # 成功/跳过/失败都算"处理完"，统一推进水位线；停止时未启动的
                # 任务不会到这里，水位线停在真实完成处，下次从其后继续
                _advance_checkpoint(idx)

        # Chunked loading: 1000 stocks per segment, pause between segments
        # 分段加载并在段间停顿，避免对行情源形成长时间持续高频请求而被风控封禁
        chunk_size = 1000
        pause_seconds = 5
        for i in range(0, len(codes), chunk_size):
            if state.stop_event.is_set():
                break
            chunk = list(enumerate(codes[i:i + chunk_size], start=i))
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(_preload_one, chunk))
            if i + chunk_size < len(codes) and not state.stop_event.is_set():
                state._set_message(
                    "Preloading (segment cooldown {}s)...".format(pause_seconds)
                )
                time.sleep(pause_seconds)

        # 只有整轮跑完（未被停止）才清断点；中途停止保留水位线供下次续跑
        if not state.stop_event.is_set():
            clear_preload_state()

        # --- Step 2: batch realtime quotes ---
        if not state.stop_event.is_set():
            state._set_message("Fetching realtime quotes ({} stocks)...".format(len(codes)))

            def _quote_progress(done, total):
                state._set_message(
                    "Fetching realtime quotes ({}/{})...".format(done, total)
                )

            try:
                written = fetch_realtime_tencent_to_db(codes, on_progress=_quote_progress)
                logger.info("[preload] realtime quotes written: %d", written)
                # Invalidate stock list cache so next filter uses fresh SQLite data
                fetcher.invalidate_stock_list_cache()
            except Exception as e:
                logger.info("[preload] realtime quote fetch failed: %s", e)

        if state.stop_event.is_set():
            state._mark_finished("Stopped by user")
        else:
            state._mark_finished("Preload complete")

    except Exception as e:
        state._mark_finished("Preload error: {}".format(str(e)))
