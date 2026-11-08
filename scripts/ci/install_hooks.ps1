#Requires -Version 7.0
<#
.SYNOPSIS
    把 .githooks\ 里的钩子装进 .git\hooks\（不碰 git config）。

.DESCRIPTION
    仓库里只保存钩子源码（.githooks\），git 本身不会自动用它们；
    这个脚本负责复制并强制写成 LF 换行——钩子由 sh 执行，CRLF 会把
    shebang 变成 ``/bin/sh^M``，报 "bad interpreter"。

    装好的钩子只有在提交信息带 ``[release]`` 时才启动发版流水线。

.EXAMPLE
    pwsh -NoProfile -File scripts\ci\install_hooks.ps1
.EXAMPLE
    pwsh -NoProfile -File scripts\ci\install_hooks.ps1 -Uninstall
#>
[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$src = Join-Path $repo '.githooks'
$dst = Join-Path $repo '.git\hooks'
if (-not (Test-Path $src)) { throw "找不到钩子源码目录 $src" }
if (-not (Test-Path $dst)) { throw "找不到 $dst：这不是 git 工作副本？" }

# UTF-8 不带 BOM：BOM 出现在 shebang 前面会让 sh 也认不出这行
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Get-HookText {
    param([string]$Path)
    return ((Get-Content $Path -Raw) -replace "`r`n", "`n")
}

foreach ($file in Get-ChildItem $src -File) {
    $target = Join-Path $dst $file.Name
    if ($Uninstall) {
        if (Test-Path $target) {
            Remove-Item $target -Force
            Write-Host "[hooks] 卸载 $($file.Name)"
        }
        continue
    }
    if (Test-Path $target) {
        $same = (Get-HookText $target) -eq (Get-HookText $file.FullName)
        if (-not $same) {
            $backup = "$target.bak-$(Get-Date -Format yyyyMMddHHmmss)"
            Copy-Item $target $backup
            Write-Host "[hooks] 原有 $($file.Name) 不是这份，已备份到 $backup"
        }
    }
    [IO.File]::WriteAllText($target, (Get-HookText $file.FullName), $utf8NoBom)
    Write-Host "[hooks] 安装 $($file.Name)（LF 换行）"
}

if ($Uninstall) {
    Write-Host '[hooks] 已卸载'
} else {
    Write-Host '[hooks] 完成：提交信息带 [release] 会自动跑发版流水线'
}
