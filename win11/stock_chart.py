#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StockChart 独立 CLI 图表导出工具（一次性命令行工具，不参与桌面壳运行）。

基于 akshare（行情数据源）+ pyecharts（绘图）在命令行交互式筛选股票，
生成 K线+均线+MACD+RSI+成交量组合图，导出 HTML / Chrome 截图 PNG。
技术指标计算复用 common.indicators；与 app.py / launcher 互不依赖。
用法示例: python stock_chart.py --code 000001 [--export-image]
"""

import os
import re
import sys
import json
import argparse
import warnings
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

import akshare as ak
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from common.indicators import calculate_ma, calculate_macd, calculate_rsi, calculate_bollinger

from pyecharts import options as opts
from pyecharts.charts import Kline, Bar, Line, Grid
from pyecharts.commons.utils import JsCode
from pyecharts.globals import CurrentConfig, ThemeType

# selenium 为可选依赖，仅在导出图片时需要
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# 忽略 akshare 的 SSL 警告
warnings.filterwarnings('ignore')


# ═════════════════════════════════════════════════════════════════════════════
# 数据模型
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class StockInfo:
    """股票基本信息"""
    code: str           # 股票代码
    name: str           # 股票名称
    price: float = 0.0  # 当前价格
    change_pct: float = 0.0  # 涨跌幅(%)
    volume: float = 0.0      # 成交量
    pe: float = 0.0          # 市盈率
    pb: float = 0.0          # 市净率
    market_cap: float = 0.0  # 总市值(亿元)


@dataclass
class FilterCriteria:
    """股票筛选条件"""
    # 基础指标
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    change_pct_min: Optional[float] = None
    change_pct_max: Optional[float] = None
    volume_min: Optional[float] = None  # 万手
    
    # 技术指标 (用于图表展示, 筛选时暂不直接使用)
    ma_periods: List[int] = field(default_factory=lambda: [5, 10, 20, 60])
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    
    # 基本面指标
    pe_min: Optional[float] = None
    pe_max: Optional[float] = None
    pb_min: Optional[float] = None
    pb_max: Optional[float] = None
    market_cap_min: Optional[float] = None  # 亿元
    market_cap_max: Optional[float] = None
    
    # 股票代码列表 (直接指定)
    codes: Optional[List[str]] = None


# ═════════════════════════════════════════════════════════════════════════════
# 技术指标计算 (已迁移至 common.indicators)
# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
# 数据获取
# ═════════════════════════════════════════════════════════════════════════════

class StockDataFetcher:
    """股票数据获取器"""
    
    def __init__(self):
        self._stock_list_cache: Optional[pd.DataFrame] = None
    
    def get_stock_list(self, refresh: bool = False) -> pd.DataFrame:
        """获取 A 股股票列表"""
        if self._stock_list_cache is not None and not refresh:
            return self._stock_list_cache
        
        # 获取沪深 A 股列表
        df = ak.stock_zh_a_spot_em()
        df = df[['代码', '名称', '最新价', '涨跌幅', '换手率', '市盈率-动态', 
                 '市净率', '总市值', '成交量']].copy()
        df.columns = ['code', 'name', 'price', 'change_pct', 'turnover', 
                      'pe', 'pb', 'market_cap', 'volume']
        # 清洗数据
        df['price'] = pd.to_numeric(df['price'], errors='coerce')
        df['change_pct'] = pd.to_numeric(df['change_pct'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        df['pe'] = pd.to_numeric(df['pe'], errors='coerce')
        df['pb'] = pd.to_numeric(df['pb'], errors='coerce')
        df['market_cap'] = pd.to_numeric(df['market_cap'], errors='coerce') / 1e8  # 转为亿元
        
        self._stock_list_cache = df
        return df
    
    def get_kline_data(self, code: str, period: str = "daily", 
                       start_date: Optional[str] = None,
                       end_date: Optional[str] = None,
                       days: int = 120) -> pd.DataFrame:
        """获取股票 K 线数据
        
        Args:
            code: 股票代码
            period: 周期 (daily/weekly/monthly)
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            days: 默认获取最近天数
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')
        if start_date is None:
            start_date = (datetime.strptime(end_date, '%Y%m%d') - 
                          timedelta(days=days)).strftime('%Y%m%d')
        
        try:
            # 尝试获取历史行情
            df = ak.stock_zh_a_hist(symbol=code, period=period,
                                     start_date=start_date, end_date=end_date)
            if df.empty:
                return pd.DataFrame()
            
            df = df.rename(columns={
                '日期': 'date', '开盘': 'open', '收盘': 'close',
                '最高': 'high', '最低': 'low', '成交量': 'volume',
                '成交额': 'amount', '振幅': 'amplitude', '涨跌幅': 'pct_change',
                '涨跌额': 'change', '换手率': 'turnover'
            })
            df['date'] = pd.to_datetime(df['date'])
            return df.sort_values('date').reset_index(drop=True)
        except Exception as e:
            print(f"获取 {code} K线数据失败: {e}")
            return pd.DataFrame()


def filter_stocks(df: pd.DataFrame, criteria: FilterCriteria) -> pd.DataFrame:
    """根据筛选条件过滤股票"""
    if criteria.codes:
        # 直接按代码列表筛选
        df = df[df['code'].isin(criteria.codes)].copy()
        return df
    
    # 价格筛选
    if criteria.price_min is not None:
        df = df[df['price'] >= criteria.price_min]
    if criteria.price_max is not None:
        df = df[df['price'] <= criteria.price_max]
    
    # 涨跌幅筛选
    if criteria.change_pct_min is not None:
        df = df[df['change_pct'] >= criteria.change_pct_min]
    if criteria.change_pct_max is not None:
        df = df[df['change_pct'] <= criteria.change_pct_max]
    
    # 成交量筛选 (万手)
    if criteria.volume_min is not None:
        df = df[df['volume'] >= criteria.volume_min * 10000]
    
    # 市盈率筛选
    if criteria.pe_min is not None:
        df = df[df['pe'] >= criteria.pe_min]
    if criteria.pe_max is not None:
        df = df[df['pe'] <= criteria.pe_max]
    
    # 市净率筛选
    if criteria.pb_min is not None:
        df = df[df['pb'] >= criteria.pb_min]
    if criteria.pb_max is not None:
        df = df[df['pb'] <= criteria.pb_max]
    
    # 市值筛选 (亿元)
    if criteria.market_cap_min is not None:
        df = df[df['market_cap'] >= criteria.market_cap_min]
    if criteria.market_cap_max is not None:
        df = df[df['market_cap'] <= criteria.market_cap_max]
    
    return df.copy()


# ═════════════════════════════════════════════════════════════════════════════
# 图表生成
# ═════════════════════════════════════════════════════════════════════════════

class StockChart:
    """股票图表生成器"""
    
    def __init__(self, width: str = "1200px", height: str = "800px"):
        self.width = width
        self.height = height
    
    def _prepare_data(self, df: pd.DataFrame, criteria: FilterCriteria) -> pd.DataFrame:
        """准备数据并计算技术指标"""
        # 计算均线
        df = calculate_ma(df, criteria.ma_periods)
        # 计算 MACD
        df = calculate_macd(df, criteria.macd_fast, criteria.macd_slow, criteria.macd_signal)
        # 计算 RSI
        df = calculate_rsi(df, criteria.rsi_period)
        # 计算布林带
        df = calculate_bollinger(df)
        
        # 去除 NA 行
        df = df.dropna(subset=['MA5', 'MA10', 'MA20', 'MACD', 'RSI', 'BOLL_UPPER'])
        return df
    
    def create_chart(self, df: pd.DataFrame, stock_name: str, 
                     criteria: FilterCriteria) -> Grid:
        """生成股票图表"""
        df = self._prepare_data(df, criteria)
        
        if df.empty:
            raise ValueError("数据为空，无法生成图表")
        
        dates = df['date'].dt.strftime('%Y-%m-%d').tolist()
        
        # ═══ 主图: K线 + 均线 + 成交量 ═══
        kline = self._create_kline_chart(df, dates, stock_name)
        
        # ═══ 副图1: MACD ═══
        macd_chart = self._create_macd_chart(df, dates)
        
        # ═══ 副图2: RSI ═══
        rsi_chart = self._create_rsi_chart(df, dates)
        
        # ═══ 副图3: 成交量 ═══
        volume_chart = self._create_volume_chart(df, dates)
        
        # ═══ 组合图表 ═══
        grid = (
            Grid(init_opts=opts.InitOpts(
                width=self.width, 
                height=self.height,
                theme=ThemeType.DARK
            ))
            .add(kline, grid_opts=opts.GridOpts(pos_top="10%", height="40%"))
            .add(volume_chart, grid_opts=opts.GridOpts(pos_top="52%", height="15%"))
            .add(macd_chart, grid_opts=opts.GridOpts(pos_top="69%", height="15%"))
            .add(rsi_chart, grid_opts=opts.GridOpts(pos_top="86%", height="12%"))
        )
        
        return grid
    
    def _create_kline_chart(self, df: pd.DataFrame, dates: List[str], 
                            stock_name: str) -> Kline:
        """创建 K 线图"""
        # K 线数据
        kline_data = df[['open', 'close', 'low', 'high']].values.tolist()
        
        kline = (
            Kline(init_opts=opts.InitOpts(width=self.width))
            .add_xaxis(dates)
            .add_yaxis(
                "K线",
                kline_data,
                itemstyle_opts=opts.ItemStyleOpts(
                    color="#ef5350",
                    color0="#26a69a",
                    border_color="#ef5350",
                    border_color0="#26a69a",
                ),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(
                    title=f"{stock_name} ({df['date'].iloc[-1].strftime('%Y-%m-%d')})",
                    pos_left="center"
                ),
                xaxis_opts=opts.AxisOpts(
                    type_="category",
                    axislabel_opts=opts.LabelOpts(is_show=False),
                ),
                yaxis_opts=opts.AxisOpts(
                    type_="value",
                    splitline_opts=opts.SplitLineOpts(is_show=True, 
                                                      linestyle_opts=opts.LineStyleOpts(opacity=0.2)),
                ),
                tooltip_opts=opts.TooltipOpts(
                    trigger="axis",
                    axis_pointer_type="cross",
                    formatter=JsCode("""
                        function(params) {
                            var data = params[0];
                            return data.name + '<br/>' +
                                   '开: ' + data.data[1] + '<br/>' +
                                   '收: ' + data.data[2] + '<br/>' +
                                   '低: ' + data.data[3] + '<br/>' +
                                   '高: ' + data.data[4];
                        }
                    """),
                ),
                datazoom_opts=[
                    opts.DataZoomOpts(type_="inside", xaxis_index=[0, 1, 2, 3]),
                    opts.DataZoomOpts(type_="slider", xaxis_index=[0, 1, 2, 3], pos_bottom="2%"),
                ],
                legend_opts=opts.LegendOpts(pos_top="5%"),
            )
        )
        
        # 添加均线
        colors = {"MA5": "#ff9800", "MA10": "#2196f3", "MA20": "#9c27b0", "MA60": "#4caf50"}
        for ma_name in ["MA5", "MA10", "MA20", "MA60"]:
            if ma_name in df.columns:
                line = (
                    Line()
                    .add_xaxis(dates)
                    .add_yaxis(
                        ma_name,
                        df[ma_name].round(2).tolist(),
                        is_symbol_show=False,
                        linestyle_opts=opts.LineStyleOpts(width=1, color=colors.get(ma_name, "#fff")),
                    )
                )
                kline = kline.overlap(line)
        
        # 添加布林带
        boll_upper = (
            Line()
            .add_xaxis(dates)
            .add_yaxis("BOLL上", df['BOLL_UPPER'].round(2).tolist(),
                       is_symbol_show=False,
                       linestyle_opts=opts.LineStyleOpts(width=1, color="#607d8b", type_="dashed"))
        )
        boll_lower = (
            Line()
            .add_xaxis(dates)
            .add_yaxis("BOLL下", df['BOLL_LOWER'].round(2).tolist(),
                       is_symbol_show=False,
                       linestyle_opts=opts.LineStyleOpts(width=1, color="#607d8b", type_="dashed"))
        )
        boll_mid = (
            Line()
            .add_xaxis(dates)
            .add_yaxis("BOLL中", df['BOLL_MID'].round(2).tolist(),
                       is_symbol_show=False,
                       linestyle_opts=opts.LineStyleOpts(width=1, color="#607d8b"))
        )
        kline = kline.overlap(boll_upper).overlap(boll_lower).overlap(boll_mid)
        
        return kline
    
    def _create_macd_chart(self, df: pd.DataFrame, dates: List[str]) -> Bar:
        """创建 MACD 图"""
        bar_colors = []
        for val in df['MACD_Hist']:
            if val >= 0:
                bar_colors.append("#ef5350")
            else:
                bar_colors.append("#26a69a")
        
        bar = (
            Bar(init_opts=opts.InitOpts(width=self.width))
            .add_xaxis(dates)
            .add_yaxis(
                "MACD",
                df['MACD_Hist'].round(4).tolist(),
                itemstyle_opts=opts.ItemStyleOpts(color=JsCode(f"""
                    function(params) {{
                        var colors = {json.dumps(bar_colors)};
                        return colors[params.dataIndex];
                    }}
                """)),
                label_opts=opts.LabelOpts(is_show=False),
            )
            .set_global_opts(
                xaxis_opts=opts.AxisOpts(
                    type_="category",
                    axislabel_opts=opts.LabelOpts(is_show=False),
                ),
                yaxis_opts=opts.AxisOpts(
                    type_="value",
                    splitline_opts=opts.SplitLineOpts(is_show=True, 
                                                      linestyle_opts=opts.LineStyleOpts(opacity=0.2)),
                ),
                legend_opts=opts.LegendOpts(is_show=False),
            )
        )
        
        # 添加 DIF 和 DEA 线
        line_dif = (
            Line()
            .add_xaxis(dates)
            .add_yaxis("DIF", df['MACD'].round(4).tolist(),
                       is_symbol_show=False,
                       linestyle_opts=opts.LineStyleOpts(width=1.5, color="#ff9800"))
        )
        line_dea = (
            Line()
            .add_xaxis(dates)
            .add_yaxis("DEA", df['MACD_Signal'].round(4).tolist(),
                       is_symbol_show=False,
                       linestyle_opts=opts.LineStyleOpts(width=1.5, color="#2196f3"))
        )
        
        bar = bar.overlap(line_dif).overlap(line_dea)
        return bar
    
    def _create_rsi_chart(self, df: pd.DataFrame, dates: List[str]) -> Line:
        """创建 RSI 图"""
        line = (
            Line(init_opts=opts.InitOpts(width=self.width))
            .add_xaxis(dates)
            .add_yaxis(
                "RSI",
                df['RSI'].round(2).tolist(),
                is_symbol_show=False,
                linestyle_opts=opts.LineStyleOpts(width=1.5, color="#ff9800"),
                label_opts=opts.LabelOpts(is_show=False),
            )
            .set_global_opts(
                xaxis_opts=opts.AxisOpts(
                    type_="category",
                    axislabel_opts=opts.LabelOpts(rotate=45, font_size=10),
                ),
                yaxis_opts=opts.AxisOpts(
                    type_="value",
                    min_=0,
                    max_=100,
                    splitline_opts=opts.SplitLineOpts(is_show=True, 
                                                      linestyle_opts=opts.LineStyleOpts(opacity=0.2)),
                ),
                legend_opts=opts.LegendOpts(is_show=False),
            )
        )
        return line
    
    def _create_volume_chart(self, df: pd.DataFrame, dates: List[str]) -> Bar:
        """创建成交量图"""
        bar_colors = []
        for idx, row in df.iterrows():
            if row['close'] >= row['open']:
                bar_colors.append("#ef5350")
            else:
                bar_colors.append("#26a69a")
        
        volume = (
            Bar(init_opts=opts.InitOpts(width=self.width))
            .add_xaxis(dates)
            .add_yaxis(
                "成交量",
                df['volume'].round(0).astype(int).tolist(),
                itemstyle_opts=opts.ItemStyleOpts(color=JsCode(f"""
                    function(params) {{
                        var colors = {json.dumps(bar_colors)};
                        return colors[params.dataIndex];
                    }}
                """)),
                label_opts=opts.LabelOpts(is_show=False),
            )
            .set_global_opts(
                xaxis_opts=opts.AxisOpts(
                    type_="category",
                    axislabel_opts=opts.LabelOpts(is_show=False),
                ),
                yaxis_opts=opts.AxisOpts(
                    type_="value",
                    splitline_opts=opts.SplitLineOpts(is_show=True, 
                                                      linestyle_opts=opts.LineStyleOpts(opacity=0.2)),
                ),
                legend_opts=opts.LegendOpts(is_show=False),
            )
        )
        return volume


# ═════════════════════════════════════════════════════════════════════════════
# 导出功能
# ═════════════════════════════════════════════════════════════════════════════

def export_to_html(grid: Grid, output_path: str) -> str:
    """导出为 HTML 文件"""
    grid.render(output_path)
    return output_path


def export_to_image(html_path: str, output_path: str, 
                    width: int = 1200, height: int = 800) -> str:
    """使用 Chrome Headless 将 HTML 转为图片"""
    if not SELENIUM_AVAILABLE:
        raise ImportError("导出图片需要安装 selenium: pip install selenium webdriver-manager")
    
    chrome_options = ChromeOptions()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument(f'--window-size={width},{height}')
    
    driver = webdriver.Chrome(options=chrome_options)
    try:
        driver.get(f"file:///{os.path.abspath(html_path).replace(os.sep, '/')}")
        driver.implicitly_wait(5)
        # 等待图表渲染完成
        import time
        time.sleep(3)
        
        driver.set_window_size(width, height)
        driver.save_screenshot(output_path)
        return output_path
    finally:
        driver.quit()


def check_market_close() -> bool:
    """检查股市是否已收盘 (15:00 后)"""
    now = datetime.now()
    close_time = now.replace(hour=15, minute=0, second=0, microsecond=0)
    # 周末或节假日也返回 True（简化处理）
    return now >= close_time or now.weekday() >= 5


# ═════════════════════════════════════════════════════════════════════════════
# 交互式筛选 (命令行)
# ═════════════════════════════════════════════════════════════════════════════

def interactive_filter() -> FilterCriteria:
    """交互式输入筛选条件"""
    criteria = FilterCriteria()
    
    print("\n===== 股票筛选条件设置 =====")
    print("(直接回车表示不限制该条件)\n")
    
    # 价格范围
    price_input = input("价格范围 (例如: 10-50 或 直接回车跳过): ").strip()
    if price_input:
        parts = price_input.split('-')
        if len(parts) == 2:
            criteria.price_min = float(parts[0].strip()) if parts[0].strip() else None
            criteria.price_max = float(parts[1].strip()) if parts[1].strip() else None
    
    # 涨跌幅
    change_input = input("涨跌幅范围 % (例如: -5~5 或 直接回车跳过): ").strip()
    if change_input:
        parts = change_input.split('~')
        if len(parts) == 2:
            criteria.change_pct_min = float(parts[0].strip()) if parts[0].strip() else None
            criteria.change_pct_max = float(parts[1].strip()) if parts[1].strip() else None
    
    # 成交量
    vol_input = input("最小成交量 (万手, 例如: 100 或 直接回车跳过): ").strip()
    if vol_input:
        criteria.volume_min = float(vol_input)
    
    # 市盈率
    pe_input = input("市盈率范围 (例如: 0-50 或 直接回车跳过): ").strip()
    if pe_input:
        parts = pe_input.split('-')
        if len(parts) == 2:
            criteria.pe_min = float(parts[0].strip()) if parts[0].strip() else None
            criteria.pe_max = float(parts[1].strip()) if parts[1].strip() else None
    
    # 市净率
    pb_input = input("市净率范围 (例如: 0-5 或 直接回车跳过): ").strip()
    if pb_input:
        parts = pb_input.split('-')
        if len(parts) == 2:
            criteria.pb_min = float(parts[0].strip()) if parts[0].strip() else None
            criteria.pb_max = float(parts[1].strip()) if parts[1].strip() else None
    
    # 市值
    cap_input = input("市值范围 (亿元, 例如: 50-500 或 直接回车跳过): ").strip()
    if cap_input:
        parts = cap_input.split('-')
        if len(parts) == 2:
            criteria.market_cap_min = float(parts[0].strip()) if parts[0].strip() else None
            criteria.market_cap_max = float(parts[1].strip()) if parts[1].strip() else None
    
    # 直接指定代码
    codes_input = input("指定股票代码 (逗号分隔, 例如: 000001,600000 或 直接回车跳过): ").strip()
    if codes_input:
        criteria.codes = [c.strip() for c in codes_input.split(',')]
    
    return criteria


# ═════════════════════════════════════════════════════════════════════════════
# 主程序
# ═════════════════════════════════════════════════════════════════════════════

def main():
    """CLI 入口：解析参数、确定筛选条件后批量抓取并生成图表。"""
    parser = argparse.ArgumentParser(description='股票图表生成工具')
    parser.add_argument('--code', type=str, help='指定股票代码 (例如: 000001)')
    parser.add_argument('--codes', type=str, help='指定多个股票代码 (逗号分隔)')
    parser.add_argument('--days', type=int, default=120, help='获取 K 线天数 (默认: 120)')
    parser.add_argument('--output-dir', type=str, default='./stock_charts', help='输出目录')
    parser.add_argument('--export-image', action='store_true', help='同时导出图片')
    parser.add_argument('--no-interactive', action='store_true', help='跳过交互式筛选')
    args = parser.parse_args()
    
    # 检查股市是否收盘
    if not check_market_close():
        print("⚠️  股市尚未收盘 (15:00 后可用), 继续执行可能获取不到当日完整数据")
        confirm = input("是否继续? (y/N): ").strip().lower()
        if confirm != 'y':
            return
    
    # 初始化
    fetcher = StockDataFetcher()
    chart_gen = StockChart()
    os.makedirs(args.output_dir, exist_ok=True)
    
    # 获取筛选条件
    if args.code:
        criteria = FilterCriteria(codes=[args.code])
    elif args.codes:
        criteria = FilterCriteria(codes=[c.strip() for c in args.codes.split(',')])
    elif not args.no_interactive:
        criteria = interactive_filter()
    else:
        criteria = FilterCriteria()
    
    # 获取股票列表
    print("\n正在获取股票列表...")
    stock_list = fetcher.get_stock_list()
    
    # 应用筛选
    filtered = filter_stocks(stock_list, criteria)
    
    if filtered.empty:
        print("没有满足条件的股票")
        return
    
    print(f"\n找到 {len(filtered)} 只符合条件的股票:")
    for _, row in filtered.head(20).iterrows():
        print(f"  {row['code']} {row['name']:8s}  价:{row['price']:>8.2f}  "
              f"涨:{row['change_pct']:>+7.2f}%  PE:{row['pe']:>8.2f}  "
              f"PB:{row['pb']:>6.2f}  市值:{row['market_cap']:>10.2f}亿")
    
    # 生成图表
    print(f"\n开始生成图表 (获取最近 {args.days} 天数据)...")
    for _, stock in filtered.iterrows():
        code = stock['code']
        name = stock['name']
        
        try:
            print(f"  正在处理: {code} {name} ...", end=" ")
            
            # 获取 K 线数据
            kline_df = fetcher.get_kline_data(code, days=args.days)
            if kline_df.empty:
                print("数据为空, 跳过")
                continue
            
            # 生成图表
            chart = chart_gen.create_chart(kline_df, f"{code} {name}", criteria)

            # 股票名称可能含 Windows 非法文件名字符（如 *ST 股的 *），
            # 不清洗会导致导出永远失败
            safe_name = re.sub(r'[\\/:*?"<>|]', '_', name)

            # 导出 HTML
            html_path = os.path.join(args.output_dir, f"{code}_{safe_name}.html")
            export_to_html(chart, html_path)
            print(f"[OK] HTML 已导出: {html_path}")

            # 导出图片
            if args.export_image:
                img_path = os.path.join(args.output_dir, f"{code}_{safe_name}.png")
                export_to_image(html_path, img_path)
                print(f"       [OK] 图片已导出: {img_path}")
                
        except Exception as e:
            print(f"[ERR] 失败: {e}")
            continue
    
    print(f"\n全部完成! 输出目录: {os.path.abspath(args.output_dir)}")


if __name__ == "__main__":
    main()
