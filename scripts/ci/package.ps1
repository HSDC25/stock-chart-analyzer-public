#Requires -Version 7.0
<#
.SYNOPSIS
    「发布准备」脚本：构建 EXE → 代码签名 → 生成带签名的 update.json。
    默认只准备产物，不上传（上传会立刻对所有客户端生效）。

.DESCRIPTION
    顺序与手工发布一致（scripts/publish_update.py / republish_update.py）：
      1. 构建 7 个变体（win7 普通/browser 各 32/64 + win11 三个主题）
      2. signtool 签名 dist\ 下 7 个 EXE（证书在用户证书存储，或 -PfxPath / 环境变量），
         再把已签字节回写到 outputs\ 稳定名副本
      3. publish_update.py 打 UI 包 + 给 zip 做 detached 签名 + 写 update.json
      4. -Publish 时再调 republish_update.py 上传（会先复核 sha 与签名）

    -SkipBuild  跳过编译，用现有 dist/侧车重签并重出清单（快速验证）
    -SkipSign   跳过签名（只适合本机调试，产物不能对外发布）
    -Publish    生成清单后直接上传（TeamCity 发布流水线用）
    -DryRun     配合 -Publish：只校验不真传

    签名材料也可以用环境变量（适合非交互/CI）：
      STOCKCHART_SIGN_PFX               PFX 路径
      STOCKCHART_SIGN_PFX_PASSWORD      PFX 口令
      STOCKCHART_SIGN_PFX_PASSWORD_FILE 口令文件（口令没设时从文件读，给 TeamCity 兜底）
      STOCKCHART_SIGN_SUBJECT           证书主题（默认 CN=HSDC StockChart Code Signing, O=HSDC）

.EXAMPLE
    pwsh -NoProfile -File scripts\ci\package.ps1 -Notes "修复排行筛选"
    pwsh -NoProfile -File scripts\ci\package.ps1 -SkipBuild -SkipSign
#>
[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$SkipSign,
    [string]$Notes = '',
    # 界面包单独定版（同一天二次发界面时必须抬一位，如 2026.10.05.2）
    [string]$UiVersion = '',
    [string]$Python,
    [string]$OutDir = 'update_dist',
    # 生成清单后直接上传到更新服务器（TeamCity 发布流水线用；上传即对所有客户端生效）
    [switch]$Publish,
    # 配合 -Publish：只校验不真传，用来演练上传步骤
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

# PFX 口令来源（优先级）：STOCKCHART_SIGN_PFX_PASSWORD → *_PASSWORD_FILE → 本机密钥库默认文件
# 后两条是为了兼容 TeamCity 版本化设置同步时 Password 参数可能读不到的情况。
if (-not $env:STOCKCHART_SIGN_PFX_PASSWORD) {
    $pwFile = $env:STOCKCHART_SIGN_PFX_PASSWORD_FILE
    if (-not $pwFile) {
        $defaultPwFile = 'C:\ProgramData\StockChart\secrets\pfx_password.txt'
        if (Test-Path $defaultPwFile) { $pwFile = $defaultPwFile }
    }
    if ($pwFile -and (Test-Path $pwFile)) {
        $env:STOCKCHART_SIGN_PFX_PASSWORD = (Get-Content $pwFile -Raw).Trim()
        Write-Host "[ci] PFX 口令来源: $pwFile"
    }
}

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $repo
Write-Host "[ci] 仓库: $repo"
# 发布在本地工作副本里跑（构建用的 venv 不在 git 里），把当前提交打出来方便对账
$head = (& git -C $repo rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -eq 0 -and $head) {
    $dirty = (& git -C $repo status --porcelain 2>$null)
    Write-Host ("[ci] 当前提交: $head" + $(if ($dirty) { '（工作副本有未提交改动）' } else { '' }))
}

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

$python = Resolve-Python -Explicit $Python
Write-Host "[ci] 使用解释器: $python"

# 1. 构建
# 清单里有 7 个变体：win7 4 个（普通 + browser，各 32/64）+ win11 3 个。
# 只构建 win11 会把上一版 win7 的 exe 混进新清单，所以三个脚本都要跑。
if (-not $SkipBuild) {
    Write-Host "`n[1/3] 构建 7 个变体（最慢的一步，通常 15~30 分钟）"
    foreach ($script in @('win7\build_win7.py',
                          'win7\build_win7_browser.py',
                          'win11\build_win11_webview.py')) {
        Write-Host "[1/3] 运行 $script"
        # 原生进程的输出不走 PowerShell 流，Start-Transcript 收不到；过一遍管道才进日志
        & $python (Join-Path $repo $script) 2>&1 | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw "构建失败：$script（exit $LASTEXITCODE）" }
    }
} else {
    Write-Host "`n[1/3] 跳过构建（-SkipBuild）"
}

$outDirs = @((Join-Path $repo 'win7\outputs'), (Join-Path $repo 'win11\outputs'))
foreach ($dir in $outDirs) {
    if (-not (Get-ChildItem $dir -Filter *.exe -ErrorAction SilentlyContinue)) {
        throw "没有找到可发布的 exe（$dir），先跑一次构建"
    }
}
# 清单是按 sidecar json 收集的，少一个就会漏发一个变体
$expected = @('StockChart_Win7_32', 'StockChart_Win7_32_browser',
              'StockChart_Win7_64', 'StockChart_Win7_64_browser',
              'StockChart_Win11_Brutalist', 'StockChart_Win11_HighEnd',
              'StockChart_Win11_Sync')
$missing = @($expected | Where-Object {
    $hit = $false
    foreach ($dir in $outDirs) {
        if (Test-Path (Join-Path $dir ($_ + '.json'))) { $hit = $true }
    }
    -not $hit
})
if ($missing.Count) { throw "缺少这些变体的产物（先跑一次完整构建）：$($missing -join ', ')" }

# 2. 签名
# 客户端与清单读的都是 sidecar 指向的 dist 字节；outputs 只是稳定名副本。
# 必须先签 dist，再把已签字节拷回 outputs，只签 outputs 等于发布未签名包。
$distExes = @()
foreach ($dir in $outDirs) {
    foreach ($json in (Get-ChildItem $dir -Filter *.json -ErrorAction SilentlyContinue)) {
        $info = Get-Content $json.FullName -Raw | ConvertFrom-Json
        if (-not $info.exe) { throw "侧车缺少 exe 字段：$($json.FullName)" }
        $distExe = Join-Path $repo ($info.exe -replace '/', '\')
        if (-not (Test-Path $distExe)) { throw "侧车指向的产物不存在：$distExe" }
        $distExes += $distExe
    }
}
if ($distExes.Count -ne 7) { throw "dist 产物数量不是 7 个（实际 $($distExes.Count)），先跑完整构建" }

if (-not $SkipSign) {
    Write-Host "`n[2/3] 代码签名（Authenticode，签 dist）"
    # 用具名哈希表 splat：数组 splat 会让 -Paths 后面的第二个路径被当成位置参数，
    # 绑定到 -PfxPassword（SecureString）上，报类型转换错误。
    $signParams = @{ Paths = $distExes }
    if ($env:STOCKCHART_SIGN_PFX) {
        $signParams.PfxPath = $env:STOCKCHART_SIGN_PFX
        Write-Host "[ci] 使用 PFX: $env:STOCKCHART_SIGN_PFX"
    }
    if ($env:STOCKCHART_SIGN_SUBJECT) {
        $signParams.Subject = $env:STOCKCHART_SIGN_SUBJECT
    }
    & (Join-Path $repo 'scripts\sign_executables.ps1') @signParams
    if ($LASTEXITCODE -ne 0) { throw "签名失败（exit $LASTEXITCODE）" }
} else {
    Write-Warning '[2/3] 跳过签名（-SkipSign）：这样的包客户端会拒收，只能本机调试'
}

# 签后自检：证书与 RFC3161 时间戳缺一不可，未签就绝不往下生成清单
foreach ($exe in $distExes) {
    $sig = Get-AuthenticodeSignature -LiteralPath $exe
    if ($sig.Status -eq 'NotSigned' -or -not $sig.SignerCertificate) {
        throw "dist 包未签名：$exe"
    }
    if (-not $sig.TimeStamperCertificate) {
        throw "dist 包缺少 RFC3161 时间戳：$exe"
    }
}

# 用已签字节覆盖 outputs 稳定名，供本机安装/手动分发
foreach ($dir in $outDirs) {
    foreach ($json in (Get-ChildItem $dir -Filter *.json -ErrorAction SilentlyContinue)) {
        $info = Get-Content $json.FullName -Raw | ConvertFrom-Json
        $src = Join-Path $repo ($info.exe -replace '/', '\')
        $dst = Join-Path $dir ($json.BaseName + '.exe')
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
}

# 3. 清单（含 UI 包的 detached 签名）
Write-Host "`n[3/3] 生成 update.json（UI 包含证书签名）"
$publishArgs = @((Join-Path $repo 'scripts\publish_update.py'),
                 '--out', (Join-Path $repo $OutDir))
if ($Notes) { $publishArgs += @('--notes', $Notes) }
if ($UiVersion) { $publishArgs += @('--ui-version', $UiVersion) }
& $python $publishArgs 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) { throw "生成清单失败（exit $LASTEXITCODE）" }

$manifest = Join-Path $repo "$OutDir\update.json"
if (-not (Test-Path $manifest)) { throw "没有生成 $manifest" }
$json = Get-Content $manifest -Raw | ConvertFrom-Json
$unsigned = @($json.variants.PSObject.Properties | Where-Object { -not $_.Value.ui_sig })
if ($unsigned.Count) {
    Write-Warning ("这些变体的 UI 包没有签名，新版客户端会拒绝界面热更：" +
                   (($unsigned | ForEach-Object { $_.Name }) -join ', '))
}

Write-Host "`n[OK] 发布产物已就绪：$manifest"

if (-not $Publish) {
    Write-Host "     人工确认后上传：$python scripts\republish_update.py"
    return
}

# 4. 上传：republish_update.py 会再核一遍 sha 与签名，任何一个变体不对就中止
Write-Host "`n[4/4] 上传到更新服务器"
$republishArgs = @((Join-Path $repo 'scripts\republish_update.py'))
if ($DryRun) { $republishArgs += '--dry-run' }
& $python $republishArgs 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) { throw "上传失败（exit $LASTEXITCODE）" }
if ($DryRun) {
    Write-Host '[dry-run] 校验通过，没有上传'
} else {
    Write-Host '[OK] 已上传并生效'
}
