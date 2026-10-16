<#
.SYNOPSIS
    在目标机器上信任 StockChart 的自签代码签名证书（一次性，需管理员）。

.DESCRIPTION
    自签证书链不来自公网 CA，Windows 默认不认。把公钥证书（.cer，不含私钥，可随便分发）
    同时导入 LocalMachine 的 Root 与 TrustedPublisher 两个存储后，资源管理器的属性页会显示
    「数字签名有效」，SmartScreen 也不再按「未知发布者」拦截下载的文件。

    注意：这只是「我们自己造信任」，不能替代商签证书的公网信誉；对内部/自用机器足够。

.EXAMPLE
    以管理员身份运行：
    powershell -ExecutionPolicy Bypass -File trust_signing_cert.ps1
    powershell -ExecutionPolicy Bypass -File trust_signing_cert.ps1 -CerPath .\stockchart-codeSigning.cer
#>
[CmdletBinding()]
param(
    [string]$CerPath,
    [string]$Url = 'https://data.hsdc.cloud/updates/stockchart-codeSigning.cer'
)

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw '需要管理员权限：请右键“以管理员身份运行”PowerShell 后重试' }

if (-not $CerPath) { $CerPath = Join-Path $PSScriptRoot '..\signing\stockchart-codeSigning.cer' }
$CerPath = [IO.Path]::GetFullPath($CerPath)

if (-not (Test-Path $CerPath)) {
    Write-Host "[i] 本地没有证书，尝试从服务器获取: $Url"
    New-Item -ItemType Directory -Force -Path (Split-Path $CerPath) | Out-Null
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $CerPath -UseBasicParsing
    } catch {
        throw "本地找不到 $CerPath，在线获取也失败：$_"
    }
}

# certutil 在 Win7/PS2.0 上就有，比 Import-Certificate 兼容；失败再退回 PS  cmdlet
function Add-ToStore([string]$store) {
    & certutil.exe -addstore -f $store $CerPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Import-Certificate -FilePath $CerPath -CertStoreLocation "Cert:\LocalMachine\$store" | Out-Null
    }
}

Add-ToStore 'Root'
Add-ToStore 'TrustedPublisher'

$pfxInfo = Get-PfxCertificate -FilePath $CerPath -ErrorAction SilentlyContinue
if ($pfxInfo) { Write-Host "[OK] 已信任自签证书（Root + TrustedPublisher），指纹 $($pfxInfo.Thumbprint)" }
else { Write-Host '[OK] 已信任自签证书（Root + TrustedPublisher）' }
Write-Host '下一步：把程序放到可写目录（例如 %LOCALAPPDATA%\Programs\StockChart），自动更新即可无人值守替换重启。'
