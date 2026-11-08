# CI 与发版

两条路，各管一段：

| 谁来跑 | 触发 | 干什么 |
| --- | --- | --- |
| TeamCity（本机 8111） | 提交后自动（VCS Trigger） | `scripts/ci/run_tests.ps1` 跑全量 pytest，结果进 Tests 标签页 |
| 本机 `release.ps1` | 提交信息带 `[release]`，或手动一条命令 | 测试 → 构建 7 个变体 → 签名 → 出清单 → 上传 → 线上验收 |

发版**不在 TeamCity 里跑**：代理服务 `TCBuildAgent` 是 `LocalSystem`，看不到
`ASUS` 用户证书库，`.venv-win11` / `win7\python38-*` 也不在 git 里。
所以 TeamCity 只当独立的测试门禁，发版永远在本机工作副本里执行。

> 这里曾经放过 Kotlin DSL（配置即代码）。本机 TeamCity 的服务端 DSL 沙箱编译坏了
> （缺少 `VcsRoot` 等核心类，`pom.xml is not a file`），同步一直失败，已经删掉，
> 两条流水线改为在网页上手工建。不要再往仓库里加 `settings.kts`。

---

## 一、日常：改完代码

```bash
git add -A && git commit -m "修复涨停统计漏算科创板"
```

TeamCity 的 VCS Trigger 在 60 秒（quiet period）内自动起一次 `测试（pytest）`。
红/绿在项目首页一眼可见，点进去 **Tests** 标签能看到具体哪个用例挂了。

## 二、发版：一条命令

```powershell
pwsh -NoProfile -File scripts\ci\release.ps1 -Notes "修复涨停统计漏算科创板"
```

它按顺序做这些事，任何一步失败就停，不会把半成品传上服务器：

1. 全量 pytest（`run_tests.ps1`）
2. 构建 7 个变体（win7 4 个 + win11 3 个，约 15~30 分钟）
3. Authenticode 签 exe，再给每个 UI zip 做 detached 签名
4. 生成 `update_dist\update.json`，**只上传服务器上没有的包体**（sha256 相同就跳过）
5. `verify_published.py` 以客户端视角走一遍 HTTPS：清单逐字节一致、每个 URL 200、
   长度对得上、UI 包全量算 sha、exe 抽查首尾两个

跑完弹一个 Windows 气泡，`build\logs\release-status.json` 里留状态；
完整控制台记录在 `build\logs\release-<时间戳>.log`。

### 完全不用开浏览器：提交时带 `[release]`

装一次钩子（把 `.githooks\post-commit` 复制进 `.git\hooks`，并强制 LF）：

```powershell
pwsh -NoProfile -File scripts\ci\install_hooks.ps1
```

之后：

```bash
git commit -m "修复涨停统计漏算科创板 [release]"
```

提交成功后在后台窗口自动跑完整条流水线，更新说明默认取提交标题（自动去掉
`[release]`）。钩子只认这个标记，普通提交什么都不会发生。

### 几种常见变体

```powershell
# 上传成功了但线上验收没过 / 想重传：不重新构建，直接再传一遍
pwsh -NoProfile -File scripts\ci\release.ps1 -PublishOnly

# 只改了模板/JS（不涉及 exe）：跳过构建和签名，只重打界面包并抬版本号
pwsh -NoProfile -File scripts\ci\release.ps1 -UiOnly -UiVersion 2026.11.08.2

# 演练：全程只校验，不上传
pwsh -NoProfile -File scripts\ci\release.ps1 -DryRun

# 只重打产物、不上传（等于 package.ps1 的默认行为）
pwsh -NoProfile -File scripts\ci\package.ps1
```

`-UiOnly` 一定要给 `-UiVersion`：客户端按版本号比大小，当天已经吃过旧界面包的机器
不会去下同版本号的第二份。

## 三、TeamCity 侧（网页上一次性建好，之后不用管）

登录 <http://localhost:8111/>（管理员）。

| 配置 | 关键设置 |
| --- | --- |
| 项目 `StockChart` | Versioned Settings 保持 **Synchronization disabled** |
| `测试（pytest）` | 构建步骤 `pwsh -NoProfile -File scripts\ci\run_tests.ps1`；Build Features → XML report processing（Ant JUnit，`+:build/reports/pytest.xml`）；Triggers → VCS Trigger |
| `发布（构建+签名+上传）` | 已废弃，不要再点 Run。发版走 `release.ps1`，省掉 20 分钟构建和网页等待 |

`发布` 那条留着只当历史记录；真要删就到项目页右上 **Actions → Delete**。

### 代理机需要的东西

`TCBuildAgent` 跑在 `LocalSystem`，工作目录是干净检出
（`C:\TeamCity\buildAgent\work\<hash>`），下面两样不在 git 里，机器上必须已有：

- **pwsh 7**：`C:\Program Files\PowerShell\7\pwsh.exe`（脚本都写了 `#Requires -Version 7.0`）
- **带 pytest 的 Python**：`run_tests.ps1` 会按
  `STOCKCHART_TEST_PYTHON` → PATH `python` → `.venv-win11` → `.venv` 逐个试 `import pytest`。
  团队里已把 `env.STOCKCHART_TEST_PYTHON` 设成
  `C:\Users\ASUS\AppData\Local\Programs\Python\Python314\python.exe`（代理的 PATH 里没有 python）。

### 签名与上传材料（一次性）

不在仓库里，统一放一个受 ACL 保护的目录：

```text
C:\ProgramData\StockChart\secrets\
    stockchart-codeSigning.pfx     # 代码签名证书（含私钥）
    pfx_password.txt               # PFX 口令（明文，靠 ACL 保护）
    id_ed25519_ubuntu              # 上传包体用的 SSH 私钥
```

```powershell
icacls "C:\ProgramData\StockChart\secrets" /inheritance:r `
       /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "ASUS:(OI)(CI)F"
```

`release.ps1` / `package.ps1` / `run_tests.ps1` 都会在环境变量没设时自动去这个目录取：
`STOCKCHART_SIGN_PFX`、`STOCKCHART_SIGN_PFX_PASSWORD(_FILE)`、`STOCKCHART_PUBLISH_KEY`，
所以在自己账户下直接跑脚本不需要任何额外配置。

## 四、排错

| 现象 | 原因 / 处理 |
| --- | --- |
| 提交带 `[release]` 但没反应 | 钩子没装：`pwsh -NoProfile -File scripts\ci\install_hooks.ps1`；或 pwsh 不在 PATH |
| 钩子装了还是不跑 | 本仓库 `core.hooksPath` 指向 `C:\Users\ASUS\.empty-hooks`（git-lfs 的钩子目录），git 不读 `.git\hooks`。安装脚本会自动装到 hooksPath 里；不放心可以 `git config --get core.hooksPath` 核对 |
| 提示 `bad interpreter: /bin/sh^M` | 钩子被写成了 CRLF：重跑安装脚本（它强制写 LF） |
| `另一个发布进程还在跑（PID …）` | 两次发版撞车。等前一个结束，或确认它已卡死后删 `build\logs\release.lock` |
| `[FAIL] 线上清单与本机不一致` | 清单没传完或被 CDN 缓存。重跑 `release.ps1 -PublishOnly` |
| `Permission denied (publickey)` | 私钥路径不对，或公钥不在服务器 `~/.ssh/authorized_keys` |
| `Host key verification failed` | 先手工 `ssh -i <key> ubuntu@81.71.134.71` 认一次 |
| 签名报「找不到证书 / 口令错误」 | PFX 路径或口令不对；用 `certmgr.msc` 重新导出并同步 `pfx_password.txt` |
| `run_tests.ps1` 报找不到 pytest | 设 `STOCKCHART_TEST_PYTHON` 指向带 pytest 的解释器 |
