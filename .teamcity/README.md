# .teamcity — 本机 TeamCity 的配置即代码

这个目录里的 `settings.kts` 是 Kotlin DSL：TeamCity 直接读它生成两条流水线，
不需要在网页上一个个点。

| BuildType | 触发 | 干什么 |
| --- | --- | --- |
| `测试（pytest）` | **每次提交自动** | `scripts/ci/run_tests.ps1` 跑全量 pytest，结果进 Tests 标签页 |
| `发布（构建+签名+上传）` | **手动 Run** | `package.ps1` 构建+签名+出清单 → `republish_update.py` 上传 |

发布流水线有一条快照依赖：同一个提交的测试没绿，发布根本起不来。
默认**不会**"测试一绿就自动发版"——发版会立刻影响所有已装客户端，必须人工点 Run。
真要自动串联，取消 `settings.kts` 末尾 `finishBuildTrigger` 的注释。

---

## 一、启用配置即代码（一次性，5 分钟）

> **前置：允许 VCS root 读本机路径。** TeamCity 默认禁止 `file:` 形式的 URL
> （"Local file URLs are forbidden for security reasons"），而我们的仓库就在本机。
> 单机开发 CI 可以打开这个开关——在 TeamCity 数据目录下建
> `C:\ProgramData\JetBrains\TeamCity\config\teamcity-internal.properties`：
>
> ```properties
> teamcity.git.allowFileUrl=true
> ```
>
> 然后**用管理员 PowerShell 重启服务**（这一步必须你自己执行，普通权限改不了服务）：
>
> ```powershell
> Restart-Service TeamCity
> ```
>
> 重启后项目会自动从 VCS 同步配置，`http://localhost:8111/project/StockChart` 里就能看到两条流水线。

1. 打开 <http://localhost:8111/> 登录管理员。
2. 进入 **Administration → Projects → StockChart**（没有就新建一个空项目）
   → 右上 **Versioned Settings**。
3. 勾选 **Enable versioned settings**。
4. **Settings format**：`Kotlin`
5. **VCS root**：新建一个指向本机仓库的 Git root
   - Fetch URL：`C:/Users/ASUS/.qoderworkcn/workspace/mqxnujmu7xwpkdh9`
   - Default branch：`main`
   - 不需要用户名密码（本地路径）
6. **When changes are committed**：选 `Use settings from VCS`
   （即 git 里的配置说了算；在网页上改会被下次同步覆盖）
7. **Apply**。TeamCity 会同步 `settings.kts`，随后项目里出现上面两条流水线。

> 本机已经用 REST API 做好了项目 `StockChart`、设置用的 VCS root
> `StockChart_LocalRepo` 以及"版本化设置=开启 / 格式=Kotlin / 路径=`.teamcity` /
> 冲突时以 VCS 为准"这几项配置。差的就是上面那次重启——重启前项目是只读的，
> 因为你还没有可同步的配置。

> 如果第一次同步报错，多半是版本不匹配：把 `settings.kts` 第一行的
> `version = "2026.2"` 改成你 TeamCity 的版本（本机是 2026.2，build 239308；
> 版本号可在登录页页脚或 `http://localhost:8111/login.html` 里看到）。

## 二、给代理准备密钥（一次性）

代理服务 `TCBuildAgent` 跑在 **LocalSystem** 下，看不到 `ASUS` 用户证书库，
所以签名一律走 PFX。密钥统一放在一个受 ACL 保护的目录，**不进 git**：

```text
C:\ProgramData\StockChart\secrets\
    stockchart-codeSigning.pfx     # 代码签名证书（含私钥）
    pfx_password.txt               # PFX 口令（明文，靠 ACL 保护）
    id_ed25519_ubuntu              # 上传 update.json / exe 用的 SSH 私钥
```

本机已按下面命令建好（`SYSTEM / Administrators / ASUS` 三个主体可读写，其它全拒）：

```powershell
icacls "C:\ProgramData\StockChart\secrets" /inheritance:r `
       /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "ASUS:(OI)(CI)F"
```

口令优先级（`scripts/ci/package.ps1` 里实现）：

1. 环境变量 `STOCKCHART_SIGN_PFX_PASSWORD`（TeamCity 的 Password 参数）
2. 环境变量 `STOCKCHART_SIGN_PFX_PASSWORD_FILE` 指向的文件
3. 默认 `C:\ProgramData\StockChart\secrets\pfx_password.txt`

所以即使 TeamCity 的 Password 参数没配好，只要第 3 条那个文件在，签名也能过。

**TeamCity 参数**（流水线已内置，按需覆盖）：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `env.STOCKCHART_SIGN_PFX` | `...\stockchart-codeSigning.pfx` | PFX 路径 |
| `env.STOCKCHART_SIGN_PFX_PASSWORD` | 空（Password 类型） | 建议留空走口令文件 |
| `env.STOCKCHART_SIGN_SUBJECT` | `CN=HSDC StockChart Code Signing, O=HSDC` | 证书主题 |
| `env.STOCKCHART_PUBLISH_KEY` | `...\id_ed25519_ubuntu` | 上传用私钥 |
| `env.STOCKCHART_PUBLISH_HOST` | `ubuntu@81.71.134.71` | 目标机 |
| `release.notes` | 空 | 更新说明，进 update.json |
| `release.ui_version` | 空 | 同一天二发界面包时抬一位 |

## 三、代理机需要的东西

`TCBuildAgent` 的工作目录是干净检出（`C:\TeamCity\buildAgent\work\<hash>`），
下面两样**不在 git 里**，必须机器上已存在：

- **pwsh 7**：`C:\Program Files\PowerShell\7\pwsh.exe`（脚本都写了 `#Requires -Version 7.0`，
  故意不支持 5.1）
- **带 pytest 的 Python**：`scripts/ci/run_tests.ps1` 会自己找
  `STOCKCHART_TEST_PYTHON` → PATH 上的 `python` → `.venv-win11` → `.venv`，
  逐个试 `import pytest`，找到就用。团队配置里已经把 `env.STOCKCHART_TEST_PYTHON`
  设成 `C:\Users\ASUS\AppData\Local\Programs\Python\Python314\python.exe`，
  因为代理是 LocalSystem，系统 PATH 里不一定有 `python`。

发布流水线额外需要 `.venv-win11`（PyInstaller）与 `win7\python38-*`（构建 win7 变体），
这两个目录被 `.gitignore` 排除了，只存在于本机工作副本里。**所以发布步骤不在代理的
干净检出目录里跑**：DSL 里的 `repo.path` 参数直接指向本机仓库，构建步骤调用的是
`%repo.path%\scripts\ci\package.ps1`。这也是"发布仍在本机执行"的落地方式——
TeamCity 只负责触发、排队和留日志。

首次发布前先确认代理能读：

```powershell
# 以 LocalSystem 身份最省事的验证：直接看文件是否可读
Get-Content C:\ProgramData\StockChart\secrets\pfx_password.txt
& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoProfile -File scripts\ci\run_tests.ps1
```

## 四、日常怎么用

**改完代码（每天都发生）**

```bash
git add -A && git commit -m "..." && git push
```

TeamCity 的 VCS Trigger 在 60 秒内（quiet period）自动起一次 `测试（pytest）`。
红/绿在项目首页一眼可见；点进去 **Tests** 标签页能直接看到哪个用例挂了。

想立刻看结果，不用等触发器：点 `测试（pytest）` → 右上 **Run**。

**发版（人工确认）**

1. 确认 `测试（pytest）` 在要发的那个提交上是绿的（快照依赖会替你检查）
2. 点 `发布（构建+签名+上传）` → **Run**
3. **Parameters** 里填：
   - `release.notes`：本次更新说明，如 `修复涨停统计漏算科创板`
   - `release.ui_version`：只有"同一天第二次发界面包"才需要，如 `2026.11.07.2`
4. Run 之后它会：构建 7 个变体 → signtool 签 exe → 给每个 UI zip 做 detached 签名
   → 写 `update_dist\update.json` → 校验 sha/签名 → 上传到 `data.hsdc.cloud`
5. 构建页面的 **Artifacts** 里能下载 `update_dist/*` 留档

> 发布步骤在**本机工作副本**里执行（日志开头会打印当前提交与是否有未提交改动）。
> 所以点 Run 之前请确认工作副本和要发的提交一致。
> 上传前 `republish_update.py` 会逐个核对 sha256 与签名，任何一个变体 `ui_sig` 为空就中止，
> 不会把坏清单推给客户端。

想先演练上传而不真发（只校验，不连服务器）：在本机跑

```powershell
pwsh -NoProfile -File scripts\ci\package.ps1 -SkipBuild -SkipSign -Publish -DryRun
```

## 五、排错

| 现象 | 原因 / 处理 |
| --- | --- |
| `Local file URLs are forbidden for security reasons` | 没加 `teamcity.git.allowFileUrl=true`，或加了没重启 TeamCity |
| 同步报 `version` 不匹配 | 改 `settings.kts` 首行 `version` 为当前 TeamCity 版本 |
| 构建报「找不到 pwsh」 | 代理 PATH 里没有 pwsh 7；把 `C:\Program Files\PowerShell\7` 加进系统 PATH 后重启服务 |
| `run_tests.ps1` 报找不到 pytest | 设 `STOCKCHART_TEST_PYTHON` 指向带 pytest 的解释器 |
| 签名报「找不到证书」 | PFX 路径不对，或 ACL 没给 `SYSTEM` 读权限 |
| 签名报「口令错误」 | `pfx_password.txt` 与 PFX 不匹配；用 `certmgr.msc` 重新导出并同步口令 |
| 上传报 `Permission denied (publickey)` | 私钥路径不对，或公钥没在服务器 `~/.ssh/authorized_keys` 里 |
| 上传报 `Host key verification failed` | `republish_update.py` 默认带 `StrictHostKeyChecking=accept-new`，仍失败就手工 `ssh -i <key> ubuntu@81.71.134.71` 认一次 |

脚本层面的坑（pwsh 与 5.1 的差异、LocalSystem 证书可见性等）见
`docs/TeamCity_CICD_上手.md`。
