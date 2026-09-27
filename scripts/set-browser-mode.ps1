#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet("headed", "headless")][string]$Mode,
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ConfigPath = Join-Path $InstallRoot "config.ps1"
if (-not (Test-Path $ConfigPath)) { throw "Harness config not found: $ConfigPath" }

$lines = Get-Content -LiteralPath $ConfigPath |
    Where-Object { $_ -notmatch '^\$env:PW_HEADED\s*=' }
$value = if ($Mode -eq "headed") { "1" } else { "0" }
$lines += "`$env:PW_HEADED = '$value'"
$encoding = New-Object Text.UTF8Encoding($true)
[IO.File]::WriteAllLines($ConfigPath, $lines, $encoding)

$task = Get-ScheduledTask -TaskName "RDC Harness - Browser Bridge" -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName "RDC Harness - Browser Bridge" -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Start-ScheduledTask -TaskName "RDC Harness - Browser Bridge"
} else {
    & (Join-Path $PSScriptRoot "stop.ps1") -InstallRoot $InstallRoot
    & (Join-Path $PSScriptRoot "start.ps1") -InstallRoot $InstallRoot
}
Write-Host "Browser Bridge mode: $Mode"
