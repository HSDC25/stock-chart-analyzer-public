# Win7 兼容方案说明

## 问题

- **WebView2 不支持 Windows 7**，Win11 版「pywebview + 系统 WebView2」的路线在 Win7 上无法使用。
- Win7 上可用的 Chromium 内核浏览器也都已停止更新（Chrome / Edge 的内核停在 109）。

## 解决方案

Win7 版提供两条外壳，**默认走浏览器壳**：

| 外壳 | 启动器 | 产物 | 做法 |
|---|---|---|---|
| **浏览器壳（推荐）** | `launcher_win7_fast.py` | `StockChart_Win7_{32,64}_browser.exe` | 起好 Flask 后，用**机内已安装的 Chromium 内核浏览器**打开引导页 |
| **内置窗口壳（备选）** | `launcher_win7.py` | `StockChart_Win7_{32,64}.exe` | 用 PyQt5 + QWebEngineView（自带 Chromium 内核）显示为独立窗口，不依赖任何外部浏览器 |

两条外壳的后端是同一个 Flask 应用（`app.py` 装配 `common/routes/` 下的 Blueprint）。

### 浏览器壳如何挑选浏览器

`find_browser()` 按三级顺序探测，命中第一个真实存在的可执行文件即用：

1. **常见安装目录**：`%ProgramFiles%` / `%ProgramFiles(x86)%` / `%LOCALAPPDATA%` 下的
   Supermium → Chromium → Brave → Chrome → Vivaldi → Opera → Firefox → Edge，
   外加桌面、用户目录与 `D:` / `E:` 盘下的 Supermium 便携目录；
2. **注册表** `SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\` 下的
   `Supermium.exe` / `chrome.exe` / `brave.exe` / `msedge.exe`（HKLM 与 HKCU）；
3. **开始菜单快捷方式**中匹配 `*supermium*` 的 `.lnk`（会把扩展名换成 `.exe` 并校验目标真实存在，
   否则 `Popen` 会直接抛 `FileNotFoundError`）。

命中 Chromium 内核浏览器时附加 `--no-sandbox --disable-gpu --disable-gpu-sandbox
--disable-software-rasterizer`（Win7 上更稳）；一个都没找到时，先弹窗提示
「建议安装 Supermium 或 Chrome」，再退回系统默认浏览器打开。

> **推荐安装 [Supermium](https://www.supermium.net/)**（[源码 / 发布页](https://github.com/win32ss/supermium)）：
> 仍在为 Win7 跟进上游安全补丁的 Chromium 分支，支持 Manifest V2（uBlock Origin 等扩展可长期用）。
> 装好后无需任何配置，浏览器壳会优先选中它。

## 文件说明

| 文件 | 说明 |
|------|------|
| `launcher_win7_fast.py` | **浏览器壳启动器（推荐）**，不依赖 Qt |
| `launcher_win7.py` | 内置窗口壳启动器（备选，PyQt5 + QWebEngineView） |
| `StockChart_Win7_{32,64}_browser.spec` | 浏览器壳的 PyInstaller 配置（入口 `launcher_win7_fast.py`） |
| `StockChart_Win7_{32,64}.spec` | 内置窗口壳的 PyInstaller 配置（入口 `launcher_win7.py`） |
| `build_win7_browser.py` / `build_win7.py` | 两个打包脚本（onefile / windowed，内联 VC 运行时，禁用 UPX） |
| `preload.py` / `preload.bat` | 收盘后手动（或计划任务）触发 K 线数据预载的小工具，需本机 Flask 服务已在运行 |
| `python38-amd64/` `python38-win32/` | 打包用的**魔改版** Python 3.8.10 embeddable 运行时，**已 gitignore** |
| `python38-win7/` | 魔改版 **3.8.20** embeddable 的 zip 源包，可作升级来源，**已 gitignore** |
| `win7_runtime/` | 构建时从系统 `System32` / `SysWOW64` 复制的 VC 运行时 DLL |

## 构建前置条件

打包前必须在本机准备好 `python38-amd64/` 与 `python38-win32/` 两套运行时目录。它们不是 python.org 官方包，
而是 **GitHub 社区魔改的 Python 3.8.10 embeddable**（官方版在 Win7 下有 bug），已自带
`vcruntime140.dll` / `sqlite3.dll` 与预装依赖；体积较大，已加入 `.gitignore`。
`win7/python38-win7/` 另存有魔改版 **3.8.20** embeddable 的 zip 源包（当前 `build_win7.py` 仍用 3.8.10）。

```bash
cd win7
python build_win7_browser.py   # 浏览器壳（推荐；32 + 64 位）
python build_win7.py           # 内置窗口壳（备选；32 + 64 位）
```

产物：`dist/StockChart_Win7_{64,32}[_browser]_v<构建日期>.exe`，并把最新一份以稳定名复制到
`outputs/StockChart_Win7_{64,32}[_browser].exe` 供分发。

## 用户使用方法

1. 双击运行对应的 exe（**推荐 `StockChart_Win7_*_browser.exe`**）；
2. 浏览器壳会自动打开机内 Chromium 内核浏览器并跳到引导页，数据准备进度在页面里实时显示；
3. 若机器上完全没有 Chromium 内核浏览器，可改用内置窗口壳的 exe，或先安装 Supermium。

## 注意事项

- 浏览器壳的界面开在浏览器里，不是独立窗口；内置窗口壳才是独立窗口。
- 浏览器壳依赖机内已安装的 Chromium 内核浏览器，**Win7 上推荐 Supermium**。
- AI 预测功能两条外壳都正常可用。
- 更新行为与 Win11 版一致：界面走覆盖层热更（免重启、浏览器标签页不变），EXE 走换体重启；
  详见根目录 `README.md` 的「自动更新」章节。
