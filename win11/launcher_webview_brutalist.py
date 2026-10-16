#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Win11 WebView 桌面启动器 - Brutalist（粗野主义）主题。

薄封装：复用 launcher_base 的通用启动流程（Flask + pywebview），
只指定窗口标题；主题样式由 themes/brutalist 下的模板与静态资源决定。
"""
from launcher_base import launch, die_with_message

if __name__ == '__main__':
    try:
        launch('StockChart - Brutalist')
    except Exception as exc:
        die_with_message(exc)
