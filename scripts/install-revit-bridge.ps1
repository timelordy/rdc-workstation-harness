#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet(2022, 2023, 2024)][int]$RevitYear = 2022,
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness",
    [string]$RevitInstallDir,
    [switch]$SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir = Join-Path $RepoRoot "build\revit"
$BuiltDll = Join-Path $BuildDir "RdcHarness.RevitBridge.dll"

if (-not $SkipBuild) {
    $arguments = @{
        RevitYear = $RevitYear
        OutputDir = $BuildDir
    }
    if ($RevitInstallDir) { $arguments.RevitInstallDir = $RevitInstallDir }
    & (Join-Path $PSScriptRoot "build-revit-bridge.ps1") @arguments
}
if (-not (Test-Path $BuiltDll)) { throw "Bridge DLL not found: $BuiltDll" }

$BridgeDir = Join-Path $InstallRoot "revit-bridge"
New-Item -ItemType Directory -Path $BridgeDir -Force | Out-Null
$InstalledDll = Join-Path $BridgeDir "RdcHarness.RevitBridge.dll"
Copy-Item -LiteralPath $BuiltDll -Destination $InstalledDll -Force

$ManifestDir = Join-Path $env:APPDATA "Autodesk\Revit\Addins\$RevitYear"
$ManifestPath = Join-Path $ManifestDir "RdcHarness.RevitBridge.addin"
New-Item -ItemType Directory -Path $ManifestDir -Force | Out-Null
$EscapedDll = [Security.SecurityElement]::Escape($InstalledDll)
$Manifest = @"
<?xml version="1.0" encoding="utf-8" standalone="no"?>
<RevitAddIns>
  <AddIn Type="Application">
    <Name>RDC Harness Revit Bridge</Name>
    <Assembly>$EscapedDll</Assembly>
    <AddInId>B5E4CA9C-47D6-4F3B-8D04-CC5D6B2A9F20</AddInId>
    <FullClassName>ChatGPT.RevitBridge.App</FullClassName>
    <VendorId>RDCH</VendorId>
    <VendorDescription>Loopback-only bridge for local workstation automation</VendorDescription>
  </AddIn>
</RevitAddIns>
"@
$Encoding = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($ManifestPath, $Manifest, $Encoding)

Write-Host "Installed Revit bridge DLL: $InstalledDll"
Write-Host "Installed manifest: $ManifestPath"
Write-Host "Restart Revit, then run scripts\diagnose.ps1."
Write-Warning "The current bridge build supports Revit 2022-2024 (.NET Framework). Revit 2025+ requires a separate .NET 8 port."
