#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness",
    [switch]$IncludeRemoteCommander
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
$TaskNames = @(
    "RDC Harness - Desktop Agent",
    "RDC Harness - Browser Bridge"
)
if ($IncludeRemoteCommander) { $TaskNames += "RDC Harness - Remote Commander" }

foreach ($name in $TaskNames) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Write-Host "Stopped task: $name"
    }
}

$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and $_.CommandLine -like "*$InstallRoot*" -and
        ($IncludeRemoteCommander -or $_.CommandLine -notmatch "remote-commander")
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped PID $($_.ProcessId)"
    }
