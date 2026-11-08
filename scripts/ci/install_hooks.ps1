#Requires -Version 7.0
<#
.SYNOPSIS
    把 .githooks\ 里的钩子装到 git 真正会读的目录（不碰 git config）。

.DESCRIPTION
    仓库里只保存钩子源码（.githooks\），git 本身不会自动用它们；这个脚本负责
    复制成 LF 换行（CRLF 会让 shebang 变成 ``/bin/sh^M``，报 bad interpreter）。

    装到哪里由 git 说了算：设了 ``core.hooksPath`` 就装那儿，否则装 ``.git\hooks``。
    本仓库的 ``core.hooksPath`` 指向 ``C:\Users\ASUS\.empty-hooks``（git-lfs 的钩子目录），
    只往 .git\hooks 装是永远不会被执行的。

    目标位置已经存在别的钩子（例如 git-lfs）时不覆盖：原样保留成 ``<name>.orig``，
    再串到我们钩子的 shebang 后面一起跑。

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
if (-not (Test-Path $src)) { throw "找不到钩子源码目录 $src" }
if (-not (Test-Path (Join-Path $repo '.git'))) { throw "这不是 git 工作副本：$repo" }

# git 只认 core.hooksPath（设了的话），相对路径按仓库根算
$configured = (& git -C $repo config --get core.hooksPath 2>$null)
if ($configured) {
    $dst = if ([IO.Path]::IsPathRooted($configured)) { $configured } else { Join-Path $repo $configured }
    Write-Host "[hooks] core.hooksPath=$configured → 装到 $dst"
} else {
    $dst = Join-Path $repo '.git\hooks'
}
if (-not (Test-Path $dst)) { New-Item -ItemType Directory -Path $dst -Force | Out-Null }

# UTF-8 不带 BOM：BOM 出现在 shebang 前面会让 sh 也认不出这行
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Get-HookText {
    param([string]$Path)
    return ((Get-Content $Path -Raw) -replace "`r`n", "`n")
}

$marker = '# 由 scripts/ci/install_hooks.ps1 安装（重装会覆盖这里）'

foreach ($file in Get-ChildItem $src -File) {
    $target = Join-Path $dst $file.Name
    $orig = "$target.orig"
    if ($Uninstall) {
        if ((Test-Path $target) -and
            ((Get-HookText $target) -split "`n") -contains $marker) {
            Remove-Item $target -Force
            Write-Host "[hooks] 卸载 $($file.Name)"
        }
        if (Test-Path $orig) {
            Move-Item $orig $target -Force
            Write-Host "[hooks] 已恢复原有的 $($file.Name)"
        }
        continue
    }

    # 目标位置原有的钩子（如 git-lfs）留成 .orig，再串到我们钩子前面一起跑
    $chain = ''
    if (Test-Path $target) {
        $installedByUs = ((Get-HookText $target) -split "`n") -contains $marker
        if (-not $installedByUs -and -not (Test-Path $orig)) {
            Move-Item $target $orig -Force
            Write-Host "[hooks] 原有 $($file.Name) 已保留为 $orig（会串联执行）"
        } elseif (-not $installedByUs) {
            Remove-Item $target -Force
        }
    }
    if (Test-Path $orig) {
        $origSh = ($orig -replace '\\', '/')
        $chain = "if [ -f `"$origSh`" ]; then sh `"$origSh`" `"`$@`"; fi"
    }

    # 生成文件 = 原 shebang + 标记 + 串联行 + 钩子正文（正文里有 exit，所以串联行必须在前面）
    $lines = (Get-HookText $file.FullName) -split "`n", 2
    $parts = @($lines[0], $marker)
    if ($chain) { $parts += $chain }
    if ($lines.Count -gt 1) { $parts += $lines[1] }
    [IO.File]::WriteAllText($target, ($parts -join "`n"), $utf8NoBom)
    Write-Host "[hooks] 安装 $($file.Name)（LF 换行，$target）"
}

if ($Uninstall) {
    Write-Host '[hooks] 已卸载'
} else {
    Write-Host '[hooks] 完成：提交信息带 [release] 会自动跑发版流水线'
}
