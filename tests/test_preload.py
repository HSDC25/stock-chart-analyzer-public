"""预加载断点续跑回归测试（M2/M3）。

重点：线程池乱序完成时，断点必须是"连续完成水位线"而不是"最后完成的那只"，
否则恢复时会跳过水位线之后、却先于它完成的股票前面的未完成项。
"""
import json

import pytest

import common.preload as preload


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "preload_state.json"
    monkeypatch.setattr(preload, "PRELOAD_STATE_FILE", path)
    preload._reset_checkpoint([])
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_watermark_out_of_order_does_not_skip(state_file):
    codes = ["a", "b", "c", "d", "e"]
    preload._reset_checkpoint(codes)
    # 乱序完成：先 c(2)、e(4)，水位线不动（a 还没好），不落盘
    preload._advance_checkpoint(2)
    preload._advance_checkpoint(4)
    assert preload._checkpoint_watermark == 0
    assert not state_file.exists()
    # a(0) 完成 → 水位线到 1，断点 a
    preload._advance_checkpoint(0)
    assert preload._checkpoint_watermark == 1
    assert _read(state_file)["last_code"] == "a"
    # b(1) 完成 → 连续吃掉已完成的 c(2)，水位线到 3，断点 c
    preload._advance_checkpoint(1)
    assert preload._checkpoint_watermark == 3
    assert _read(state_file)["last_code"] == "c"
    # d(3) 完成 → 连续吃掉 e(4)，水位线到 5，断点 e
    preload._advance_checkpoint(3)
    assert preload._checkpoint_watermark == 5
    assert _read(state_file)["last_code"] == "e"


def test_resume_index_equals_watermark(state_file):
    codes = ["a", "b", "c", "d", "e"]
    preload._reset_checkpoint(codes)
    for idx in (0, 1, 2):
        preload._advance_checkpoint(idx)
    last_code = _read(state_file)["last_code"]
    # 恢复逻辑 codes.index(last_code)+1 必须正好等于水位线，不重不漏
    assert codes.index(last_code) + 1 == preload._checkpoint_watermark == 3


def test_in_order_completion(state_file):
    codes = ["x", "y", "z"]
    preload._reset_checkpoint(codes)
    for idx, code in enumerate(codes):
        preload._advance_checkpoint(idx)
        assert _read(state_file)["last_code"] == code
    assert preload._checkpoint_watermark == 3


def test_reset_clears_pending_set(state_file):
    preload._reset_checkpoint(["a", "b", "c"])
    preload._advance_checkpoint(2)  # 乱序，记入待完成集合
    assert 2 in preload._checkpoint_done
    preload._reset_checkpoint(["m", "n"])
    assert preload._checkpoint_done == set()
    assert preload._checkpoint_watermark == 0
    preload._advance_checkpoint(0)
    assert _read(state_file)["last_code"] == "m"


def test_clear_preload_state_removes_file(state_file):
    preload._reset_checkpoint(["a"])
    preload._advance_checkpoint(0)
    assert state_file.exists()
    preload.clear_preload_state()
    assert not state_file.exists()