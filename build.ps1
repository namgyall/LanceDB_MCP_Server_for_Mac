param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$setupScript = Join-Path $PSScriptRoot "setup.ps1"

& $setupScript @PSBoundParameters
if ($LASTEXITCODE -ne 0) {
    throw "Could not prepare the portable Python, MCP, and PySide6 runtime."
}

$guiScript = Join-Path $PSScriptRoot "gui.py"
if (-not (Test-Path -LiteralPath $guiScript)) {
    throw "The PySide6 GUI entry point is missing."
}

Write-Host "Portable PySide6 app is ready. Launch it with Run.bat."
