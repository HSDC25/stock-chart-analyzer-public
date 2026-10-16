#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地命令行预加载触发器（一次性工具，收盘后手动或计划任务运行）。

向本机 Flask 服务（app.py，通常由桌面壳启动）的 /api/preload 接口发
POST，让服务端在后台把 K 线数据预取到本地 SQLite；预载后打开界面
秒级出图。服务端开启了 CSRF 保护，所以先用同一会话 GET token 再带上
它 POST。

用法: python preload.py [--host <url>]
"""

import sys
import requests
import argparse
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description='收盘后自动预加载 K 线数据')
    parser.add_argument('--host', default='http://127.0.0.1:5000', help='Flask 服务地址')
    args = parser.parse_args()
    
    url = f"{args.host.rstrip('/')}/api/preload"

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始预加载 K 线数据...")
    print(f"目标地址: {url}")

    try:
        # 服务端启用了 CSRF 保护：先用同一会话 GET token，再带上它 POST
        session = requests.Session()
        token_resp = session.get(
            f"{args.host.rstrip('/')}/api/csrf-token", timeout=10)
        token_resp.raise_for_status()
        csrf_token = token_resp.json().get('csrf_token', '')
        if not csrf_token:
            print("预加载失败: 无法获取 CSRF token")
            sys.exit(1)

        response = session.post(
            url, timeout=3600,  # 最多等1小时
            headers={'X-CSRF-Token': csrf_token})
        response.raise_for_status()
        data = response.json()
        
        if data.get('success'):
            print(f"预加载完成: 总计 {data.get('total', 0)} 只, 成功 {data.get('loaded', 0)} 只, 失败 {data.get('failed', 0)} 只")
        else:
            print(f"预加载失败: {data.get('error', '未知错误')}")
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(f"连接失败: 请确保 Flask 服务已在 {args.host} 运行")
        sys.exit(1)
    except KeyboardInterrupt:
        print("用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"预加载异常: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
