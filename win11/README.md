# Win11 版本（WebView2 / pywebview）

Windows 11 桌面版。使用系统自带的 **WebView2（Edge Chromium）** 通过 **pywebview** 渲染界面，
原生窗口体验最佳。支持多主题，并提供「服务器同步」模式（从云端下载数据库）。

## 文件
- `app.py` —— Flask 装配层，注册 `common/routes/` 下的 Blueprint
- `launcher_base.py` + `launcher_webview_{dark,minimalist,highend,brutalist}.py` —— 各主题启动器
- `launcher_webview_sync.py` —— 服务器同步模式（从云端拉取 `stock_data.db`）
- `build_win11_webview.py` —— 唯一打包脚本（uv + PyInstaller，onefile / windowed），同时生成各主题的 `.spec`
- `StockChart_Win11_*.spec` —— 由打包脚本生成的 PyInstaller 配置
- `themes/` —— 各主题的 `templates/` 与 `static/`（Dark / Minimalist / HighEnd / Brutalist）
- `stock_chart.py` `ml_module.py` `preload.py` —— 本版本独有的界面/预测模块

## 构建
```bash
cd win11
python build_win11_webview.py
```
产物：`dist/StockChart_Win11_{HighEnd,Brutalist,Sync}_v<构建日期>.exe`（同名旧版本自动清除），
最新一份同时以稳定名复制到 `outputs/`，并复制到桌面 `StockChart_Win11/`。

## 自动更新

变体名即输出名：`StockChart_Win11_{HighEnd,Brutalist,Sync}`（Dark / Minimalist 已停止发布，主题目录保留），构建时会写进
`build_info.json`（打进 EXE 根部）并在 `outputs/<变体>.json` 留侧车。启动器在 Flask 就绪后调用
`common/updater.py`：界面有新版就落到 `~/Documents/StockChart/ui/<变体>/`（免重启），EXE 有新版就
后台下载、校验 sha256、脱离进程脚本换体并重启。`Sync` 变体的界面源就是 `themes/dark/`。

UI 徽章脚本 `/assets/js/update.js`（仓库根 `assets/js/`，五个主题共用）负责显示进度与手动检查。
详见根目录 `README.md` 的「自动更新」章节。

> 安装位置建议：`%LOCALAPPDATA%\Programs\StockChart\`。exe 所在目录必须可写才能原地替换；
> 若放在 `Program Files`（普通权限不可写），更新器会把新包**暂存**并在状态里提示，不会静默失败。

## 运行（开发模式）
```bash
cd win11
python launcher_webview_dark.py
```
> 注意：打包后的 exe 通过 PyInstaller 将 `themes/<主题>/templates` 映射为 `templates` 目录，
> 因此发行版可正常加载对应主题界面；开发模式直接运行脚本时需保证模板路径正确。
