# stock-chart-analyzer (Stock Chart Analysis Tool)

**English** | [简体中文](README.md)

An A-share stock chart analysis tool: a **Flask + ECharts** web application that fetches A-share market data
(baostock / Sina API), renders candlestick (K-line) charts plus technical indicators, and ships a built-in
**AI prediction module** (`ml_module`). It can be packaged as a Windows desktop application.

Because the runtime environments differ, the project is split into two independently buildable versions:

| Version | Directory | Rendering | Bundled Python | Notes |
|---------|-----------|-----------|----------------|-------|
| **Win7** | `win7/` | Opens the UI in a locally installed Chromium-based browser (**Supermium preferred**); a PyQt5 + QWebEngineView embedded-window build is also available | A GitHub community **patched** Python 3.8 embeddable (the official build is broken on Win7) | WebView2 does not support Win7, so it falls back to the locally installed browser engine (see `win7/WIN7_COMPAT.md`); installing **Supermium** — a Chromium fork still updated for Win7 — is recommended |
| **Win11** | `win11/` | System WebView2 (Edge Chromium) / pywebview | uv + Python 3.13 | Actively maintained release variants: HighEnd / Brutalist / Sync (the theme directories still contain Dark / Minimalist) |

## Directory layout

```
stock-chart-analyzer/
├── common/      # Shared backend package (config/db/security/preload/models/routes/updater)
├── win7/        # Win7 desktop build (browser shell preferred, PyQt5/QWebEngine fallback): launcher + templates + build scripts
├── win11/       # Win11 desktop build (WebView2): launcher + themes/ + build scripts
├── assets/      # Resources shared by all themes (fonts/, js/ update-badge script)
├── scripts/     # Build-time / release-time tools (update_lib, publish_update, signing scripts)
├── tests/       # pytest test cases
├── docs/        # Design notes, product manual
├── LICENSE      # Project license (GPL-3.0)
├── README.md / README_EN.md                    # Chinese / English README
├── THIRD_PARTY_NOTICES.md / THIRD_PARTY_NOTICES_EN.md  # Third-party notices (Chinese / English)
├── requirements.txt
├── pyproject.toml
└── uv.lock
```

## Build

| Version | Command | Output |
|---------|---------|--------|
| Win7 | `python win7/build_win7.py` | `win7/dist/StockChart_Win7_{64,32}_v<date>.exe` |
| Win7 (browser edition) | `python win7/build_win7_browser.py` | `win7/dist/StockChart_Win7_{64,32}_browser_v<date>.exe` |
| Win11 | `python win11/build_win11_webview.py` | `win11/dist/StockChart_Win11_{HighEnd,Brutalist,Sync}_v<date>.exe` |

Naming rules: artifacts in `dist/` carry a build-date version suffix (`_v2026.10.04`), and an older file with
the same name is removed on the next build; `outputs/` always holds the **stable-named** (no version suffix)
latest copy used for distribution and installation. `<date>` is the build day, generated automatically by the scripts.

> Before building Win7 you must prepare the `python38-amd64` and `python38-win32` runtime directories locally
> (see `win7/WIN7_COMPAT.md`).
> Note: these two are **community-patched Python 3.8.10 embeddable** builds (the official build cannot run on Win7),
> already bundled with `vcruntime140.dll` / `sqlite3.dll` and pre-installed dependencies — they are **not** the
> official python.org packages.
> `win7/python38-win7/` additionally keeps the zip source of a patched **3.8.20** embeddable build, which can serve
> as an upgrade source (the current `build_win7.py` still uses 3.8.10).
> These runtime directories are large and are listed in `.gitignore`, so they are not committed to the repository.
> The Win7 package must be built with `--paths <repo root>` and hidden-imports for `common.*` (the scripts already
> include this); otherwise the exe fails at startup with `ModuleNotFoundError: No module named 'common'`.
> The Win11 package must be built with `--exclude-module cefpython3` (the scripts already include this): pywebview's
> PyInstaller hook pulls in the CEF backend (108 MB unpacked, nearly half the original size), and when the onefile
> archive extracts `cefpython3\chrome_elf.dll` antivirus software can intermittently block it — the symptom is a
> single dialog titled `Error` at startup with not a single line written to `stockchart.log`. That is a bootloader
> extraction failure, not an application error.

## Auto-update

After startup the desktop builds check `https://data.hsdc.cloud/update.json` in the background, and on a hit the
update is **fully automatic with no confirmation dialog at all** (on the Win7 side the users are elderly people and
a dialog would only stall the flow): a UI hot-update simply re-points the running Flask app to the new overlay and
`update.js` refreshes the page — the process is not restarted, and neither the port nor the browser tab changes;
once the full body is downloaded the app swaps and restarts itself.
Should it ever reach the 「downloaded, pending restart」 fallback state, the front end also automatically triggers the
swap 4 seconds after the badge lights up, likewise without waiting for a click.
Updates come in two granularities, implemented by `common/updater.py`:

| Granularity | Size | Restart required | Notes |
|-------------|------|------------------|-------|
| **UI overlay** | ~680 KB | No | `templates/ + static/ + assets/js` packed into a zip and extracted to `~/Documents/StockChart/ui/<variant>/`; used preferentially over the copy inside the EXE when it is **newer**; the launcher hands the live Flask app to `updater` (registered along the way in `use_overlay`), and on a hot update `repoint_overlay()` switches the pointer in place |
| **Program body (EXE)** | 180 MB (win11) / 66–150 MB (win7) | Yes | A running image cannot be overwritten but can be renamed: a hidden-window `cmd` is launched to run a `.cmd`, which waits for the old process to exit, then `move` old exe → `copy` new exe → `start`, with automatic rollback on failure and self-deletion of the script |

> **The in-place re-point for UI hot-updates must change three things — miss one and the page is still the old one**
> (verified 2026-10-05 on Flask 3.1.2 and the win7 runtime's 3.0.3):
> changing only `app.template_folder` has no effect, because `jinja_loader` is a `cached_property` that is never
> recomputed once evaluated;
> swapping only `app.jinja_loader = FileSystemLoader(...)` also has no effect, because Jinja still holds the compiled
> templates;
> only `app.jinja_env.cache.clear()` actually turns the page. `app.static_folder` is an ordinary attribute, so after
> changing it `/static/...` immediately serves the new directory.
> The `/assets/js` copy is looked up against the overlay on every request by `assets_routes`, so it becomes hot
> automatically.
> Regression tests covering all three together: `tests/test_updater.py::test_repoint_overlay_switches_a_running_flask`
> and `test_run_update_hot_repoints_live_app_without_restart`; end-to-end (real socket + asserting the PID is
> unchanged) in `test_output/hotrepoint_live.py`.
> **But the old EXE already installed on machines runs the old updater**, which lacks this hot path: when it receives a
> UI hot-update it still calls the swap script, exits itself, yet cannot bring up a new process (that batch of scripts
> is broken) — the user just needs to re-open the icon once, after which the overlay has already been attached by
> `use_overlay` at startup and it will not exit repeatedly.

> **The swap script must be launched with `CREATE_NO_WINDOW`, not `DETACHED_PROCESS`** (verified 2026-10-05):
> the `cmd` given by `DETACHED_PROCESS` has no console, so the script aborts entirely at the first `>nul` redirection
> with no error output at all — the symptom is 「download completes, process exits, but the exe does not budge and the
> `.cmd` stays put」. `CREATE_NO_WINDOW` allocates a hidden console, so it neither flashes a window nor breaks
> redirection. Regression test: `tests/test_updater.py::test_launch_bat_really_swaps_the_exe` (it really performs one swap).
> **Consequently this swap path never once succeeded in EXEs released before 2026-10-05**: their `version` must not be
> raised again, otherwise clients repeatedly download 171 MB, exit, and download again. Those users can only receive UI
> hot-updates (`ui_version`); restoring automatic swapping requires manually reinstalling one new build.
> The online manifest therefore pins the `version` of all seven variants at `2026.10.04` (= the installed version,
> which cannot trigger a body download) and raises only `ui_version` to `2026.10.05`; the fixed EXE is uploaded under a
> `_v2026.10.05.exe` name to serve as the 「manual reinstall」 download source.
> Verified on a real machine on 2026-10-05: both modes of `test_output/e2e_swap_test.py` passed (in that round of EXEs
> the UI hot-update was still 「exit + restart」, because hot swapping was implemented only later the same day); the
> criteria were that the sandbox EXE's sha changed to the new version package, `.old` appeared, the script deleted
> itself, and after the new process started `/api/update/status` reported `overlay_active=true`.

Version numbers are the **build date** `YYYY.MM.DD` (written into `build_info.json` by the build scripts and packaged
at the root of the EXE, then read by `common/version.py`); when run from source it reports `0.0.0` / `dev`, so it will
not mistakenly use an overlay. The release script leaves a sidecar at `outputs/<variant>.json` for each variant,
recording the version and artifact paths.

### Security constraints

* The manifest and all packages travel **over HTTPS only** (plaintext is allowed only for `127.0.0.1`, as a test hook);
  the IP-direct HTTP channel used by the database is not used for code distribution.
* The `sha256` in `update.json` is **mandatory**; if it is missing or mismatched the update fails outright — there is
  no "downgrade to no verification".
* Extraction rejects absolute paths and `..` (zip-slip); overlay switching uses 「temp dir → backup → atomic rename」,
  and no step can leave half of a UI behind on failure.
* Environment variables: `STOCKCHART_UPDATE_URL` to change the manifest address, `STOCKCHART_DISABLE_AUTO_UPDATE=1` to
  disable the automatic check, `STOCKCHART_UI_OVERLAY=1` to make a source run consume the overlay too (debug only).

### Release process

```bash
# 1. Build (dist/ gets dated exe files, outputs/ gets stable names + sidecar json)
python win11/build_win11_webview.py
python win7/build_win7.py
python win7/build_win7_browser.py

# 2. (Optional) code signing, see the next section (after signing, make sure TRUSTED_CERT_SHA256
#    in common/updater.py holds the new fingerprint)
powershell -File scripts/sign_executables.ps1

# 3. Generate update.json + the UI zip for each variant, and upload to the server
python scripts/publish_update.py --notes "release notes"
python scripts/publish_update.py --scp user@<your-server>:/var/www/stockdata/updates/
```

`publish_update.py` refuses a non-HTTPS `--base-url`. On the server, nginx needs two locations:
`= /update.json` and `/updates/` (see `.deploy/stockdata.ssl`; `.deploy/` is a local deployment copy and is not
committed). Once the upload is done, clients discover it on their next poll — no service restart needed.

> **Upload order**: upload the exe and ui.zip first, confirm every URL returns 200 and the sha matches, and upload
> `update.json` **last**.
> The manifest is the client's only trigger; uploading it first makes clients fetch URLs that point to missing files.
> **Do not delete previously released older EXEs from the server**: `update.json` describes only the latest version,
> but a client that has already auto-updated to some older version downloads the package name recorded in the manifest
> when it upgrades later; if historical packages are wiped, it can never update again.
> Disk retention accumulates per 「each released version × 7 variants (four win7 + three win11)」; before cleaning up,
> confirm no client still points at it.
> Retired variants (`StockChart_Win11_Dark`, `StockChart_Win11_Minimalist`) no longer appear in the manifest; their
> historical packages remain in `/updates/` so installed users can keep running, but they no longer receive updates.
>
> Two details that will make you waste a 200 MB upload (learned the hard way on 2026-10-05):
>
> * **Neither signing nor building may still be running.** `publish_update.py` records the sha of the file 「at the
>   moment it runs」; signing changes bytes and a background build overwrites the same-named artifact in `dist/`. You
>   must wait for the build to fully finish → sign → then generate the manifest, otherwise the bytes the manifest
>   points to differ from the package on the server and clients reject it outright. After uploading, always run
>   `sha256sum` on the server and compare before uploading the manifest.
> * **A UI zip gets a new sha every time it is regenerated** (the `build_info.json` inside carries a `build_time`).
>   So once the manifest is regenerated, all seven zips must be re-uploaded; you cannot patch only the changed EXE.
> * Pure front-end changes (touching only `templates/`, `static/`, `assets/js`) do not require rebuilding the EXE:
>   just raise that variant's `ui_version` in the manifest to the new version and leave `version` untouched — clients
>   download only a few hundred KB of overlay and the page refreshes. Conversely, once `version` is raised you must
>   supply a genuinely updated EXE, otherwise the version number is unchanged after the swap-and-restart and it will
>   swap repeatedly.
> * **A second UI-package release on the same day must bump the position**:
>   `python scripts/publish_update.py --ui-version 2026.10.05.2`.
>   The client compares the manifest's `ui_version` against the local `overlay_version`; reusing the already-released
>   `2026.10.05` counts as no release (clients that already installed that overlay will never download again). This
>   value is written both into the manifest and into the `build_info.json` inside the package, and the two must be
>   identical, otherwise the comparison still counts as 「has update」 after installation and it re-downloads a few
>   hundred KB every cycle. Regression test:
>   `tests/test_updater.py::test_same_day_ui_republish_needs_a_bumped_ui_version`.

### Certificates

* **TLS for the update server**: `data.hsdc.cloud` uses Let's Encrypt with automatic renewal via the certbot webroot
  method.
  On 2026-10-04 the renewal chain was verified and fixed: the one that actually works on the server is the **snap
  build of certbot** (`/snap/bin/certbot`, 5.8.0, which knows about the certificates in `/etc/letsencrypt`); the
  original `/etc/cron.d/certbot-renew` pointed at `/opt/certbot-env/bin/certbot`, a venv created on Windows and copied
  over whose shebang points at `/tmp/certbot-env/bin/python` and which simply cannot execute on Linux — it had been
  spinning idle all along. That cron now invokes the snap certbot, and a new
  `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh` was added to ensure nginx is reloaded immediately after
  renewal by snap's own `certbot.renew.timer` (otherwise the old certificate stays in memory). `renew --dry-run
  --run-deploy-hooks` passes completely.
  The local `.deploy/` directory is a **deployment copy and is gitignored**; after editing, scp it to the server manually.
* **EXE code signing**: there is no "free and publicly trusted" code-signing CA. This project uses a free scheme of
  **self-built self-signed certificates + timestamping**:

  ```powershell
  .\scripts\new_signing_cert.ps1      # Generate/reuse a self-signed cert (RSA3072/SHA256, 3 years), export pfx + cer
  .\scripts\sign_executables.ps1      # Sign and verify the win7/win11 outputs/*.exe
  .\scripts\trust_signing_cert.ps1    # On the target machine (admin) install the cer into Root + TrustedPublisher
  ```

  When a certificate has less than 30 days of validity left, `new_signing_cert.ps1` re-signs automatically (the `.cer`
  public key must be redistributed once).
  A self-signed certificate is not in the system trust chain, so automatic updates have a **second acceptance path**:
  the `TRUSTED_CERT_SHA256` whitelist in `common/updater.py`. When a client verifies the signature of an update payload
  it accepts it when **either** the system trust chain passes **or** the SHA-256 of the signer certificate's DER matches
  the whitelist — machines that never ran `trust_signing_cert.ps1` have only the latter. **Before releasing you must make
  sure the whitelist holds the fingerprint of the current signing certificate**, otherwise you ship a package that locks
  those machines out of their own update chain:
  * `new_signing_cert.ps1` and `sign_executables.ps1` both print the certificate's DER SHA-256 when they finish and write
    it to `signing/cert_sha256.txt`; the latter also diffs it against the whitelist in the source tree and warns on a
    miss.
  * Put that value into `TRUSTED_CERT_SHA256 = ('<64 lowercase hex>',)` in `common/updater.py`.
  * **Rotation**: list the old and the new fingerprint **side by side**, and only drop the old one after every client
    still on an older package has upgraded (or that package has been retired). Remove it too early and machines still
    running the old build get their swap-and-restart rejected by their own check and can never update again.
  * For temporary debugging, `STOCKCHART_TRUSTED_CERT_SHA256` (comma-separated) appends entries without touching code.
  * The whitelist only applies to "signature intact, purely a trust decision that failed" error codes (such as
    `CERT_E_UNTRUSTEDROOT`); unsigned payloads (`TRUST_E_NOSIGNATURE`) and tampered digests (`TRUST_E_BAD_DIGEST`) are
    always rejected — the whitelist must never become a way around tamper detection. Regression test:
    `tests/test_updater.py::test_signature_never_pins_unsigned_or_tampered`.
  **The cost must be stated clearly**: a self-signed certificate is not in the system trust chain, so Windows 10/11's
  SmartScreen still blocks it as an "unknown publisher", requiring the user to click "Run anyway" in the properties or
  to distribute the `.cer` in advance; enterprise distribution, or eliminating the warning entirely, requires buying a
  commercial code-signing certificate.
  `signing/`, `*.pfx`, `*.cer` are listed in `.gitignore`, and the private key is never committed (the PFX password is
  stored in `signing/pfx_password.txt`, likewise not committed; you can move it to Windows Credential Manager yourself).
  Two gotchas: `.ps1` files must be saved as **UTF-8 with BOM**, otherwise Windows PowerShell 5.1 parses the Chinese
  comments as GBK and reports a syntax error outright; `-Paths` is an array parameter, so calling it from Git-Bash must
  go through `powershell -Command "& .\scripts\sign_executables.ps1 -Paths @('a','b') -VerifyOnly"`, as passing a
  comma-separated string via `-File` is treated as a single path. After signing, a local `verify /pa` will inevitably
  report 「root certificate not trusted」; the script counts that case separately as 「signed/untrusted」, and it only
  goes all-green after running `trust_signing_cert.ps1`.

## Notes

- The backend has been extracted into the root-level `common/` package; `win7/app.py` and `win11/app.py` only assemble
  the app and register the Blueprints under `common/routes/`; each version keeps its own launcher, UI resources and
  packaging scripts.
- This repository is a public open-source repository; issues and PRs are welcome.

## License

- This project is released under the **GNU General Public License v3.0 (GPL-3.0)**; the full terms are in
  [LICENSE](LICENSE).
- GPL-3.0 was chosen because the Win7 build bundles **PyQt5 / PyQtWebEngine**, which are licensed under GPL-3.0;
  if you need to distribute under non-GPL terms (closed source or commercial), you must obtain your own commercial
  PyQt license from Riverbank.
- All third-party open-source components used by the project and their licenses (Python dependencies, the Win7 desktop
  shell, front-end libraries, fonts, build tools) are recorded in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) (Chinese) / [THIRD_PARTY_NOTICES_EN.md](THIRD_PARTY_NOTICES_EN.md) (English).