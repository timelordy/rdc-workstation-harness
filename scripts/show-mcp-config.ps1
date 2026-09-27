#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\RdcWorkstationHarness"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Node = (Get-Command node.exe -ErrorAction Stop).Source
$Server = Join-Path $InstallRoot "desktop-agent\mcp\server.mjs"
if (-not (Test-Path -LiteralPath $Server)) {
    throw "MCP server is not installed: $Server"
}

[ordered]@{
    name = "desktop_agent"
    transport = "stdio"
    command = $Node
    args = @($Server)
} | ConvertTo-Json -Depth 4
