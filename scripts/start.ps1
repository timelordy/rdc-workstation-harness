#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness",
    [switch]$IncludeRemoteCommander
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$TaskNames = @(
    "RDC Harness - Desktop Agent",
    "RDC Harness - Browser Bridge"
)
if ($IncludeRemoteCommander) { $TaskNames += "RDC Harness - Remote Commander" }

foreach ($name in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Start-ScheduledTask -TaskName $name
        Write-Host "Started task: $name"
        continue
    }
    $runner = switch -Wildcard ($name) {
        "*Desktop Agent" { "run-desktop-agent.ps1" }
        "*Browser Bridge" { "run-browser-bridge.ps1" }
        default { "run-remote-commander.ps1" }
    }
    $path = Join-Path $InstallRoot $runner
    if (-not (Test-Path $path)) { throw "Runner not found: $path" }
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $path)
    Write-Host "Started runner: $path"
}
