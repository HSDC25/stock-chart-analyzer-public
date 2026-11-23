#Requires -Version 7.0
<#
.SYNOPSIS
    一键发版：测试 → 构建 7 个变体 → 签名 → 出清单 → 上传 → 线上验收。

.DESCRIPTION
    本机发布流水线的唯一入口（TeamCity 只做独立的测试门禁）。
    任何一步失败就停，不会把半成品传上服务器。

    -PublishOnly   不构建不签名，直接把现有 update_dist\update.json 重新上传（重试上传用）
    -UiOnly        只重打界面包（跳过 EXE 构建与签名，通常配合 -UiVersion）
    -SkipBuild     跳过构建，用现有 dist 产物重签/重出清单（快速验证）
    -SkipSign      跳过签名（产物不能对外发布，只适合本机调试）
    -SkipTests     跳过 pytest（只在你刚跑过测试时用）
    -DryRun        走到上传那步只校验不真传，也不做线上验收
    -Force         配合 -PublishOnly：服务器上 sha 相同的包也重传

    日志与状态都在 build\logs\（已被 .gitignore 排除）：
      release-<时间戳>.log   完整控制台记录
      release-status.json    最近一次的状态，给脚本/托盘工具读
      release.lock           互斥锁，防止两次发布同时改同一批产物

    没给 -Notes 时，更新说明取当前提交的标题（自动去掉 [release] 标记）。

.EXAMPLE
    pwsh -NoProfile -File scripts\ci\release.ps1 -Notes "修复排行筛选"
.EXAMPLE
    pwsh -NoProfile -File scripts\ci\release.ps1 -PublishOnly
.EXAMPLE
    pwsh -NoProfile -File scripts\ci\release.ps1 -UiOnly -UiVersion 2026.11.08.2
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$SkipSign,
    [switch]$UiOnly,
    [switch]$PublishOnly,
    [switch]$DryRun,
    [switch]$Force,
    [string]$Notes = '',
    [string]$UiVersion = '',
    [string]$Python,
    [string]$OutDir = 'update_dist'
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $repo

# 签名/上传材料默认去受 ACL 保护的目录取（与 TeamCity 用同一套）；显式环境变量优先
$secretDir = 'C:\ProgramData\StockChart\secrets'
if (-not $env:STOCKCHART_SIGN_PFX) {
    $pfx = Join-Path $secretDir 'stockchart-codeSigning.pfx'
    if (Test-Path $pfx) { $env:STOCKCHART_SIGN_PFX = $pfx }
}
if (-not $env:STOCKCHART_PUBLISH_KEY) {
    $key = Join-Path $secretDir 'id_ed25519_ubuntu'
    if (Test-Path $key) { $env:STOCKCHART_PUBLISH_KEY = $key }
}

$logDir = Join-Path $repo 'build\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logPath = Join-Path $logDir "release-$stamp.log"
$statusPath = Join-Path $logDir 'release-status.json'
$lockPath = Join-Path $logDir 'release.lock'
$startedAt = Get-Date -Format 'o'
$step = '准备'

function Resolve-Python {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    if ($env:STOCKCHART_TEST_PYTHON) { $candidates += $env:STOCKCHART_TEST_PYTHON }
    $candidates += @('python.exe', (Join-Path $repo '.venv-win11\Scripts\python.exe'))
    foreach ($exe in $candidates) {
        if (-not $exe) { continue }
        if ($exe -ne 'python.exe' -and -not (Test-Path $exe)) { continue }
        $null = & $exe -c "import sys" 2>&1
        if ($LASTEXITCODE -eq 0) { return $exe }
    }
    throw '找不到可用的 Python（-Python 指定路径，或把 python 放进 PATH）'
}

function Get-CommitSubject {
    # git 的提交信息是 UTF-8，而 pwsh 按控制台代码页（本机 GBK）解码原生命令输出，
    # 直接用 & git log 会把中文更新说明变成乱码；这里显式按 UTF-8 读。
    param([string]$Repo)
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'git'
    foreach ($a in @('-C', $Repo, 'log', '-1', '--pretty=%s')) { $psi.ArgumentList.Add($a) }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        $out = $proc.StandardOutput.ReadToEnd()
        $proc.WaitForExit()
        if ($proc.ExitCode -ne 0) { return '' }
        return $out.Trim()
    } catch {
        return ''
    }
}

function Set-ReleaseStatus {
    param([string]$State, [string]$Message = '', [string]$Version = '')
    try {
        [ordered]@{
            state    = $State
            step     = $step
            message  = $Message
            version  = $Version
            notes    = $Notes
            started  = $startedAt
            finished = (Get-Date -Format 'o')
            pid      = $PID
            log      = $logPath
        } | ConvertTo-Json -Depth 3 | Set-Content -Path $statusPath -Encoding utf8
    } catch {
        Write-Warning "写状态文件失败：$_"
    }
}

function Show-Toast {
    param([string]$Title, [string]$Text, [string]$Level = 'Info')
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = if ($Level -eq 'Error') {
            [System.Drawing.SystemIcons]::Error
        } else {
            [System.Drawing.SystemIcons]::Information
        }
        $icon.BalloonTipIcon = if ($Level -eq 'Error') {
            [System.Windows.Forms.ToolTipIcon]::Error
        } else {
            [System.Windows.Forms.ToolTipIcon]::Info
        }
        $icon.Visible = $true
        $icon.BalloonTipTitle = $Title
        $icon.BalloonTipText = $Text
        $icon.ShowBalloonTip(8000)
        # 进程退出气泡就消失，等它显示完再收尾
        Start-Sleep -Seconds 6
        $icon.Dispose()
    } catch {
        Write-Warning "通知没弹出来（不影响发布）：$_"
    }
}

try { Start-Transcript -Path $logPath -Force | Out-Null } catch { Write-Warning "转录日志失败：$_" }
Write-Host "[release] 仓库: $repo"
Write-Host "[release] 日志: $logPath"

# 互斥：两个发布同时跑会互相覆盖 outputs\ 与 update_dist\
if (Test-Path $lockPath) {
    $owner = (Get-Content $lockPath -Raw -ErrorAction SilentlyContinue)
    $ownerId = 0
    if ($owner -and [int]::TryParse($owner.Trim(), [ref]$ownerId) -and
        (Get-Process -Id $ownerId -ErrorAction SilentlyContinue)) {
        throw "另一个发布进程还在跑（PID $ownerId）。等它结束，或确认它已卡死后删掉 $lockPath"
    }
    Write-Warning "清理上次残留的发布锁（PID $owner 已不在）"
    Remove-Item $lockPath -Force
}
Set-Content -Path $lockPath -Value $PID -Encoding ascii

if (-not $Notes) {
    $subject = Get-CommitSubject -Repo $repo
    if ($subject) { $Notes = ($subject -replace '\[release\]', '').Trim() }
}

$version = ''
try {
    Set-ReleaseStatus -State 'running'

    $python = Resolve-Python -Explicit $Python
    Write-Host "[release] 使用解释器: $python"
    if ($Notes) { Write-Host "[release] 更新说明: $Notes" }
    if ($UiOnly -and -not $UiVersion) {
        Write-Warning '界面包单独发版建议给 -UiVersion（如 2026.11.08.2），否则当天装过旧界面包的客户端不会更新'
    }

    # 1. 测试：用独立进程调 run_tests.ps1，避免脚本里的 exit 把我们这一层也退出
    if ($PublishOnly) {
        Write-Host "`n[1/3] 跳过测试（-PublishOnly 只重传）"
    } elseif ($SkipTests) {
        Write-Warning '[1/4] 跳过测试（-SkipTests）'
    } else {
        $step = '测试'
        Set-ReleaseStatus -State 'running'
        Write-Host "`n[1/4] 跑全量 pytest"
        # 子进程的输出不经过本进程的流，Start-Transcript 收不到；过一遍管道才进日志
        & (Join-Path $PSHOME 'pwsh.exe') -NoProfile -File (Join-Path $repo 'scripts\ci\run_tests.ps1') 2>&1 |
            ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw "测试失败（exit $LASTEXITCODE）：不发版" }
    }

    # 2. 产物
    if ($PublishOnly) {
        $manifestPath = Join-Path $repo "$OutDir\update.json"
        if (-not (Test-Path $manifestPath)) { throw "找不到 $manifestPath，先跑一次完整发布" }
        $step = '重新上传'
        Set-ReleaseStatus -State 'running'
        Write-Host "`n[2/3] 不构建，直接重传 $manifestPath"
        $republishArgs = @((Join-Path $repo 'scripts\republish_update.py'))
        if ($Force) { $republishArgs += '--force' }
        if ($DryRun) { $republishArgs += '--dry-run' }
        & $python @republishArgs 2>&1 | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw "上传失败（exit $LASTEXITCODE）" }
    } else {
        $step = '构建/签名/清单/上传'
        Set-ReleaseStatus -State 'running'
        $pkg = @{ Publish = $true; OutDir = $OutDir }
        if ($SkipBuild -or $UiOnly) { $pkg.SkipBuild = $true }
        if ($SkipSign -or $UiOnly) { $pkg.SkipSign = $true }
        if ($Notes) { $pkg.Notes = $Notes }
        if ($UiVersion) { $pkg.UiVersion = $UiVersion }
        if ($DryRun) { $pkg.DryRun = $true }
        & (Join-Path $repo 'scripts\ci\package.ps1') @pkg
        if ($LASTEXITCODE -ne 0) { throw "构建/签名/出清单失败（exit $LASTEXITCODE）" }
    }

    $manifestPath = Join-Path $repo "$OutDir\update.json"
    if (Test-Path $manifestPath) {
        $json = Get-Content $manifestPath -Raw | ConvertFrom-Json
        $version = ($json.variants.PSObject.Properties | Select-Object -First 1).Value.version
    }

    # 3. 线上验收：客户端视角再核一遍清单与包体
    if ($DryRun) {
        Write-Host "`n[3/3] [dry-run] 跳过线上验收（没有上传）"
    } else {
        $step = '线上验收'
        Set-ReleaseStatus -State 'running' -Version $version
        Write-Host "`n[3/3] 线上验收（客户端视角）"
        & $python (Join-Path $repo 'scripts\ci\verify_published.py') 2>&1 |
            ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw "线上验收没通过（exit $LASTEXITCODE）" }
    }

    $step = '完成'
    Set-ReleaseStatus -State 'success' -Version $version
    Write-Host "`n[OK] 发布完成：$version"
    if ($DryRun) {
        Write-Host '[dry-run] 产物只做校验，没有上传；去掉 -DryRun 才会真发'
    }
    Show-Toast 'StockChart 发布完成' "版本 $version 已上传并通过线上验收。日志：$logPath" 'Info'
    exit 0
} catch {
    Set-ReleaseStatus -State 'failed' -Message "$_"
    Write-Host "`n[FAIL] $step 失败：$_" -ForegroundColor Red
    Write-Host "       日志：$logPath"
    Show-Toast 'StockChart 发布失败' "$step 失败：$_（日志：$logPath）" 'Error'
    exit 1
} finally {
    try { Stop-Transcript | Out-Null } catch { }
    Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
}
