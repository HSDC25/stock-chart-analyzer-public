<#
.SYNOPSIS
    生成/续期 StockChart 的免费自签代码签名证书（Self-signed Code Signing Certificate）。

.DESCRIPTION
    公网可信任的代码签名证书没有免费的（DigiCert/Sectigo 约 $100+/年，Azure Trusted
    Signing 需要企业主体）。自签证书是免费且能自动续期的方案：签名可验证「包未被篡改」，
    但信任要靠把证书分发到目标机器（trust_signing_cert.ps1）。

    续期策略：证书存在且剩余 > 30 天则复用；否则新建一张（有效期 3 年）。
    旧签名不会失效——只要签名时带了 RFC3161 时间戳（sign_executables.ps1 默认带）。

.EXAMPLE
    $env:SIGN_PFX_PASSWORD='读一次性的强密码'; .\scripts\new_signing_cert.ps1
    .\scripts\new_signing_cert.ps1 -PfxPassword (Read-Host -AsSecureString)
#>
[CmdletBinding()]
param(
    [string]$Subject = 'CN=HSDC StockChart Code Signing, O=HSDC',
    [int]$YearsValid = 3,
    [int]$RenewWhenDaysLeft = 30,
    [string]$StoreDir,
    [securestring]$PfxPassword
)

$ErrorActionPreference = 'Stop'
if (-not $StoreDir) { $StoreDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'signing' }
$StoreDir = [IO.Path]::GetFullPath($StoreDir)
New-Item -ItemType Directory -Force -Path $StoreDir | Out-Null

if (-not $PfxPassword) {
    if ($env:SIGN_PFX_PASSWORD) {
        $PfxPassword = ConvertTo-SecureString $env:SIGN_PFX_PASSWORD -AsPlainText -Force
    } else {
        # PFX 含私钥，必须有密码；密码只需记在构建机上（发布时用同一台机器即可）
        $PfxPassword = Read-Host 'PFX 保护密码' -AsSecureString
    }
}

# 1. 已有证书还够用就复用（同一台构建机上保持稳定身份，目标机只需信任一次）
$existing = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
            Where-Object { $_.Subject -eq $Subject } |
            Sort-Object NotAfter -Descending | Select-Object -First 1
if ($existing -and ($existing.NotAfter - (Get-Date)).Days -gt $RenewWhenDaysLeft) {
    Write-Host "[OK] 复用现有证书: $($existing.Thumbprint) 有效期至 $($existing.NotAfter)"
    $cert = $existing
} else {
    if ($existing) { Write-Host "[i] 证书将在 $($existing.NotAfter) 过期（< $RenewWhenDaysLeft 天），自动续期" }
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $Subject `
        -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 `
        -KeyUsage DigitalSignature `
        -CertStoreLocation Cert:\CurrentUser\My `
        -NotAfter (Get-Date).AddYears($YearsValid) `
        -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3')
    Write-Host "[OK] 新建证书: $($cert.Thumbprint) 有效期至 $($cert.NotAfter)"
}

# 2. 导出：PFX（含私钥，留在构建机，绝不入库）+ CER（公钥，发给目标机器）
$pfx = Join-Path $StoreDir 'stockchart-codeSigning.pfx'
$cer = Join-Path $StoreDir 'stockchart-codeSigning.cer'
Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $PfxPassword | Out-Null
Export-Certificate -Cert $cert -FilePath $cer | Out-Null

Write-Host ""
Write-Host "PFX  : $pfx      （构建机专用，含私钥，已加入 .gitignore）"
Write-Host "CER  : $cer      （分发到目标机器执行 trust_signing_cert.ps1）"
Write-Host "指纹 : $($cert.Thumbprint)"
Write-Host "下一步: .\scripts\sign_executables.ps1"
