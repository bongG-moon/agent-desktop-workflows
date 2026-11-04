param([string]$OutputPath = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) { $OutputPath = Join-Path $projectRoot 'bin/WorkflowNative.exe' }
$outputParent = Split-Path -Parent ([IO.Path]::GetFullPath($OutputPath))
if (-not (Test-Path -LiteralPath $outputParent)) { New-Item -ItemType Directory -Path $outputParent | Out-Null }
$frameworkRoot = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319'
$compilerPath = Join-Path $frameworkRoot 'csc.exe'
if (-not (Test-Path -LiteralPath $compilerPath)) { throw 'Local .NET Framework C# compiler is required. No download is performed.' }
$wpfRoot = Join-Path $frameworkRoot 'WPF'
$references = @('System.dll','System.Core.dll','System.Drawing.dll','System.Windows.Forms.dll','System.Web.Extensions.dll', (Join-Path $wpfRoot 'UIAutomationClient.dll'), (Join-Path $wpfRoot 'UIAutomationTypes.dll'), (Join-Path $wpfRoot 'WindowsBase.dll'))
$compilerArgs = @('/nologo','/target:winexe','/platform:x64','/optimize+','/codepage:65001',('/out:' + ([IO.Path]::GetFullPath($OutputPath))))
$compilerArgs += $references | ForEach-Object { '/reference:' + $_ }
$compilerArgs += (Join-Path $projectRoot 'native/WorkflowNative.cs')
& $compilerPath @compilerArgs
if ($LASTEXITCODE -ne 0) { throw 'Native helper build failed.' }
$nativeManifest = [ordered]@{
    version = '0.1.0'
    sha256 = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    source_sha256 = (Get-FileHash -LiteralPath (Join-Path $projectRoot 'native/WorkflowNative.cs') -Algorithm SHA256).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText((Join-Path $outputParent 'native-manifest.json'), ($nativeManifest | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256 | Select-Object Hash
