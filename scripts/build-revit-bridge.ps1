#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet(2022, 2023, 2024)][int]$RevitYear = 2022,
    [string]$RevitInstallDir,
    [string]$OutputDir = (Join-Path $PSScriptRoot "..\build\revit")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $RevitInstallDir) {
    $RevitInstallDir = Join-Path $env:ProgramFiles "Autodesk\Revit $RevitYear"
}
$DbApi = Join-Path $RevitInstallDir "RevitAPI.dll"
$UiApi = Join-Path $RevitInstallDir "RevitAPIUI.dll"
$Source = Join-Path $RepoRoot "src\revit_bridge\RevitBridge.cs"
$Compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"

foreach ($path in @($DbApi, $UiApi, $Source, $Compiler)) {
    if (-not (Test-Path $path)) { throw "Required file not found: $path" }
}
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$Output = Join-Path $OutputDir "RdcHarness.RevitBridge.dll"

& $Compiler /nologo /target:library /platform:x64 /optimize+ `
    "/out:$Output" `
    "/reference:$DbApi" `
    "/reference:$UiApi" `
    /reference:System.dll `
    /reference:System.Core.dll `
    /reference:System.Web.Extensions.dll `
    /reference:Microsoft.CSharp.dll `
    $Source
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Output)) {
    throw "Revit bridge compilation failed."
}
Write-Host "Built: $Output"
