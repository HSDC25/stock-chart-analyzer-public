"""Stock code validation and filter criteria data models.

简介：股票代码合法性判断（主板/创业板白名单、ST 过滤、格式校验）
与选股筛选条件 FilterCriteria 数据类，被 fetcher/screening 及各路由共用。
"""

import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional


def is_allowed_stock_code(code):
    # type: (...) -> bool
    """Check whether a stock code belongs to the retained A-share universe.

    Kept: Shanghai main board (6xx, excl. STAR 688), Shenzhen main board (0xx),
          ChiNext / GEM (3xx).
    Excluded: STAR Market (688), BSE / Beijing Exchange (4/8/9 prefix),
              and codes starting with 388.
    """
    if not code:
        return False
    code = str(code)
    if code.startswith('688'):
        return False
    return code.startswith(('6', '0', '3')) and not code.startswith('388')


def is_stock_st(name):
    # type: (...) -> bool
    """Check whether a stock is ST / *ST (to be filtered out entirely).

    Covers prefixes: ST, *ST, SST, S*ST (case-insensitive).
    """
    if not name:
        return False
    name = str(name).strip().upper()
    return name.startswith(('ST', '*ST', 'SST', 'S*ST'))


def validate_stock_code_format(code):
    # type: (...) -> bool
    """Validate that *code* matches the standard 6-digit A-share format.

    Accepted prefixes: 0 (Shenzhen main board), 3 (ChiNext), 6 (Shanghai main board).
    """
    if not code:
        return False
    return bool(re.match(r'^[036]\d{5}$', str(code)))


@dataclass
class FilterCriteria:
    """Stock screening filter criteria."""
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    change_pct_min: Optional[float] = None
    change_pct_max: Optional[float] = None
    volume_min: Optional[float] = None
    pe_min: Optional[float] = None
    pe_max: Optional[float] = None
    pb_min: Optional[float] = None
    pb_max: Optional[float] = None
    market_cap_min: Optional[float] = None
    market_cap_max: Optional[float] = None
    # 3-day cumulative change filter
    three_day_change_min: Optional[float] = None
    # Volume change rate filter (daily volume change >= 10%)
    volume_change_pct_min: Optional[float] = None
    codes: Optional[List[str]] = None
    # Sort field and direction
    sort_by: Optional[str] = None          # e.g. 'price'
    sort_order: Optional[str] = None       # 'asc' / 'desc', else default
    # Limit-up pullback filter
    limit_up_pullback: bool = False       # previous day limit-up, then 2-4 day pullback <= 6-7%
    # Count of days with >3% gain in the last 50 trading days
    big_up_days_count_min: Optional[int] = None
    big_up_days_count_max: Optional[int] = None

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> FilterCriteria
        """Create a FilterCriteria instance from a dictionary."""
        return cls(**{k: v for k, v in data.items() if v is not None})
