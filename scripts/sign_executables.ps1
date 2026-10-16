<#
.SYNOPSIS
    给构建出来的 EXE 做 Authenticode 代码签名（SHA-256 + RFC3161 时间戳）。

.DESCRIPTION
    默认用当前用户存储里那张贴胸指纹（new_signing_cert.ps1 生成的），无需 PFX 密码。
    带时间戳的意义：证书三年后续期/更换，已发布的旧签名依然被判定有效。

    边界（务必知道）：自签证书不能消除 SmartScreen 的「未知发布者」提示——那需要
    商签证书 + 下载信誉。签名在这里解决的是「文件是否被篡改」「发布者是哪个身份」，
    以及配合自动更新做完整性兜底。目标机器需先执行 trust_signing_cert.ps1 建立信任。
    Win7 上 SHA-256 签名需要 KB4474419（2019 年后的 SHA-2 支持更新）。

.EXAMPLE
    .\scripts\sign_executables.ps1                        # 签 win7/outputs 与 win11/outputs 下全部 exe
    .\scripts\sign_executables.ps1 -Paths .\update_dist   # 只签指定目录
    .\scripts\sign_executables.ps1 -VerifyOnly
#>
[CmdletBinding()]
param(
    [string[]]$Paths,
    [string]$Subject = 'CN=HSDC StockChart Code Signing, O=HSDC',
    [string]$PfxPath,
    [securestring]$PfxPassword,
    [string]$Timestamp = 'http://timestamp.digicert.com',
    [string]$Description = 'StockChart 股票图表分析工具',
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

# 1. 定位 signtool（Windows 10/11 SDK 里，取最新目录）
$signtool = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin' -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } |
            Sort-Object FullName -Descending | Select-Object -First 1
if (-not $signtool) {
    throw '未找到 signtool.exe，请安装 Windows 10/11 SDK（Visual Studio 里勾选 Windows SDK）'
}
$signtool = $signtool.FullName
Write-Host "[signtool] $signtool"

# 2. 收集待签产物
if (-not $Paths) {
    $Paths = @((Join-Path $repo 'win7\outputs'), (Join-Path $repo 'win11\outputs'))
}
$exes = foreach ($p in $Paths) {
    $full = [IO.Path]::GetFullPath($p)
    if (Test-Path $full -PathType Leaf) { Get-Item $full }
    elseif (Test-Path $full -PathType Container) { Get-ChildItem $full -Filter *.exe -Recurse }
}
$exes = @($exes | Where-Object { $_.Length -gt 0 })
if (-not $exes.Count) { throw '没有要签名的 exe（先跑构建脚本，或用 -Paths 指定）' }

# 3. 签名参数：优先 PFX，其次用户存储里的证书指纹
if ($VerifyOnly) {
    $signArgs = @()
} elseif ($PfxPath) {
    $signArgs = @('sign', '/f', $PfxPath, '/fd', 'SHA256', '/td', 'SHA256', '/tr', $Timestamp, '/d', $Description)
    if ($PfxPassword) {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringUni(
            [Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($PfxPassword))
        $signArgs = @('sign', '/f', $PfxPath, '/p', $plain, '/fd', 'SHA256', '/td', 'SHA256', '/tr', $Timestamp, '/d', $Description)
    }
} else {
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
            Where-Object { $_.Subject -eq $Subject } |
            Sort-Object NotAfter -Descending | Select-Object -First 1
    if (-not $cert) { throw "找不到证书 $Subject，请先执行 new_signing_cert.ps1（或改用 -PfxPath）" }
    if (($cert.NotAfter - (Get-Date)).Days -lt 30) {
        Write-Warning "证书将在 $($cert.NotAfter) 过期（不足 30 天），建议先跑 new_signing_cert.ps1 续期"
    }
    $signArgs = @('sign', '/sha1', $cert.Thumbprint, '/fd', 'SHA256', '/td', 'SHA256', '/tr', $Timestamp, '/d', $Description)
}

# 4. 逐个签名 + 校验
# verify /pa 走的是系统信任链：自签证书在目标机执行 trust_signing_cert.ps1 之前必然报
# 「terminated in a root certificate which is not trusted」，那是预期结果，不代表签名坏了。
# 因此把它单独计数，只用「签名是否写入 / 是否有其它错误」判定失败。
$fail = 0
$untrusted = 0
# signtool 会把错误写到 stderr，配合 2>&1 捕获时 PowerShell 会把它当错误记录抛出来；
# 这里靠 $LASTEXITCODE 判定成败，所以暂时放宽偏好，避免 'Stop' 把循环打断。
$ErrorActionPreference = 'Continue'
foreach ($exe in $exes) {
    if (-not $VerifyOnly) {
        Write-Host "`n[sign] $($exe.Name) ($([math]::Round($exe.Length/1MB,1)) MB)"
        $null = & $signtool @signArgs $exe.FullName 2>&1
        if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] 签名失败: $($exe.Name)"; $fail++; continue }
    }
    $verify = & $signtool verify /pa /v $exe.FullName 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK] 签名有效且受信: $($exe.Name)"
    } elseif ($verify -match 'not trusted by the trust provider') {
        $untrusted++
        Write-Host "[SIGN-OK/未受信] $($exe.Name)：签名与时间戳都在，只是本机尚未信任自签根证书"
    } else {
        Write-Host "[FAIL] 签名校验不通过: $($exe.Name)"
        Write-Host ($verify -split "`r?`n" | Select-Object -Last 6 | Out-String)
        $fail++
    }
}

$ErrorActionPreference = 'Stop'
Write-Host "`n完成：共 $($exes.Count) 个，失败 $fail 个，未建立信任 $untrusted 个"
if ($fail) { exit 1 }
if ($untrusted) {
    Write-Host '提示：在目标机（管理员）执行 .\scripts\trust_signing_cert.ps1 后，verify /pa 才会通过。'
    Write-Host '      SmartScreen 的「未知发布者」提示仍会出现——自签证书无法消除，那需要商业代码签名证书。'
}
