param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$pythonVersion = "3.14.2"
$runtime = Join-Path $PSScriptRoot "runtime"
$python = Join-Path $runtime "python.exe"
$sitePackages = Join-Path $runtime "Lib\site-packages"
$archive = Join-Path $env:TEMP "lancedb-mcp-python-$pythonVersion-embed-amd64.zip"
$getPip = Join-Path $env:TEMP "lancedb-mcp-get-pip.py"
$readyFile = Join-Path $PSScriptRoot ".runtime-ready"

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "This portable app currently requires 64-bit Windows."
}

if ((Test-Path $readyFile) -and -not $Force -and (Test-Path $python)) {
    & $python -c "import mcp, pymupdf, pyarrow, PySide6, openpyxl, docx"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Portable PySide6 and MCP application runtime is ready in this app folder."
        return
    }
}

if (-not (Test-Path $python)) {
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    $downloadUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
    Write-Host "Downloading portable Python $pythonVersion..."
    Invoke-WebRequest -Uri $downloadUrl -OutFile $archive
    Expand-Archive -Path $archive -DestinationPath $runtime -Force
    Remove-Item -LiteralPath $archive -Force

    $pythonPathFile = Join-Path $runtime "python314._pth"
    @(
        "python314.zip"
        "."
        "Lib\site-packages"
        "import site"
    ) | Set-Content -LiteralPath $pythonPathFile -Encoding ASCII
}

New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
Write-Host "Installing the GUI, MCP server, and app dependencies into this app folder..."

if (-not (Test-Path (Join-Path $sitePackages "pip"))) {
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
    & $python $getPip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install pip into the portable Python runtime."
    }
}

& $python -m pip install --disable-pip-version-check --no-warn-script-location --target $sitePackages -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed. Check your internet connection and rerun Run.bat."
}

& $python -c "import mcp, pymupdf, pyarrow, PySide6, openpyxl, docx; print('Portable GUI, PDF, Office, and MCP dependencies are ready.')"
if ($LASTEXITCODE -ne 0) {
    throw "The portable runtime validation failed."
}

Set-Content -LiteralPath $readyFile -Value $pythonVersion -Encoding ASCII
if (Test-Path $getPip) {
    Remove-Item -LiteralPath $getPip -Force
}
Write-Host "Setup complete. Start the app with Run.bat."
