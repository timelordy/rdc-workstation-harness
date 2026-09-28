#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness",
    [ValidateRange(1024, 65535)][int]$DesktopAgentPort = 17322,
    [ValidateRange(1024, 65535)][int]$BrowserBridgePort = 17321,
    # Accepted only so the v0.1.0 updater can invoke this installer.
    [ValidateRange(1024, 65535)][int]$RevitBridgePort = 17323,
    [ValidateSet("auto", "brave", "chrome", "edge", "bundled")]
    [string]$Browser = "auto",
    [switch]$InstallBundledChromium,
    [switch]$InstallRemoteCommander,
    [string]$RemoteCommanderVersion = "0.2.50",
    [ValidateSet("stable", "main")]
    [string]$UpdateChannel = "stable",
    [switch]$NoUpdateCheck,
    [switch]$RotateTokens,
    [switch]$NoAutostart,
    [switch]$SkipStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$TokenDir = Join-Path $env:USERPROFILE ".chatgpt-desktop-agent"
$TaskPrefix = "RDC Harness"

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}
function Assert-Command([string]$Name, [string]$Hint) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "$Name is required. $Hint"
    }
    return $command.Source
}

function Assert-Version([string]$Actual, [version]$Minimum, [string]$Name) {
    $match = [regex]::Match($Actual, "\d+(\.\d+){1,3}")
    if (-not $match.Success -or [version]$match.Value -lt $Minimum) {
        throw "$Name $Minimum or newer is required; detected: $Actual"
    }
}
function Assert-NativeExit([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

function New-SecureToken {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $parent = Split-Path -Parent $Path
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Write-PowerShellFile([string]$Path, [string]$Content) {
    $parent = Split-Path -Parent $Path
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $encoding = New-Object Text.UTF8Encoding($true)
    [IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Quote-Single([string]$Value) {
    return $Value.Replace("'", "''")
}
function Find-FirstExisting([string[]]$Candidates) {
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}
function Resolve-BrowserExecutable([string]$Name) {
    $roots = @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}) |
        Where-Object { $_ }
    switch ($Name) {
        "brave" {
            return Find-FirstExisting ($roots | ForEach-Object {
                Join-Path $_ "BraveSoftware\Brave-Browser\Application\brave.exe" })
        }
        "chrome" {
            return Find-FirstExisting ($roots | ForEach-Object {
                Join-Path $_ "Google\Chrome\Application\chrome.exe" })
        }
        "edge" {
            return Find-FirstExisting ($roots | ForEach-Object {
                Join-Path $_ "Microsoft\Edge\Application\msedge.exe" })
        }
        "auto" {
            foreach ($candidate in @("brave", "chrome", "edge")) {
                $found = Resolve-BrowserExecutable $candidate
                if ($found) { return $found }
            }
            return $null
        }
        default { return $null }
    }
}
function Register-HarnessTask([string]$Name, [string]$ScriptPath) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal `
        -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Installed by rdc-workstation-harness" `
        -Force | Out-Null
}

function Register-UpdateTask([string]$Name, [string]$ScriptPath) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`" -InstallRoot `"$InstallRoot`" -Notify"
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User $identity),
        (New-ScheduledTaskTrigger -Daily -At 10:00)
    )
    $principal = New-ScheduledTaskPrincipal `
        -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers `
        -Principal $principal -Settings $settings `
        -Description "Checks GitHub for RDC Workstation Harness updates" -Force | Out-Null
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This harness targets Windows 10/11."
}

Write-Step "Checking prerequisites"
$Python = Assert-Command "python.exe" "Install Python 3.10+ and enable Add Python to PATH."
$Node = Assert-Command "node.exe" "Install Node.js 18+ LTS."
$Npm = Assert-Command "npm.cmd" "npm is installed with Node.js."
Assert-Version (& $Python --version 2>&1 | Out-String) ([version]"3.10") "Python"
Assert-Version (& $Node --version 2>&1 | Out-String) ([version]"18.0") "Node.js"
Write-Step "Copying runtime files"
$DesktopDir = Join-Path $InstallRoot "desktop-agent"
$BrowserDir = Join-Path $InstallRoot "browser-bridge"
$ToolsDir = Join-Path $InstallRoot "tools"
$MaintenanceDir = Join-Path $InstallRoot "maintenance"
$LogDir = Join-Path $InstallRoot "logs"
$ProfileDir = Join-Path $InstallRoot "browser-profile"
$ArtifactDir = Join-Path $env:USERPROFILE "Downloads\rdc-harness"
$BrowserArtifactDir = Join-Path $env:USERPROFILE "Downloads\rdc-harness-browser"

@($InstallRoot, $DesktopDir, $BrowserDir, $ToolsDir, $MaintenanceDir, $LogDir, $ProfileDir,
  $ArtifactDir, $BrowserArtifactDir, $TokenDir) |
    ForEach-Object { New-Item -ItemType Directory -Path $_ -Force | Out-Null }

Copy-Item -Path (Join-Path $RepoRoot "src\desktop_agent\*") `
    -Destination $DesktopDir -Recurse -Force
Copy-Item -Path (Join-Path $RepoRoot "src\browser_bridge\*") `
    -Destination $BrowserDir -Recurse -Force
Copy-Item -Path (Join-Path $RepoRoot "tools\*") `
    -Destination $ToolsDir -Recurse -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\check-update.ps1") `
    -Destination $MaintenanceDir -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\update.ps1") `
    -Destination $MaintenanceDir -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot "VERSION") `
    -Destination $InstallRoot -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot "CHANGELOG.md") `
    -Destination $InstallRoot -Force

Get-ChildItem -Path $InstallRoot -Directory -Filter "__pycache__" -Recurse `
    -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force

Write-Step "Creating isolated Python environment"
$VenvDir = Join-Path $InstallRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    & $Python -m venv $VenvDir
    Assert-NativeExit "Creating Python environment"
}
& $VenvPython -m pip install --disable-pip-version-check --quiet `
    -r (Join-Path $DesktopDir "requirements.txt")
Assert-NativeExit "Installing Python dependencies"
Write-Step "Installing Node.js dependencies"
Push-Location $BrowserDir
try {
    & $Npm ci --ignore-scripts --no-audit --no-fund
    Assert-NativeExit "Installing Browser Bridge dependencies"
} finally { Pop-Location }
Push-Location (Join-Path $DesktopDir "mcp")
try {
    & $Npm ci --ignore-scripts --no-audit --no-fund
    Assert-NativeExit "Installing MCP dependencies"
} finally { Pop-Location }

$BrowserExe = $null
if ($Browser -eq "bundled") {
    $InstallBundledChromium = $true
} else {
    $BrowserExe = Resolve-BrowserExecutable $Browser
}
if ($Browser -ne "auto" -and $Browser -ne "bundled" -and -not $BrowserExe) {
    throw "Selected browser '$Browser' was not found in standard install locations."
}
if ($Browser -eq "auto" -and -not $BrowserExe) {
    Write-Warning "No supported system browser found; installing Playwright Chromium."
    $InstallBundledChromium = $true
}
if ($InstallBundledChromium) {
    Write-Step "Installing Playwright Chromium"
    Push-Location $BrowserDir
    try {
        & (Join-Path $BrowserDir "node_modules\.bin\playwright.cmd") install chromium
        Assert-NativeExit "Installing Playwright Chromium"
    } finally { Pop-Location }
}

Write-Step "Creating local credentials"
foreach ($name in @("desktop.token", "browser.token")) {
    $path = Join-Path $TokenDir $name
    if ($RotateTokens -or -not (Test-Path $path)) {
        Write-Utf8NoBom $path (New-SecureToken)
    }
}

$ConfigPath = Join-Path $InstallRoot "config.ps1"
$Config = @"
`$env:RDC_HARNESS_INSTALL_ROOT = '$(Quote-Single $InstallRoot)'
`$env:RDC_HARNESS_TOKEN_DIR = '$(Quote-Single $TokenDir)'
`$env:DESKTOP_AGENT_PORT = '$DesktopAgentPort'
`$env:PW_BRIDGE_PORT = '$BrowserBridgePort'
`$env:NODE_EXE = '$(Quote-Single $Node)'
`$env:DESKTOP_AGENT_ARTIFACT_DIR = '$(Quote-Single $ArtifactDir)'
`$env:PW_PROFILE_DIR = '$(Quote-Single $ProfileDir)'
`$env:PW_ARTIFACT_DIR = '$(Quote-Single $BrowserArtifactDir)'
`$env:DESKTOP_AGENT_LOG = '$(Quote-Single (Join-Path $LogDir "desktop-agent.log"))'
`$env:PW_BRIDGE_LOG = '$(Quote-Single (Join-Path $LogDir "browser-bridge.log"))'
`$env:RDC_HARNESS_AUDIT_LOG = '$(Quote-Single (Join-Path $LogDir "browser-actions.ndjson"))'
"@
if ($BrowserExe) {
    $Config += "`r`n`$env:PW_EXECUTABLE = '$(Quote-Single $BrowserExe)'`r`n"
}
Write-PowerShellFile $ConfigPath $Config

$DesktopRunner = Join-Path $InstallRoot "run-desktop-agent.ps1"
$BrowserRunner = Join-Path $InstallRoot "run-browser-bridge.ps1"
Write-PowerShellFile $DesktopRunner @"
. "`$PSScriptRoot\config.ps1"
& "`$PSScriptRoot\.venv\Scripts\python.exe" "`$PSScriptRoot\desktop-agent\watchdog.py"
exit `$LASTEXITCODE
"@
Write-PowerShellFile $BrowserRunner @"
. "`$PSScriptRoot\config.ps1"
& "`$PSScriptRoot\.venv\Scripts\python.exe" "`$PSScriptRoot\browser-bridge\watchdog.py"
exit `$LASTEXITCODE
"@

if ($InstallRemoteCommander) {
    Write-Step "Installing Remote Desktop Commander $RemoteCommanderVersion"
    $RemoteDir = Join-Path $InstallRoot "remote-commander"
    New-Item -ItemType Directory -Path $RemoteDir -Force | Out-Null
    $RemotePackage = @{ private = $true; dependencies = @{ "@wonderwhy-er/desktop-commander" = $RemoteCommanderVersion } }
    Write-Utf8NoBom (Join-Path $RemoteDir "package.json") (($RemotePackage | ConvertTo-Json -Depth 4) + "`n")
    Push-Location $RemoteDir
    try {
        & $Npm install --omit=dev --ignore-scripts --no-audit --no-fund
        Assert-NativeExit "Installing Remote Desktop Commander"
    } finally { Pop-Location }
    $RemoteEntry = Join-Path $RemoteDir "node_modules\@wonderwhy-er\desktop-commander\dist\index.js"
    if (-not (Test-Path $RemoteEntry)) { throw "Remote Desktop Commander entry point was not installed." }
    $RemoteRunner = Join-Path $InstallRoot "run-remote-commander.ps1"
    # The share hook is loaded into the device agent with --import. It only acts
    # on remote read_file of images/documents and only when RDC_HARNESS_SHARE_CMD
    # is set; otherwise results pass through unchanged.
    Write-PowerShellFile $RemoteRunner @"
. "`$PSScriptRoot\config.ps1"
`$hook = ([uri](Join-Path `$PSScriptRoot "tools\rdc_share_hook.mjs")).AbsoluteUri
& "$(Quote-Single $Node)" --import `$hook "`$PSScriptRoot\remote-commander\node_modules\@wonderwhy-er\desktop-commander\dist\index.js" remote
exit `$LASTEXITCODE
"@
}

Write-Step "Adding pc-agent to the user PATH"
# Terminal clients (ChatGPT via Remote Desktop Commander) call `pc-agent ...`.
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathParts = @($UserPath -split ";" | Where-Object { $_ })
if ($PathParts -notcontains $ToolsDir) {
    [Environment]::SetEnvironmentVariable("Path", (($PathParts + $ToolsDir) -join ";"), "User")
    Write-Host "Added $ToolsDir to the user PATH. Restart Remote Commander to pick it up."
}

$SourceVersion = $env:RDC_HARNESS_SOURCE_VERSION
if (-not $SourceVersion) {
    $SourceVersion = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
}
$SourceRef = $env:RDC_HARNESS_SOURCE_REF
if (-not $SourceRef) {
    $Git = Get-Command "git.exe" -ErrorAction SilentlyContinue
    if ($Git) {
        Push-Location $RepoRoot
        try { $SourceRef = (& $Git.Source rev-parse HEAD 2>$null | Select-Object -First 1).Trim() }
        catch { $SourceRef = $null }
        finally { Pop-Location }
    }
}
$RemoteEntryCurrent = Join-Path $InstallRoot "remote-commander\node_modules\@wonderwhy-er\desktop-commander\dist\index.js"
$RemoteInstalled = Test-Path -LiteralPath $RemoteEntryCurrent
$InstallState = [ordered]@{
    schema_version = 1
    repository = "timelordy/rdc-workstation-harness"
    update_channel = $UpdateChannel
    update_check_enabled = (-not $NoUpdateCheck)
    installed_version = $SourceVersion
    installed_commit = $SourceRef
    installed_at = (Get-Date).ToUniversalTime().ToString("o")
    install_root = $InstallRoot
    browser = $Browser
    install_bundled_chromium = [bool]$InstallBundledChromium
    remote_commander_installed = [bool]$RemoteInstalled
    remote_commander_version = $RemoteCommanderVersion
    no_autostart = [bool]$NoAutostart
    desktop_agent_port = $DesktopAgentPort
    browser_bridge_port = $BrowserBridgePort
}
Write-Utf8NoBom (Join-Path $InstallRoot "install-state.json") `
    (($InstallState | ConvertTo-Json -Depth 6) + "`n")

if (-not $NoAutostart) {
    Write-Step "Registering one autostart task per component"
    Register-HarnessTask "$TaskPrefix - Desktop Agent" $DesktopRunner
    Register-HarnessTask "$TaskPrefix - Browser Bridge" $BrowserRunner
    if ($InstallRemoteCommander) {
        Register-HarnessTask "$TaskPrefix - Remote Commander" $RemoteRunner
    }
} else {
    foreach ($taskName in @("$TaskPrefix - Desktop Agent", "$TaskPrefix - Browser Bridge", "$TaskPrefix - Remote Commander")) {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
    }
}

$UpdateCheckScript = Join-Path $MaintenanceDir "check-update.ps1"
if (-not $NoUpdateCheck) {
    Register-UpdateTask "$TaskPrefix - Update Check" $UpdateCheckScript
} elseif (Get-ScheduledTask -TaskName "$TaskPrefix - Update Check" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "$TaskPrefix - Update Check" -Confirm:$false
}

if (-not $SkipStart) {
    Write-Step "Starting services"
    if ($NoAutostart) {
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $DesktopRunner)
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $BrowserRunner)
    } else {
        Start-ScheduledTask -TaskName "$TaskPrefix - Desktop Agent"
        Start-ScheduledTask -TaskName "$TaskPrefix - Browser Bridge"
    }
    if ($InstallRemoteCommander) {
        $RemoteIdentity = Join-Path $env:USERPROFILE ".desktop-commander-device\device.json"
        if (Test-Path $RemoteIdentity) {
            if ($NoAutostart) {
                Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
                    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $RemoteRunner)
            } else {
                Start-ScheduledTask -TaskName "$TaskPrefix - Remote Commander"
            }
        } else {
            Write-Warning "Remote Commander still needs one-time pairing. Run scripts\pair-remote-commander.ps1 in a visible terminal."
        }
    }
}
Write-Step "Installation complete"
Write-Host "Install root: $InstallRoot"
Write-Host "Token directory: $TokenDir"
Write-Host "Desktop agent: http://127.0.0.1:$DesktopAgentPort/health"
Write-Host "Browser bridge: http://127.0.0.1:$BrowserBridgePort/health"
Write-Host "Diagnostics: powershell -ExecutionPolicy Bypass -File `"$RepoRoot\scripts\diagnose.ps1`" -InstallRoot `"$InstallRoot`""
Write-Host "Update channel: $UpdateChannel"
Write-Host "Updater: $InstallRoot\maintenance\update.ps1"
if ($InstallRemoteCommander) {
    Write-Host "Remote Commander uses its own per-device registration under %USERPROFILE%\.desktop-commander-device."
    Write-Host "Do not copy that directory to another workstation."
}
