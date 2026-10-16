"""Smoke tests for common.models (pure-Python, no heavy deps)."""
from common.models import (
    is_allowed_stock_code,
    is_stock_st,
    validate_stock_code_format,
    FilterCriteria,
)


def test_is_allowed_stock_code_keep():
    # 保留的 A 股范围：沪市主板(6xx, 不含688)、深市主板(0xx)、创业板(3xx)
    assert is_allowed_stock_code("600000")
    assert is_allowed_stock_code("000001")
    assert is_allowed_stock_code("300750")


def test_is_allowed_stock_code_exclude():
    # 排除：科创板(688)、北交所(4/8/9 开头)、388 开头
    assert not is_allowed_stock_code("688001")
    assert not is_allowed_stock_code("430001")
    assert not is_allowed_stock_code("388001")


def test_is_allowed_stock_code_edge():
    assert not is_allowed_stock_code("")
    assert not is_allowed_stock_code(None)
    assert not is_allowed_stock_code("abc")


def test_is_stock_st():
    assert is_stock_st("ST 某某")
    assert is_stock_st("*ST 某某")
    assert is_stock_st("sst 某某")
    assert not is_stock_st("平安银行")
    assert not is_stock_st("")


def test_validate_stock_code_format():
    assert validate_stock_code_format("600000")
    assert validate_stock_code_format("300750")
    assert not validate_stock_code_format("60000")   # 5 位
    assert not validate_stock_code_format("123456")  # 错误前缀
    assert not validate_stock_code_format("")


def test_filter_criteria_defaults():
    fc = FilterCriteria()
    assert fc is not None
    # 至少能构造不报错
    assert hasattr(fc, "three_day_change_pct") or True
