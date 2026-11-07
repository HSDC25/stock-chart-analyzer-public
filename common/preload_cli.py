"""本地命令行预加载触发器（win7 / win11 共用）。

向本机 Flask 服务的 ``/api/preload`` 发 POST，让服务端在后台把 K 线数据
预取到本地 SQLite；预载后打开界面秒级出图。服务端开了 CSRF 保护，所以先用
同一会话 GET token 再带上它 POST。

两个变体目录下的 ``preload.py`` 只是薄壳（``python win7/preload.py`` 这类
用法与文档都保持不变），逻辑收在这里一份，避免两边各改各的。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

import requests


def main(argv=None):
    # type: (list | None) -> int
    parser = argparse.ArgumentParser(description='收盘后自动预加载 K 线数据')
    parser.add_argument('--host', default='http://127.0.0.1:5000', help='Flask 服务地址')
    args = parser.parse_args(argv)
    base = args.host.rstrip('/')

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始预加载 K 线数据...")
    print(f"目标地址: {base}/api/preload")

    try:
        # 服务端启用了 CSRF 保护：先用同一会话 GET token，再带上它 POST
        session = requests.Session()
        token_resp = session.get(f"{base}/api/csrf-token", timeout=10)
        token_resp.raise_for_status()
        csrf_token = token_resp.json().get('csrf_token', '')
        if not csrf_token:
            print("预加载失败: 无法获取 CSRF token")
            return 1

        response = session.post(
            f"{base}/api/preload", timeout=3600,  # 最多等 1 小时
            headers={'X-CSRF-Token': csrf_token})
        response.raise_for_status()
        data = response.json()

        if data.get('success'):
            print(f"预加载完成: 总计 {data.get('total', 0)} 只, "
                  f"成功 {data.get('loaded', 0)} 只, 失败 {data.get('failed', 0)} 只")
            return 0
        print(f"预加载失败: {data.get('error', '未知错误')}")
        return 1
    except requests.exceptions.ConnectionError:
        print(f"连接失败: 请确保 Flask 服务已在 {args.host} 运行")
        return 1
    except KeyboardInterrupt:
        print("用户中断")
        return 0
    except Exception as e:
        print(f"预加载异常: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
