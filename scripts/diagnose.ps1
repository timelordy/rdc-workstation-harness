#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness",
    [ValidateRange(1024, 65535)][int]$DesktopAgentPort = 17322,
    [ValidateRange(1024, 65535)][int]$BrowserBridgePort = 17321,
    [ValidateRange(1024, 65535)][int]$RevitBridgePort = 17323
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$script:Failures = 0
$script:Warnings = 0

function Report([string]$Level, [string]$Message) {
    switch ($Level) {
        "OK" { $color = "Green" }
        "WARN" { $color = "Yellow"; $script:Warnings++ }
        default { $color = "Red"; $script:Failures++ }
    }
    Write-Host ("[{0}] {1}" -f $Level, $Message) -ForegroundColor $color
}

function Test-CommandVersion([string]$Command, [version]$Minimum) {
    $found = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $found) { Report "FAIL" "$Command is not on PATH"; return }
    try {
        $raw = & $found.Source --version 2>&1 | Out-String
        $match = [regex]::Match($raw, "\d+(\.\d+){1,3}")
        if ($match.Success -and [version]$match.Value -ge $Minimum) {
            Report "OK" "$Command $($match.Value)"
        } else { Report "FAIL" "$Command is older than $Minimum ($raw)" }
    } catch { Report "FAIL" "$Command failed: $($_.Exception.Message)" }
}
function Test-Health([string]$Name, [int]$Port, [bool]$Required) {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        if ($response.ok -or $response.ready) {
            Report "OK" "$Name is healthy on 127.0.0.1:$Port"
        } else { Report "FAIL" "$Name returned an unhealthy response" }
    } catch {
        if ($Required) { Report "FAIL" "$Name is not responding on port $Port" }
        else { Report "WARN" "$Name is not running on port $Port (optional)" }
    }
}

Write-Host "RDC Workstation Harness diagnostics" -ForegroundColor Cyan
Write-Host "Install root: $InstallRoot`n"

if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) { Report "OK" "Windows host detected" }
else { Report "FAIL" "This project supports Windows only" }

Test-CommandVersion "python.exe" ([version]"3.10")
Test-CommandVersion "node.exe" ([version]"18.0")
if (Get-Command "npm.cmd" -ErrorAction SilentlyContinue) { Report "OK" "npm.cmd is available" }
else { Report "FAIL" "npm.cmd is not on PATH" }

$RequiredFiles = @(
    "config.ps1",
    ".venv\Scripts\python.exe",
    "desktop-agent\agent.py",
    "browser-bridge\bridge.js",
    "maintenance\check-update.ps1",
    "maintenance\update.ps1",
    "install-state.json",
    "run-desktop-agent.ps1",
    "run-browser-bridge.ps1"
)
foreach ($relative in $RequiredFiles) {
    $path = Join-Path $InstallRoot $relative
    if (Test-Path $path) { Report "OK" "$relative exists" }
    else { Report "FAIL" "$relative is missing" }
}
$TokenDir = Join-Path $env:USERPROFILE ".chatgpt-desktop-agent"
foreach ($name in @("desktop.token", "browser.token")) {
    $path = Join-Path $TokenDir $name
    if (-not (Test-Path $path)) {
        Report "FAIL" "$name is missing"
        continue
    }
    $length = ((Get-Content -LiteralPath $path -Raw).Trim()).Length
    if ($length -ge 64) { Report "OK" "$name exists and has a plausible length" }
    else { Report "FAIL" "$name is unexpectedly short" }
}

Test-Health "Desktop agent" $DesktopAgentPort $true
Test-Health "Browser bridge" $BrowserBridgePort $true
Test-Health "Revit bridge" $RevitBridgePort $false

$TaskNames = @(
    "RDC Harness - Desktop Agent",
    "RDC Harness - Browser Bridge"
)
foreach ($name in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) { Report "OK" "Scheduled task '$name' is $($task.State)" }
    else { Report "WARN" "Scheduled task '$name' is absent (NoAutostart may be intentional)" }
}

$RemoteTask = Get-ScheduledTask -TaskName "RDC Harness - Remote Commander" -ErrorAction SilentlyContinue
if ($RemoteTask) { Report "OK" "Remote Commander task is $($RemoteTask.State)" }
else { Report "WARN" "Remote Commander task is not installed" }

$UpdateTask = Get-ScheduledTask -TaskName "RDC Harness - Update Check" -ErrorAction SilentlyContinue
if ($UpdateTask) { Report "OK" "Update navigator task is $($UpdateTask.State)" }
else { Report "WARN" "Update navigator task is absent (NoUpdateCheck may be intentional)" }

$DeviceIdentity = Join-Path $env:USERPROFILE ".desktop-commander-device\device.json"
if (Test-Path $DeviceIdentity) { Report "OK" "Remote Commander has a local device identity" }
else { Report "WARN" "Remote Commander is not paired on this workstation" }
try {
    $listeners = Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -in @($DesktopAgentPort, $BrowserBridgePort, $RevitBridgePort) }
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -in @("127.0.0.1", "::1")) {
            Report "OK" "Port $($listener.LocalPort) is loopback-only"
        } else {
            Report "FAIL" "Port $($listener.LocalPort) is not loopback-only"
        }
    }
} catch {
    Report "WARN" "Could not inspect TCP listeners: $($_.Exception.Message)"
}

$LegacyTasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $_.TaskName -match "Remote Desktop Commander|DesktopAgent|PlaywrightAgent" -and
    $_.TaskName -notlike "RDC Harness*"
})
if ($LegacyTasks.Count -gt 0) {
    Report "WARN" "Legacy/duplicate-looking tasks found: $($LegacyTasks.TaskName -join ', ')"
} else {
    Report "OK" "No legacy harness tasks detected"
}
Write-Host ""
if ($script:Failures -gt 0) {
    Write-Host "Result: $($script:Failures) failure(s), $($script:Warnings) warning(s)" -ForegroundColor Red
    exit 1
}
if ($script:Warnings -gt 0) {
    Write-Host "Result: healthy core with $($script:Warnings) warning(s)" -ForegroundColor Yellow
    exit 0
}
Write-Host "Result: all checks passed" -ForegroundColor Green
exit 0
