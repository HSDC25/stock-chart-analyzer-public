#Requires -Version 7.0
<#
.SYNOPSIS
    TeamCity「测试」构建步骤：跑全量 pytest，并输出 JUnit XML 供 TeamCity 展示。

.DESCRIPTION
    在仓库根用**带 pytest 的解释器**跑 `pytest tests`，退出码即构建成败。

    为什么要探测解释器：.venv-win11 / .venv-win7 只装了打包依赖（PyInstaller 等），
    没有 pytest；装 pytest 的是系统 Python。写死任何路径都会在换机器/换 Python 后失效。

    产出：build\reports\pytest.xml（TeamCity 的 XML Report Processing 读它）

.EXAMPLE
    pwsh -NoProfile -File scripts\ci\run_tests.ps1
#>
[CmdletBinding()]
param(
    # 显式指定解释器（CI 里也可用环境变量 STOCKCHART_TEST_PYTHON 覆盖）
    [string]$Python,
    # 报告目录，相对仓库根
    [string]$ReportDir = 'build\reports'
)

$ErrorActionPreference = 'Stop'
# 原生命令（python）写到 stderr 是正常现象，不要当成终止错误
$PSNativeCommandUseErrorActionPreference = $false

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $repo
Write-Host "[ci] 仓库: $repo"

function Test-HasPytest {
    param([string]$Exe)
    $null = & $Exe -c "import pytest" 2>&1
    return ($LASTEXITCODE -eq 0)
}

function Resolve-Python {
    param([string]$Explicit)

    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    if ($env:STOCKCHART_TEST_PYTHON) { $candidates += $env:STOCKCHART_TEST_PYTHON }
    # PATH 上的 python 优先：测试依赖装在那里；.venv-* 只有打包依赖，没有 pytest
    $candidates += @(
        'python.exe',
        (Join-Path $repo '.venv-win11\Scripts\python.exe'),
        (Join-Path $repo '.venv\Scripts\python.exe')
    )

    foreach ($exe in $candidates) {
        if (-not $exe) { continue }
        if ($exe -ne 'python.exe' -and -not (Test-Path $exe)) { continue }
        if (-not (Test-HasPytest -Exe $exe)) { continue }
        $version = & $exe -c "import sys;print(sys.version.split()[0])"
        Write-Host "[ci] 使用解释器: $exe (Python $version)"
        return $exe
    }
    throw '找不到装好 pytest 的 Python。请 pip install pytest，或用 -Python 指定路径。'
}

$python = Resolve-Python -Explicit $Python

if (-not (Test-Path $ReportDir)) {
    New-Item -ItemType Directory -Path $ReportDir -Force | Out-Null
}
$reportPath = Join-Path (Resolve-Path $ReportDir) 'pytest.xml'

# -p no:cacheprovider：CI 工作区可能只读，别让 .pytest_cache 写失败刷警告
Write-Host "[ci] $python -m pytest tests -q -p no:cacheprovider --junitxml=$reportPath"
& $python -m pytest tests -q -p no:cacheprovider "--junitxml=$reportPath"
$code = $LASTEXITCODE

if (Test-Path $reportPath) {
    Write-Host "[ci] 测试报告: $reportPath"
} else {
    Write-Warning '[ci] 没有生成 JUnit 报告，检查所选解释器里是否真的装了 pytest'
}

if ($code -ne 0) {
    Write-Host "[ci] pytest 退出码 $code：构建失败"
}
exit $code
