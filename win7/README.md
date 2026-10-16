# Win7 版本（PyQt5 + QWebEngineView）

Windows 7 兼容桌面版。由于 **WebView2 不支持 Windows 7**，本版本改用
**PyQt5 + QWebEngineView**（内置 Chromium 引擎）作为渲染窗口，后端仍是同一个 Flask 应用。

## 文件
- `app.py` —— Flask 装配层，注册 `common/routes/` 下的 Blueprint；`launcher_win7.py` 为主启动器（PyQt5 + QWebEngineView，无沙箱/禁用 GPU 以提升 Win7 稳定性）
- `launcher_win7_fast.py` —— 浏览器版启动器（不依赖 Qt，调用系统 Chromium 内核浏览器）
- `build_win7.py` / `build_win7_browser.py` —— 两个打包脚本（onefile / windowed，内联 VC 运行时，禁用 UPX）
- `StockChart_Win7_{32,64}[_browser].spec` —— 由打包脚本按命令行参数自动生成的 PyInstaller 配置
- `templates/` `static/` —— 本版本 UI
- `python38-amd64/` `python38-win32/` —— 打包用的**魔改版** Python 3.8.10（Win7 专用，官方版在 Win7 有 bug 故用 GitHub 社区构建），含预装依赖（flask / numpy / pandas 等），体积大，**已 gitignore**
- `python38-win7/` —— 魔改版 Python **3.8.20** embeddable 的 zip 源包（`python-3.8.20-embed-amd64.zip` / `win32.zip`）及解压目录，可作为升级来源
- `win7_runtime/` —— 构建时从系统 `System32` / `SysWOW64` 复制的 VC 运行时 DLL（`api-ms-win-*`、`msvcp140`、`vcruntime140`、`ucrtbase` 等）
- `WIN7_COMPAT.md` —— 兼容性说明

## 构建
```bash
cd win7
python build_win7.py           # PyQt5/QWebEngine 内置浏览器版（32 + 64 位）
python build_win7_browser.py   # 走系统浏览器的轻量版（32 + 64 位）
```
产物：`dist/StockChart_Win7_{64,32}[_browser]_v<构建日期>.exe`（同名旧版本自动清除），
同时把最新一份以稳定名复制到 `outputs/StockChart_Win7_{64,32}[_browser].exe` 供分发。

| 目标机器 | 用哪个 |
|---|---|
| 32 位 Win7 | `outputs/StockChart_Win7_32.exe` |
| 64 位 Win7 | `outputs/StockChart_Win7_64.exe` |
| Qt 渲染异常 / 内存紧张 | 对应的 `*_browser.exe` |

## 自动更新

变体名即输出名：`StockChart_Win7_{64,32}[_browser]`，由 `build_win7*.py` 写进 `build_info.json`
（打进 EXE 根部）并在 `outputs/<变体>.json` 留侧车。两个启动器都在服务就绪后调用 `common/updater.py`：
界面更新落到 `~/Documents/StockChart/ui/<变体>/`（免重启），EXE 更新走「改名 + 复制 + 重启」的
脱离子进程脚本（`tasklist`/`move`/`copy`/`start` 都是 Win7 原生命令）。

Win7 侧的两点约束：`common/updater.py`、`common/version.py` 必须保持 **Python 3.8 语法**（打包时由
3.8 解释器 import，用了新语法会在构建阶段直接失败）；下载走 HTTPS 且**要求 TLS 1.2**，服务端的
`ssl_protocols TLSv1.2 TLSv1.3` 已满足。

详见根目录 `README.md` 的「自动更新」章节。

## 运行（开发模式）
```bash
cd win7
python launcher_win7.py
```
Flask 在 `127.0.0.1` 随机端口启动，PyQt5 窗口加载该地址。

> 详细兼容方案见 `WIN7_COMPAT.md`。
