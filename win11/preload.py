#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预加载命令行入口（薄壳）。

实现见 ``common/preload_cli.py``：win7 / win11 两个目录曾经各存一份完全相同
的脚本，改一处漏一处；这里只保留用户熟悉的调用路径：
    python preload.py [--host http://127.0.0.1:5000]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.preload_cli import main

if __name__ == '__main__':
    sys.exit(main())
