# stock-chart-analyzer（股票图表分析工具）

**简体中文** | [English](README_EN.md)

A 股股票图表分析工具：基于 **Flask + ECharts** 的 Web 应用，抓取 A 股行情（baostock / 新浪接口），
渲染 K 线 + 技术指标图表，内置 **AI 预测模块**（`ml_module`）。可打包为 Windows 桌面程序。

因运行环境不同，分为两个独立可构建的版本：

| 版本 | 目录 | 渲染方式 | 打包 Python | 说明 |
|------|------|----------|-------------|------|
| **Win7** | `win7/` | PyQt5 + QWebEngineView（内置 Chromium） | GitHub 社区**魔改**的 Python 3.8 embeddable（官方版在 Win7 下有 bug） | WebView2 不支持 Win7，回退到浏览器内核 |
| **Win11** | `win11/` | 系统 WebView2（Edge Chromium）/ pywebview | uv + Python 3.13 | 在维护的发布变体：HighEnd / Brutalist / Sync（主题目录仍含 Dark / Minimalist） |

## 目录结构

```
stock-chart-analyzer/
├── common/      # 两个版本共享的后端包（config/db/security/preload/models/routes/updater）
├── win7/        # Win7 桌面版（PyQt5/QWebEngine），启动器 + 模板 + 构建脚本
├── win11/       # Win11 桌面版（WebView2），启动器 + themes/ 多主题 + 构建脚本
├── assets/      # 所有主题共享的资源（fonts/ 字体、js/ 更新徽章脚本）
├── scripts/     # 构建期/发布期工具（update_lib、publish_update、签名脚本）
├── tests/       # pytest 用例
├── docs/        # 设计说明、产品说明书
├── LICENSE      # 项目许可证（GPL-3.0）
├── README_EN.md                # 英文版 README
├── THIRD_PARTY_NOTICES.md      # 开源软件声明（中文）
├── THIRD_PARTY_NOTICES_EN.md   # 开源软件声明（英文）
├── requirements.txt
├── pyproject.toml
└── uv.lock
```

## 构建

| 版本 | 命令 | 产物 |
|------|------|------|
| Win7 | `python win7/build_win7.py` | `win7/dist/StockChart_Win7_{64,32}_v<日期>.exe` |
| Win7（浏览器版） | `python win7/build_win7_browser.py` | `win7/dist/StockChart_Win7_{64,32}_browser_v<日期>.exe` |
| Win11 | `python win11/build_win11_webview.py` | `win11/dist/StockChart_Win11_{HighEnd,Brutalist,Sync}_v<日期>.exe` |

命名规则：`dist/` 里的产物带构建日期版本号（`_v2026.10.04`），同名旧版本会在下次构建时清除；
`outputs/` 里始终是**稳定名**（无版本号）的最新一份，用于分发和安装。`<日期>` 取构建当天，由脚本自动生成。

> Win7 构建前需在本机准备好 `python38-amd64`、`python38-win32` 运行时目录（见 `win7/WIN7_COMPAT.md`）。
> 注意：这两套是 **GitHub 社区魔改的 Python 3.8.10 embeddable**（官方版在 Win7 下有 bug，无法运行），已自带 `vcruntime140.dll` / `sqlite3.dll` 与预装依赖，**并非** python.org 官方包。
> `win7/python38-win7/` 另存有魔改版 **3.8.20** embeddable 的 zip 源包，可作为升级来源（当前 `build_win7.py` 仍用 3.8.10）。
> 这些运行时目录体积较大，已加入 `.gitignore`，不会提交到仓库。
> Win7 打包必须带 `--paths <仓库根>` 与 `common.*` 的 hidden-import（脚本已内置）；否则 exe 启动即 `ModuleNotFoundError: No module named 'common'`。
> Win11 打包必须带 `--exclude-module cefpython3`（脚本已内置）：pywebview 的 PyInstaller hook 会把 CEF 后端一并收进来（解压后 108 MB，占原体积近一半），而 onefile 解压 `cefpython3\chrome_elf.dll` 时会被杀软偶发拦停，症状是启动只弹一个标题为 `Error` 的框、`stockchart.log` 一行不写——那是 bootloader 解压失败，不是应用错误。

## 自动更新

桌面版启动后会在后台检查 `https://data.hsdc.cloud/update.json`，命中更新时**全自动完成，全程不弹任何确认框**
（Win7 侧的用户是老人，弹窗只会卡住流程）：界面热更直接把正在服务的 Flask 指向新覆盖层、`update.js` 自动刷新页面，
进程不重启、端口和浏览器标签页都不变；本体下载完则自动换体重启。
万一走到「已下载待重启」的兜底状态，前端也会在徽章亮出后 4 秒自动触发换体，同样不等点击。
更新分两个粒度，由 `common/updater.py` 实现：

| 粒度 | 大小 | 是否重启 | 说明 |
|------|------|----------|------|
| **界面覆盖层（UI）** | 约 680 KB | 不需要 | `templates/ + static/ + assets/js` 打成 zip，解压到 `~/Documents/StockChart/ui/<变体>/`，比 EXE 内置副本**更新**时优先使用；启动器把活着的 Flask 交给 `updater`（`use_overlay` 里顺带登记），热更时 `repoint_overlay()` 原地换指向 |
| **程序本体（EXE）** | 180 MB（win11）/ 66–150 MB（win7） | 需要 | 运行中的映像不能覆盖但能改名：拉起一个隐藏窗口的 `cmd` 执行 `.cmd`，等旧进程消失后 `move` 旧 exe → `copy` 新 exe → `start`，失败自动回滚，脚本自删除 |

> **界面热更的原地换指向必须改三处，少一处页面就还是旧的**（2026-10-05 在 Flask 3.1.2 与 win7 运行时的 3.0.3 上实测）：
> 只改 `app.template_folder` 无效，因为 `jinja_loader` 是 `cached_property`，算过一次就不再重算；
> 只换 `app.jinja_loader = FileSystemLoader(...)` 也无效，因为 Jinja 还握着编译好的模板；
> 再 `app.jinja_env.cache.clear()` 才真正翻页。`app.static_folder` 是普通属性，改完 `/static/...` 立即走新目录。
> `/assets/js` 那份由 `assets_routes` 每次请求现查覆盖层，天然热生效。
> 三处齐做的回归测试见 `tests/test_updater.py::test_repoint_overlay_switches_a_running_flask` 与
> `test_run_update_hot_repoints_live_app_without_restart`，端到端（真 socket + 断言 PID 不变）见
> `test_output/hotrepoint_live.py`。
> **但已装机的老 EXE 里跑的是老 updater**，它没有这条热路径：吃到界面热更时仍会调用换体脚本、自己退出却拉不起新进程
> （脚本坏的这批）——用户重开一次图标就好，之后覆盖层已在启动时被 `use_overlay` 接上，不再重复退出。

> **换体脚本必须以 `CREATE_NO_WINDOW` 启动，不能用 `DETACHED_PROCESS`**（2026-10-05 实测）：
> `DETACHED_PROCESS` 给出的 `cmd` 没有控制台，脚本执行到第一处 `>nul` 重定向就整体中断，且没有任何错误输出
> ——症状是「下载完成、进程退出、但 exe 纹丝不动、`.cmd` 留在原地」。`CREATE_NO_WINDOW` 会分配一个隐藏控制台，
> 既不闪窗口又能正常重定向。回归测试见 `tests/test_updater.py::test_launch_bat_really_swaps_the_exe`（真跑一次换体）。
> **因此 2026-10-05 之前发布的 EXE 里这条换体路径从来没成功过**：它们的 `version` 不能再抬高，否则客户端会
> 反复下载 171 MB、退出、再下载。这批用户只能拿到界面热更（`ui_version`），要恢复自动换体需手动重装一次新构建。
> 线上清单因此把七个变体的 `version` 压在 `2026.10.04`（= 已安装版本，触发不了本体下载）、只把 `ui_version`
> 抬到 `2026.10.05`；修好的 EXE 以 `_v2026.10.05.exe` 同名上传，作为「手动重装」的下载源。
> 2026-10-05 真机验证：`test_output/e2e_swap_test.py` 两种模式都跑通（那一轮 EXE 里界面热更还是「退出+重启」，
> 因为热切换是同日稍后才实现的），判据是沙箱 EXE 的 sha 变成新版本包、`.old` 出现、脚本自删除、
> 新进程起来后 `/api/update/status` 报 `overlay_active=true`。

版本号是**构建日期** `YYYY.MM.DD`（构建脚本写进 `build_info.json` 并打进 EXE 根部，`common/version.py`
读取）；源码运行时报 `0.0.0` / `dev`，因此不会误用覆盖层。发布脚本会为每个变体在 `outputs/<变体>.json`
留一份侧车，记录版本与产物路径。

### 安全约束

* 清单与所有包体**只走 HTTPS**（仅 `127.0.0.1` 允许明文，作为测试钩子），数据库那条 IP 直连的 HTTP 通道不用于代码分发。
* `update.json` 里 `sha256` 是**必填**，缺失或不匹配直接失败，不存在"降级为不校验"。
* 解压拒绝绝对路径与 `..`（zip-slip）；覆盖层切换用「临时目录 → 备份 → 原子改名」，任一步失败都不会留下半个界面。
* 环境变量：`STOCKCHART_UPDATE_URL` 换清单地址，`STOCKCHART_DISABLE_AUTO_UPDATE=1` 关掉自动检查，
  `STOCKCHART_UI_OVERLAY=1` 让源码运行也吃覆盖层（仅调试用）。

### 发布流程

```bash
# 1. 构建（dist/ 出带日期的 exe，outputs/ 出稳定名 + 侧车 json）
python win11/build_win11_webview.py
python win7/build_win7.py
python win7/build_win7_browser.py

# 2. （可选）代码签名，见下节
powershell -File scripts/sign_executables.ps1

# 3. 生成 update.json + 各变体 UI zip，并上传到服务器
python scripts/publish_update.py --notes "本次更新说明"
python scripts/publish_update.py --scp user@<your-server>:/var/www/stockdata/updates/
```

`publish_update.py` 拒绝非 HTTPS 的 `--base-url`。服务器侧 nginx 需要两条 location：
`= /update.json` 与 `/updates/`（见 `.deploy/stockdata.ssl`，`.deploy/` 是本机部署副本、不入库）。
上传完成后客户端下一次轮询即可发现，无需重启服务。

> **上传顺序**：先传 exe 与 ui.zip，确认每个 URL 都能 200 且 sha 对得上，**最后**再传 `update.json`。
> 清单是客户端唯一的触发点，先传清单会让客户端拿到指向缺失文件的 URL。
> **不要删除服务器上已发布的旧版 EXE**：`update.json` 只描述最新版本，但已经自动更新到某个旧版本的
> 客户端，日后升级时下载的是清单里那条记录的包名；如果历史包被清掉，它就再也更不动了。
> 磁盘留存量按「每个已发布版本 × 7 个变体（win7 四个 + win11 三个）」累计，清理前先确认没有客户端仍指向它。
> 已下线的变体（`StockChart_Win11_Dark`、`StockChart_Win11_Minimalist`）不再出现在清单里，它们的历史包保留在
> `/updates/` 供已安装用户继续跑，但不会再收到更新。
>
> 两个会让你白传一遍 200MB 的细节（2026-10-05 踩过）：
>
> * **签名和构建都不能还在跑**。`publish_update.py` 记录的是「运行那一刻」文件的 sha，签名会改字节、
>   后台构建会覆盖 `dist/` 里同名产物。必须等构建彻底结束 → 签名 → 再生成清单，否则清单指向的字节
>   和服务器上的包不一致，客户端直接拒收。上传完务必在服务器 `sha256sum` 对一遍再传清单。
> * **UI zip 每次重新生成都换 sha**（包里 `build_info.json` 带 `build_time`）。所以清单一旦重生成，
>   七个 zip 就得全部重传，不能只补改动的 EXE。
> * 纯前端改动（只动 `templates/`、`static/`、`assets/js`）不必重打 EXE：清单里把该变体的
>   `ui_version` 单独抬到新版本、`version` 保持不动即可，客户端只下几百 KB 覆盖层、页面刷新生效。
>   反过来，`version` 一旦抬高就必须给出真实更新的 EXE，否则换体重启后版本号仍不变，会反复换体。
> * **同一天里第二次发界面包必须抬位**：`python scripts/publish_update.py --ui-version 2026.10.05.2`。
>   客户端拿清单的 `ui_version` 与本地 `overlay_version` 比大小，沿用当天已经发过的 `2026.10.05`
>   等于没发（已装过那份覆盖层的客户端永远不会再下）。这个值会同时写进清单和包内 `build_info.json`，
>   两者必须是同一个，否则装完再比仍算「有更新」，每轮重复下载几百 KB。回归测试见
>   `tests/test_updater.py::test_same_day_ui_republish_needs_a_bumped_ui_version`。

### 证书

* **更新服务器的 TLS**：`data.hsdc.cloud` 用 Let's Encrypt，certbot webroot 方式自动续期。
  2026-10-04 已核验并修好续期链路：服务器上真正能用的是 **snap 版 certbot**（`/snap/bin/certbot`，5.8.0，
  认得 `/etc/letsencrypt` 里的证书）；原先 `/etc/cron.d/certbot-renew` 指向的 `/opt/certbot-env/bin/certbot`
  是在 Windows 上建好拷过去的 venv，shebang 指向 `/tmp/certbot-env/bin/python`，在 Linux 上根本执行不了，
  等于一直在空跑。现该 cron 改为调用 snap certbot，并新增
  `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh`，保证 snap 自带的 `certbot.renew.timer`
  续期后 nginx 也会立刻重载（否则内存里仍是旧证书）。`renew --dry-run --run-deploy-hooks` 全部通过。
  本机 `.deploy/` 目录是**部署副本且已 gitignore**，改完要手动 scp 到服务器。
* **EXE 代码签名**：不存在"免费且被公开信任"的代码签名 CA。本项目采用**自建自签证书 + 时间戳**的免费方案：

  ```powershell
  .\scripts\new_signing_cert.ps1      # 生成/复用自签证书（RSA3072/SHA256，3 年），导出 pfx + cer
  .\scripts\sign_executables.ps1      # 对 win7/win11 的 outputs/*.exe 签名并校验
  .\scripts\trust_signing_cert.ps1    # 在目标机（管理员）把 cer 装入 Root + TrustedPublisher
  ```

  证书剩余有效期不足 30 天时 `new_signing_cert.ps1` 会自动重签（`.cer` 公钥需重新分发一次）。
  **必须说清的代价**：自签证书不在系统信任链里，Windows 10/11 的 SmartScreen 仍会拦一道"未知发布者"，
  需要用户在属性里点"仍然运行"或预先分发 `.cer`；企业分发或彻底消除警告只能买商业代码签名证书。
  `signing/`、`*.pfx`、`*.cer` 已加入 `.gitignore`，私钥绝不入库（PFX 密码存 `signing/pfx_password.txt`，
  同样不入库，可自行迁到 Windows 凭据管理器）。
  两个坑：`.ps1` 必须存成**带 BOM 的 UTF-8**，否则 Windows PowerShell 5.1 按 GBK 解析中文注释直接语法报错；
  `-Paths` 是数组参数，从 Git-Bash 调用要走 `powershell -Command "& .\scripts\sign_executables.ps1 -Paths @('a','b') -VerifyOnly"`，
  用 `-File` 传逗号串会被当成单个路径。签名后本机 `verify /pa` 必然报「root certificate not trusted」，
  脚本已把这种情况单独计为「已签名/未受信」，跑 `trust_signing_cert.ps1` 后才会全绿。

## 说明
- 后端已抽取为根目录 `common/` 包，`win7/app.py` 与 `win11/app.py` 只做装配并注册
  `common/routes/` 下的 Blueprint；两个版本各自保留启动器、UI 资源与打包脚本。
- 本仓库为开源公开仓库，欢迎提交 issue 与 PR。

## 许可证

- 本项目以 **GNU General Public License v3.0（GPL-3.0）** 发布，完整条款见 [LICENSE](LICENSE)。
- 采用 GPL-3.0 是因为 Win7 版捆绑了以 GPL-3.0 授权的 **PyQt5 / PyQtWebEngine**；
  若需以非 GPL 方式分发（闭源或商用），须自备 Riverbank 的 PyQt 商业授权。
- 项目使用的全部第三方开源组件及其许可证（Python 依赖、Win7 桌面壳、前端库、字体、构建工具），
  统一记录在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)（中文）/ [THIRD_PARTY_NOTICES_EN.md](THIRD_PARTY_NOTICES_EN.md)（English）。
- 英文版说明文档见 [README_EN.md](README_EN.md)。
