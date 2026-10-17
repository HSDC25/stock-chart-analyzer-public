# Third-Party Notices

[简体中文](THIRD_PARTY_NOTICES.md) | **English**

This project, "Stock Chart Analysis Tool" (Stock Chart Analyzer, hereinafter "this project"), uses the following
third-party open-source components in its source code and distributed artifacts (including the Windows EXE packaged
with PyInstaller). The copyright of each component belongs to its respective authors, and each is used under the terms
of its own license.

This project itself is released under the **GNU General Public License v3.0 (GPL-3.0)**; the full terms are in the
[LICENSE](LICENSE) file at the repository root.

> Note: the license and version information below was compiled by inspecting the metadata shipped with each component
> (`*.dist-info/METADATA`) in this machine's build environments (`.venv`, `.venv-win7`). **The exact version numbers
> change with the build environment** — refer to `requirements.txt`, `uv.lock` and the actual packaging environment;
> license types generally do not change across minor versions.
> Components marked "(Win7)" appear only in the `win7/` version, and those marked "(Win11)" only in the `win11/` version.

---

## 1. Direct Python Runtime Dependencies

| Component | Typical version | License | Homepage |
|---|---|---|---|
| Flask | 3.1.3 / 3.0.3 | BSD-3-Clause | https://palletsprojects.com/p/flask/ |
| baostock | 0.9.3 / 0.9.2 | BSD License | http://baostock.com/ |
| akshare | 1.18.64 | MIT | https://github.com/akfamily/akshare |
| pyecharts | 2.1.0 | MIT | https://github.com/pyecharts/pyecharts |
| pandas | 3.0.3 / 2.0.3 | BSD-3-Clause | https://pandas.pydata.org/ |
| numpy | 2.5.1 / 1.24.4 | BSD-3-Clause (also 0BSD, MIT, Zlib, CC0-1.0) | https://numpy.org/ |
| requests | 2.34.2 / 2.32.4 | Apache-2.0 | https://requests.readthedocs.io/ |
| certifi | 2026.6.17 | MPL-2.0 | https://github.com/certifi/python-certifi |
| pywebview (Win11) | 6.2.1 | BSD-3-Clause | https://pywebview.flowrl.com/ |

### Optional dependencies (chart export, `chart-export`)

| Component | Version | License | Homepage |
|---|---|---|---|
| selenium | 4.45.0 | Apache-2.0 | https://www.selenium.dev/ |
| webdriver-manager | 4.1.2 | Apache-2.0 | https://github.com/SergeyPirogov/webdriver_manager |

---

## 2. Win7 Desktop Shell Components (Win7 build only, distributed inside the EXE)

> ⚠️ This group is the direct reason the project as a whole adopts **GPL-3.0**: PyQt5 is licensed under GPL-3.0
> (a commercial license from Riverbank is also available; this project does not use it).

| Component | Version | License | Homepage |
|---|---|---|---|
| PyQt5 | 5.15.11 | GPL-3.0 | https://riverbankcomputing.com/software/pyqt/ |
| PyQtWebEngine | 5.15.7 | GPL-3.0 | https://riverbankcomputing.com/software/pyqtwebengine/ |
| PyQt5-sip | 12.15.0 | SIP License (GPL-3.0 / commercial dual) | https://pypi.org/project/PyQt5-sip/ |
| PyQt5-Qt5 | 5.15.2 | LGPL-3.0 (Qt 5 runtime libraries) | https://www.qt.io/ |
| PyQtWebEngine-Qt5 | 5.15.2 | LGPL-3.0 (Qt WebEngine runtime libraries, embedded Chromium) | https://www.qt.io/ |

---

## 3. Python Transitive Dependencies (pulled in by the above, bundled into the EXE)

| Component | License | Component | License |
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
| PySocks | BSD | python-dateutil | BSD-3-Clause (dual) |
| python-dotenv | BSD-3-Clause | pythonnet | MIT |
| pytz (Win7) | MIT | rich | MIT |
| simplejson | MIT OR AFL-2.1 | six | MIT |
| sniffio | MIT OR Apache-2.0 | sortedcontainers | Apache-2.0 |
| soupsieve | MIT | tabulate | MIT |
| tqdm | MPL-2.0 AND MIT | trio | MIT OR Apache-2.0 |
| trio-websocket | MIT | typing_extensions | PSF-2.0 |
| tzdata | Apache-2.0 | urllib3 | MIT |
| wcwidth | MIT | webencodings | BSD |
| websocket-client | Apache-2.0 | Werkzeug | BSD-3-Clause |
| wsproto | MIT | xlrd | BSD |
| importlib_metadata (Win7) | Apache-2.0 | zipp (Win7) | MIT |

---

## 4. Front-End and Static Resources (bundled in each theme's `static/`)

| Component | Version | License | Homepage |
|---|---|---|---|
| Apache ECharts | 5.4.4 | Apache-2.0 | https://echarts.apache.org/ |
| SheetJS Community Edition (`xlsx.full.min.js`) | 0.20.3 | Apache-2.0 | https://sheetjs.com/ |
| HarmonyOS Sans (3 weights: Regular / Medium / Bold) | — | HarmonyOS Sans Fonts License Agreement | https://developer.harmonyos.com/ |

**HarmonyOS Sans font license note** (original text in [assets/fonts/LICENSE.txt](assets/fonts/LICENSE.txt)):
Copyright 2021 Huawei Device Co., Ltd. It permits copying, merging, embedding, bundling and redistributing
**unmodified** copies bundled with software; this project accordingly bundles the font **as-is**, making no
modifications, and makes this prominent notice. The font **must not be distributed or sold on a stand-alone basis**
and must not be modified. Font copyright belongs to Huawei.

---

## 5. Build-Time and Development Tools

> The build tools themselves do not enter the final distributed application code, but **PyInstaller's bootloader is
> embedded into the EXE**, so it is declared here as well.

| Component | Version | License | Notes |
|---|---|---|---|
| PyInstaller | 6.21.0 | GPL-2.0-or-later (with Bootloader Exception) | The special exception explicitly permits building and distributing programs under any license (including closed-source/commercial) |
| pyinstaller-hooks-contrib | 2026.6 | Apache-2.0 | Collection of official PyInstaller hooks |
| altgraph | 0.17.5 | MIT | PyInstaller dependency |
| pefile | 2024.8.26 | MIT | PyInstaller dependency |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | PyInstaller dependency |
| setuptools | 75.3.4 | MIT | Build/packaging |
| hatchling | — | MIT | Build backend (`pyproject.toml`, not distributed with artifacts) |
| pytest | — | MIT | Testing (dev dependency) |
| pytest-cov | — | MIT | Testing (dev dependency) |
| responses | — | Apache-2.0 | Testing (dev dependency) |

---

## 6. License Compatibility and Compliance Notes

1. **The project as a whole adopts GPL-3.0.** This is to stay consistent with the **PyQt5 (GPL-3.0)** bundled in the
   Win7 build — when distributing a binary program that contains PyQt5, the distributor must comply with the relevant
   obligations of GPL-3.0 (providing the corresponding source code, etc.).
2. The **Apache-2.0, MIT, BSD, ISC, MPL-2.0, PSF-2.0, Zlib** and other permissive/weak-copyleft licenses in the tables
   above are **all compatible with GPL-3.0** and may be absorbed and distributed by a GPL-3.0 project (Apache-2.0 is
   compatible with GPL-3.0 but not with GPL-2.0).
3. **Qt runtime libraries** (`PyQt5-Qt5` / `PyQtWebEngine-Qt5`) are licensed under **LGPL-3.0**; this project uses them
   via dynamic linking, in compliance with their terms. The Chromium and other third-party components embedded in Qt
   WebEngine follow their respective BSD-style licenses.
4. The **HarmonyOS Sans font** is distributed bundled with the software in unmodified form under the terms of the font
   license above, and is not redistributed on a stand-alone basis.
5. **Data sources**: market data comes from baostock, akshare and their underlying data sources (such as Sina Finance);
   the copyright of the data itself belongs to the respective data providers and is unrelated to this software's
   open-source license — please observe the relevant data service terms when using it.
6. PyInstaller's **Bootloader Exception** explicitly permits distributing non-free/commercial programs packaged with
   PyInstaller, so even though this project adopts GPL-3.0, packaging and distribution do not constitute a violation of
   the PyInstaller license.

---

## 7. How to Regenerate This List

Run the following in the corresponding virtual environment (`pip-licenses` is not bundled with `pip` and must be
installed separately):

```powershell
# Option 1: install pip-licenses and export in one go
pip install pip-licenses
pip-licenses --format=markdown --with-urls --order=license

# Option 2: read the metadata shipped with each package (the method used for this project)
Get-ChildItem ".venv\Lib\site-packages" -Directory -Filter "*.dist-info" | ForEach-Object {
    $meta = Join-Path $_.FullName "METADATA"
    if (Test-Path $meta) {
        $lic = (Select-String -Path $meta -Pattern '^License(-Expression)?:' | Select-Object -First 1).Line
        "{0}  =>  {1}" -f $_.Name, $lic
    }
}
```

---

_This notice was compiled by the project maintainer and aims to be accurate but does not constitute legal advice; for
commercial use or if you have compliance concerns, please consult a professional._