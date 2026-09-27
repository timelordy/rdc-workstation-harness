#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Entry = Join-Path $InstallRoot "remote-commander\node_modules\@wonderwhy-er\desktop-commander\dist\index.js"
if (-not (Test-Path $Entry)) {
    throw "Remote Commander is not installed. Re-run scripts\install.ps1 with -InstallRemoteCommander."
}

$Identity = Join-Path $env:USERPROFILE ".desktop-commander-device\device.json"
if (Test-Path $Identity) {
    Write-Host "This workstation already has a Remote Commander identity."
    Write-Host "Keep it local; never commit or copy .desktop-commander-device."
}

Write-Host "Starting Remote Commander in the foreground."
Write-Host "Complete any pairing/sign-in steps shown by the program, then press Ctrl+C when finished."
& node.exe $Entry remote
exit $LASTEXITCODE
