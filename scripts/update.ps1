#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness",
    [switch]$Force,
    [switch]$KeepDownload
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$StatePath = Join-Path $InstallRoot "install-state.json"
$CheckScript = Join-Path $PSScriptRoot "check-update.ps1"

if (-not (Test-Path -LiteralPath $StatePath)) {
    throw "Install state is missing: $StatePath. Re-run scripts\install.ps1 once."
}
if (-not (Test-Path -LiteralPath $CheckScript)) {
    throw "Update checker is missing: $CheckScript"
}

$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
$checkRaw = & $CheckScript -InstallRoot $InstallRoot -Json
$check = $checkRaw | ConvertFrom-Json
if (-not $check.update_available -and -not $Force) {
    Write-Host "RDC Workstation Harness is already up to date." -ForegroundColor Green
    exit 0
}
$downloadUrl = [string]$check.download_url
if (-not $downloadUrl) { throw "GitHub did not provide a source archive URL." }
$repository = [string]$check.repository
if ($repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw "Invalid GitHub repository in install state."
}
$archiveUri = [uri]$downloadUrl
$expectedApiPrefix = "/repos/$repository/zipball/"
$expectedWebPrefix = "/$repository/archive/"
if ($archiveUri.Scheme -ne "https" -or
    -not (($archiveUri.Host -eq "api.github.com" -and $archiveUri.AbsolutePath.StartsWith($expectedApiPrefix)) -or
           ($archiveUri.Host -eq "github.com" -and $archiveUri.AbsolutePath.StartsWith($expectedWebPrefix)))) {
    throw "Update archive URL is outside the configured GitHub repository."
}

$tempRoot = Join-Path $env:TEMP ("rdc-harness-update-" + [guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $tempRoot "source.zip"
$extractDir = Join-Path $tempRoot "source"
New-Item -ItemType Directory -Path $tempRoot, $extractDir -Force | Out-Null

$headers = @{ "User-Agent" = "rdc-workstation-harness-updater" }
Write-Host "Downloading $($check.remote_identifier)..." -ForegroundColor Cyan
Invoke-WebRequest -Uri $downloadUrl -Headers $headers -OutFile $zipPath -UseBasicParsing
Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

$installScript = Get-ChildItem -Path $extractDir -Filter "install.ps1" -File -Recurse |
    Where-Object { $_.FullName -match "\\scripts\\install\.ps1$" } |
    Select-Object -First 1
if (-not $installScript) { throw "Downloaded archive does not contain scripts\install.ps1." }

$backupRoot = Join-Path $InstallRoot "backups"
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$backupZip = Join-Path $backupRoot ("pre-update-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".zip")
$backupStage = Join-Path $tempRoot "backup"
New-Item -ItemType Directory -Path $backupStage -Force | Out-Null
$backupItems = @(
    ".venv", "desktop-agent", "browser-bridge", "remote-commander", "tools", "maintenance",
    "config.ps1", "run-desktop-agent.ps1", "run-browser-bridge.ps1",
    "run-remote-commander.ps1", "install-state.json", "VERSION", "CHANGELOG.md"
)
foreach ($item in $backupItems) {
    $source = Join-Path $InstallRoot $item
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $backupStage -Recurse -Force
    }
}
Compress-Archive -Path (Join-Path $backupStage "*") -DestinationPath $backupZip -Force
Write-Host "Backup: $backupZip"

$serviceTasks = @("RDC Harness - Desktop Agent", "RDC Harness - Browser Bridge")
foreach ($taskName in $serviceTasks) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }
}
$installArgs = @{
    InstallRoot = $InstallRoot
    DesktopAgentPort = [int]$state.desktop_agent_port
    BrowserBridgePort = [int]$state.browser_bridge_port
    Browser = [string]$state.browser
    RemoteCommanderVersion = [string]$state.remote_commander_version
    UpdateChannel = [string]$state.update_channel
}
if ([bool]$state.install_bundled_chromium) { $installArgs.InstallBundledChromium = $true }
if ([bool]$state.remote_commander_installed) { $installArgs.InstallRemoteCommander = $true }
if ([bool]$state.no_autostart) { $installArgs.NoAutostart = $true }
if (-not [bool]$state.update_check_enabled) { $installArgs.NoUpdateCheck = $true }

$oldSourceRef = $env:RDC_HARNESS_SOURCE_REF
$oldSourceVersion = $env:RDC_HARNESS_SOURCE_VERSION
$env:RDC_HARNESS_SOURCE_REF = [string]$check.remote_identifier
$env:RDC_HARNESS_SOURCE_VERSION = [string]$check.remote_version

try {
    Write-Host "Applying update..." -ForegroundColor Cyan
    & $installScript.FullName @installArgs
    Write-Host "Update applied successfully." -ForegroundColor Green
} catch {
    Write-Warning "Update failed. Restoring the previous runtime from $backupZip"
    Expand-Archive -LiteralPath $backupZip -DestinationPath $InstallRoot -Force
    foreach ($taskName in $serviceTasks) {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        }
    }
    throw
} finally {
    $env:RDC_HARNESS_SOURCE_REF = $oldSourceRef
    $env:RDC_HARNESS_SOURCE_VERSION = $oldSourceVersion
    if (-not $KeepDownload -and (Test-Path -LiteralPath $tempRoot)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
