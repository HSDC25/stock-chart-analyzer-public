# 开源软件声明 / Third-Party Notices

**简体中文** | [English](THIRD_PARTY_NOTICES_EN.md)

本项目「股票图表分析工具」（Stock Chart Analyzer，以下简称"本项目"）在源代码与发行物
（含使用 PyInstaller 打包的 Windows EXE）中使用了下列第三方开源组件。
各组件的版权归其各自作者所有，并按各自的许可证条款授权使用。

本项目自身以 **GNU General Public License v3.0（GPL-3.0）** 发布，完整条款见仓库根目录的 [LICENSE](LICENSE)。

> 说明：下列许可证与版本信息，通过核对本机构建环境（`.venv`、`.venv-win7`）中各组件随包发布的
> 元数据（`*.dist-info/METADATA`）整理得到。**具体版本号会随构建环境变化**，以 `requirements.txt`、
> `uv.lock` 及实际打包环境为准；许可证类型通常不随小版本变化。
> 标注为"（Win7）"的组件仅在 `win7/` 版本中出现，标注为"（Win11）"的仅在 `win11/` 版本中出现。

---

## 一、Python 直接运行时依赖

| 组件 | 典型版本 | 许可证 | 项目主页 |
|---|---|---|---|
| Flask | 3.1.3 / 3.0.3 | BSD-3-Clause | https://palletsprojects.com/p/flask/ |
| baostock | 0.9.3 / 0.9.2 | BSD License | http://baostock.com/ |
| akshare | 1.18.64 | MIT | https://github.com/akfamily/akshare |
| pyecharts | 2.1.0 | MIT | https://github.com/pyecharts/pyecharts |
| pandas | 3.0.3 / 2.0.3 | BSD-3-Clause | https://pandas.pydata.org/ |
| numpy | 2.5.1 / 1.24.4 | BSD-3-Clause（并含 0BSD、MIT、Zlib、CC0-1.0） | https://numpy.org/ |
| requests | 2.34.2 / 2.32.4 | Apache-2.0 | https://requests.readthedocs.io/ |
| certifi | 2026.6.17 | MPL-2.0 | https://github.com/certifi/python-certifi |
| pywebview （Win11） | 6.2.1 | BSD-3-Clause | https://pywebview.flowrl.com/ |

### 可选依赖（图表导出，`chart-export`）

| 组件 | 版本 | 许可证 | 项目主页 |
|---|---|---|---|
| selenium | 4.45.0 | Apache-2.0 | https://www.selenium.dev/ |
| webdriver-manager | 4.1.2 | Apache-2.0 | https://github.com/SergeyPirogov/webdriver_manager |

---

## 二、Win7 桌面壳组件（仅 `win7/` 构建，随 EXE 分发）

> ⚠️ 这一组是本项目整体采用 **GPL-3.0** 的直接原因：PyQt5 采用 GPL-3.0 授权
> （另有 Riverbank 商业授权可选，本项目未使用）。

| 组件 | 版本 | 许可证 | 项目主页 |
|---|---|---|---|
| PyQt5 | 5.15.11 | GPL-3.0 | https://riverbankcomputing.com/software/pyqt/ |
| PyQtWebEngine | 5.15.7 | GPL-3.0 | https://riverbankcomputing.com/software/pyqtwebengine/ |
| PyQt5-sip | 12.15.0 | SIP License（GPL-3.0 / 商业双授权） | https://pypi.org/project/PyQt5-sip/ |
| PyQt5-Qt5 | 5.15.2 | LGPL-3.0（Qt 5 运行库） | https://www.qt.io/ |
| PyQtWebEngine-Qt5 | 5.15.2 | LGPL-3.0（Qt WebEngine 运行库，内置 Chromium） | https://www.qt.io/ |

---

## 三、Python 传递依赖（由上述组件带入，随 EXE 一并打包）

| 组件 | 许可证 | 组件 | 许可证 |
|---|---|---|---|
| attrs | MIT | beautifulsoup4 | MIT |
| blinker | MIT | bottle | MIT |
| cffi | MIT-0 | charset-normalizer | MIT |
| click | BSD-3-Clause | clr-loader | MIT |
| colorama | BSD-3-Clause | curl_cffi | MIT |
| decorator | BSD-2-Clause | et_xmlfile | MIT |
| h11 | MIT | html5lib | MIT |
| idna | BSD-3-Clause | itsdangerous | BSD-3-Clause |
| Jinja2 | BSD-3-Clause | jsonpath | MIT |
| lxml | BSD-3-Clause | markdown-it-py | MIT |
| MarkupSafe | BSD-3-Clause | mdurl | MIT |
| mini-racer | ISC | openpyxl | MIT |
| outcome | MIT OR Apache-2.0 | packaging | Apache-2.0 OR BSD-2-Clause |
| prettytable | BSD-3-Clause | proxy-tools | MIT |
| pycparser | BSD-3-Clause | Pygments | BSD-2-Clause |
| PySocks | BSD | python-dateutil | BSD-3-Clause（双许可） |
| python-dotenv | BSD-3-Clause | pythonnet | MIT |
| pytz （Win7） | MIT | rich | MIT |
| simplejson | MIT OR AFL-2.1 | six | MIT |
| sniffio | MIT OR Apache-2.0 | sortedcontainers | Apache-2.0 |
| soupsieve | MIT | tabulate | MIT |
| tqdm | MPL-2.0 AND MIT | trio | MIT OR Apache-2.0 |
| trio-websocket | MIT | typing_extensions | PSF-2.0 |
| tzdata | Apache-2.0 | urllib3 | MIT |
| wcwidth | MIT | webencodings | BSD |
| websocket-client | Apache-2.0 | Werkzeug | BSD-3-Clause |
| wsproto | MIT | xlrd | BSD |
| importlib_metadata （Win7） | Apache-2.0 | zipp （Win7） | MIT |

---

## 四、前端与静态资源（直接内置于各主题的 `static/`）

| 组件 | 版本 | 许可证 | 项目主页 |
|---|---|---|---|
| Apache ECharts | 5.4.4 | Apache-2.0 | https://echarts.apache.org/ |
| SheetJS Community Edition（`xlsx.full.min.js`） | 0.20.3 | Apache-2.0 | https://sheetjs.com/ |
| HarmonyOS Sans（3 个字重：Regular / Medium / Bold） | — | HarmonyOS Sans Fonts License Agreement | https://developer.harmonyos.com/ |

**HarmonyOS Sans 字体许可说明**（原文见 [assets/fonts/LICENSE.txt](assets/fonts/LICENSE.txt)）：
Copyright 2021 Huawei Device Co., Ltd.。允许以**未修改**的形式随软件复制、合并、内嵌、打包、再分发；
本项目据此**原样内置**该字体，未做任何修改，并在此作出显著声明。
该字体**不得单独分发或销售**，也不得修改。字体版权归华为所有。

---

## 五、构建期与开发工具

> 构建工具本身不进入最终分发的业务代码，但 **PyInstaller 的 bootloader 会被内嵌进 EXE**，
> 故一并声明。

| 组件 | 版本 | 许可证 | 说明 |
|---|---|---|---|
| PyInstaller | 6.21.0 | GPL-2.0-or-later（含 Bootloader Exception） | 特殊例外条款明确允许用它构建并以任意许可证（含闭源/商业）分发程序 |
| pyinstaller-hooks-contrib | 2026.6 | Apache-2.0 | PyInstaller 官方 hooks 集合 |
| altgraph | 0.17.5 | MIT | PyInstaller 依赖 |
| pefile | 2024.8.26 | MIT | PyInstaller 依赖 |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | PyInstaller 依赖 |
| setuptools | 75.3.4 | MIT | 构建/打包 |
| hatchling | — | MIT | 构建后端（`pyproject.toml`，未随产物分发） |
| pytest | — | MIT | 测试（开发依赖） |
| pytest-cov | — | MIT | 测试（开发依赖） |
| responses | — | Apache-2.0 | 测试（开发依赖） |

---

## 六、许可证兼容性与合规说明

1. **本项目整体采用 GPL-3.0**。这是为了与 Win7 版所捆绑的 **PyQt5（GPL-3.0）** 保持一致——
   分发含 PyQt5 的二进制程序时，分发者必须遵守 GPL-3.0 的相应义务（提供对应源代码等）。
2. 上表中的 **Apache-2.0、MIT、BSD、ISC、MPL-2.0、PSF-2.0、Zlib** 等宽松/弱著佐权许可证
   **均与 GPL-3.0 兼容**，可被 GPL-3.0 项目吸收与分发（Apache-2.0 兼容 GPL-3.0，但不兼容 GPL-2.0）。
3. **Qt 运行库**（`PyQt5-Qt5` / `PyQtWebEngine-Qt5`）以 **LGPL-3.0** 授权，本项目以动态链接方式使用，
   符合其条款；Qt WebEngine 内含的 Chromium 等第三方组件遵循其各自的 BSD 类许可。
4. **HarmonyOS Sans 字体**按上述字体许可条款以未修改形式随软件分发，不单独再分发。
5. **数据来源**：行情数据来自 baostock、akshare 及其背后数据源（如新浪财经等），
   数据本身的版权归各数据提供方所有，与本软件的开源许可证无关，使用时请自行遵守相关数据服务条款。
6. PyInstaller 的 **Bootloader Exception** 明确允许将非自由/商业程序以 PyInstaller 打包发布，
   故本项目即便采用 GPL-3.0，打包与分发均不构成对 PyInstaller 许可证的违反。

---

## 七、如何重新生成本清单

在对应虚拟环境中执行（`pip` 附带的 `pip-licenses` 需单独安装）：

```powershell
# 方式一：安装 pip-licenses 后一键导出
pip install pip-licenses
pip-licenses --format=markdown --with-urls --order=license

# 方式二：直接读取每个包随附的元数据（本项目使用的方式）
Get-ChildItem ".venv\Lib\site-packages" -Directory -Filter "*.dist-info" | ForEach-Object {
    $meta = Join-Path $_.FullName "METADATA"
    if (Test-Path $meta) {
        $lic = (Select-String -Path $meta -Pattern '^License(-Expression)?:' | Select-Object -First 1).Line
        "{0}  =>  {1}" -f $_.Name, $lic
    }
}
```

---

_本声明由项目维护者整理，力求准确但不构成法律意见；如需商用或有合规疑虑，请咨询专业法律人士。_