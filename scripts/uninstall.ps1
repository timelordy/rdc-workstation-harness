#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness",
    [switch]$RemoveTokens,
    [switch]$RemoveBrowserProfile,
    [switch]$RemoveRemoteCommanderIdentity
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$TaskNames = @(
    "RDC Harness - Desktop Agent",
    "RDC Harness - Browser Bridge",
    "RDC Harness - Remote Commander",
    "RDC Harness - Update Check"
)

if (-not $PSCmdlet.ShouldProcess($InstallRoot, "Uninstall RDC Workstation Harness")) {
    return
}

foreach ($name in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallRoot*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500
if (Test-Path $InstallRoot) {
    if ($RemoveBrowserProfile) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    } else {
        $profile = Join-Path $InstallRoot "browser-profile"
        $saved = $null
        if (Test-Path $profile) {
            $saved = Join-Path $env:TEMP ("rdc-browser-profile-" + [guid]::NewGuid())
            Move-Item -LiteralPath $profile -Destination $saved
        }
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
        if ($saved) {
            New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
            Move-Item -LiteralPath $saved -Destination $profile
            Write-Host "Preserved browser profile: $profile"
        }
    }
}

if ($RemoveTokens) {
    Remove-Item (Join-Path $env:USERPROFILE ".chatgpt-desktop-agent") `
        -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "Preserved local tokens under %USERPROFILE%\.chatgpt-desktop-agent"
}

if ($RemoveRemoteCommanderIdentity) {
    Remove-Item (Join-Path $env:USERPROFILE ".desktop-commander-device") `
        -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "Preserved the workstation's Remote Commander identity."
}

Write-Host "RDC Workstation Harness was uninstalled."
