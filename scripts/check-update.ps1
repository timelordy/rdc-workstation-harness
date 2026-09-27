#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness",
    [string]$Repository = "timelordy/rdc-workstation-harness",
    [ValidateSet("stable", "main")]
    [string]$Channel,
    [switch]$Notify,
    [switch]$OpenDownload,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$StatePath = Join-Path $InstallRoot "install-state.json"
$NotificationStatePath = Join-Path $InstallRoot "update-state.json"
$AvailablePath = Join-Path $InstallRoot "update-available.json"

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { return $null }
}
function Write-JsonFile([string]$Path, $Value) {
    $parent = Split-Path -Parent $Path
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $json = $Value | ConvertTo-Json -Depth 8
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $encoding)
}

function Normalize-Version([string]$Value) {
    if (-not $Value) { return $null }
    return $Value.Trim().TrimStart("v", "V")
}

function Show-UpdateNotification([string]$Title, [string]$Message) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.Visible = $true
        $icon.BalloonTipTitle = $Title
        $icon.BalloonTipText = $Message
        $icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
        $icon.ShowBalloonTip(10000)
        Start-Sleep -Seconds 8
        $icon.Dispose()
    } catch {
        Write-Verbose "Notification failed: $($_.Exception.Message)"
    }
}
$state = Read-JsonFile $StatePath
if ($state) {
    if (-not $PSBoundParameters.ContainsKey("Repository") -and $state.repository) {
        $Repository = [string]$state.repository
    }
    if (-not $PSBoundParameters.ContainsKey("Channel") -and $state.update_channel) {
        $Channel = [string]$state.update_channel
    }
}
if (-not $Channel) { $Channel = "stable" }

$currentVersion = if ($state -and $state.installed_version) {
    Normalize-Version ([string]$state.installed_version)
} else { $null }
$currentCommit = if ($state -and $state.installed_commit) {
    [string]$state.installed_commit
} else { $null }

$headers = @{
    "User-Agent" = "rdc-workstation-harness-update-checker"
    "Accept" = "application/vnd.github+json"
}
$baseApi = "https://api.github.com/repos/$Repository"
if ($Channel -eq "stable") {
    $remote = Invoke-RestMethod -Uri "$baseApi/releases/latest" -Headers $headers -TimeoutSec 15
    $remoteVersion = Normalize-Version ([string]$remote.tag_name)
    $identifier = [string]$remote.tag_name
    $downloadUrl = [string]$remote.zipball_url
    $pageUrl = [string]$remote.html_url
    $publishedAt = [string]$remote.published_at
    $summary = if ($remote.name) { [string]$remote.name } else { "Release $identifier" }

    $available = $false
    if ($remoteVersion) {
        try {
            if (-not $currentVersion) { $available = $true }
            else { $available = ([version]$remoteVersion -gt [version]$currentVersion) }
        } catch {
            $available = ($remoteVersion -ne $currentVersion)
        }
    }
} else {
    $remote = Invoke-RestMethod -Uri "$baseApi/commits/main" -Headers $headers -TimeoutSec 15
    $identifier = [string]$remote.sha
    $downloadUrl = "https://github.com/$Repository/archive/refs/heads/main.zip"
    $pageUrl = [string]$remote.html_url
    $publishedAt = [string]$remote.commit.committer.date
    $summary = ([string]$remote.commit.message -split "`r?`n")[0]
    $remoteVersion = $null
    $available = (-not $currentCommit) -or (-not $identifier.StartsWith($currentCommit)) -and (-not $currentCommit.StartsWith($identifier))
}
$result = [ordered]@{
    checked_at = (Get-Date).ToUniversalTime().ToString("o")
    repository = $Repository
    channel = $Channel
    update_available = [bool]$available
    current_version = $currentVersion
    current_commit = $currentCommit
    remote_version = $remoteVersion
    remote_identifier = $identifier
    summary = $summary
    published_at = $publishedAt
    page_url = $pageUrl
    download_url = $downloadUrl
}

if ($available) {
    Write-JsonFile $AvailablePath $result
} elseif (Test-Path -LiteralPath $AvailablePath) {
    Remove-Item -LiteralPath $AvailablePath -Force -ErrorAction SilentlyContinue
}

if ($Notify -and $available) {
    $notificationState = Read-JsonFile $NotificationStatePath
    $lastNotified = if ($notificationState -and $notificationState.last_notified) {
        [string]$notificationState.last_notified
    } else { $null }
    if ($lastNotified -ne $identifier) {
        $label = if ($Channel -eq "stable") { "v$remoteVersion" } else { $identifier.Substring(0, 8) }
        Show-UpdateNotification "RDC Workstation Harness update" "$label is available. Run the installed update.ps1 to download and apply it."
        Write-JsonFile $NotificationStatePath ([ordered]@{
            last_notified = $identifier
            notified_at = (Get-Date).ToUniversalTime().ToString("o")
        })
    }
}

if ($OpenDownload -and $available) {
    Start-Process $pageUrl
}

if ($Json) {
    $result | ConvertTo-Json -Depth 6
} else {
    if ($available) {
        $label = if ($Channel -eq "stable") { "v$remoteVersion" } else { $identifier.Substring(0, 8) }
        Write-Host "Update available: $label" -ForegroundColor Yellow
        Write-Host $summary
        Write-Host "Page: $pageUrl"
        Write-Host ('Apply: powershell -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $InstallRoot "maintenance\\update.ps1"))
    } else {
        Write-Host "RDC Workstation Harness is up to date." -ForegroundColor Green
    }
}
