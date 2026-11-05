param(
    [string]$OutputDirectory = "D:\Desktop"
)

$ErrorActionPreference = "Stop"

$appDir = $PSScriptRoot
$python = (Get-Command python.exe -ErrorAction Stop).Source
$sitePackages = Join-Path $appDir "runtime\Lib\site-packages"
$outputDir = [System.IO.Path]::GetFullPath($OutputDirectory)
$name = $env:LANCEDB_BUILD_NAME
if (-not $name) {
    $name = "LanceDB MCP App"
}
$stagingDir = Join-Path $outputDir ".lancedb-mcp-dist-$([guid]::NewGuid().ToString('N'))"
$workDir = Join-Path $stagingDir "pyinstaller-work"
$exePath = Join-Path $outputDir "$name.exe"

function Invoke-PackagedValidation {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExecutablePath,
        [Parameter(Mandatory = $true)]
        [string]$Argument
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $ExecutablePath
    $startInfo.Arguments = $Argument
    $startInfo.WorkingDirectory = Split-Path -Parent $ExecutablePath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $process.WaitForExit(120000)) {
        $processId = $process.Id
        Stop-Process -Id $processId -Force
        $workers = Get-CimInstance Win32_Process | Where-Object {
            $_.ExecutablePath -eq $ExecutablePath -and $_.ParentProcessId -eq $processId
        }
        foreach ($worker in $workers) {
            Stop-Process -Id $worker.ProcessId -Force -ErrorAction SilentlyContinue
        }
        throw "Packaged validation timed out: $Argument."
    }
    if ($process.ExitCode -ne 0) {
        throw "Packaged validation failed with exit code $($process.ExitCode): $Argument."
    }
    Write-Host "Packaged validation passed: $Argument."
}

if (-not (Test-Path -LiteralPath (Join-Path $sitePackages "PySide6"))) {
    throw "The app-local PySide6 runtime is missing. Run build.ps1 first."
}

$runningApps = Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -eq $exePath
}
if ($runningApps) {
    throw "Close '$exePath' before rebuilding it. No running app process was stopped."
}

$env:LANCEDB_BUILD_DIST = $stagingDir
$env:LANCEDB_BUILD_WORK = $workDir
$env:LANCEDB_BUILD_NAME = $name
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $buildOutput = & $python (Join-Path $appDir "build-exe.py") 2>&1
    $buildExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
$buildOutput | ForEach-Object { Write-Host $_ }
if ($buildExitCode -ne 0) {
    throw "PyInstaller failed to build the standalone app."
}

$stagedExe = Join-Path $stagingDir "$name.exe"
if (-not (Test-Path -LiteralPath $stagedExe)) {
    throw "PyInstaller completed without producing $stagedExe."
}

Invoke-PackagedValidation -ExecutablePath $stagedExe -Argument "--validate-pdf-runtime"
Invoke-PackagedValidation -ExecutablePath $stagedExe -Argument "--validate-office-runtime"

if (Test-Path -LiteralPath $exePath) {
    $backupPath = Join-Path $stagingDir "$name.previous.exe"
    [System.IO.File]::Move($exePath, $backupPath)
    try {
        [System.IO.File]::Move($stagedExe, $exePath)
    } catch {
        if (-not (Test-Path -LiteralPath $exePath)) {
            [System.IO.File]::Move($backupPath, $exePath)
        }
        throw
    }
    [System.IO.File]::Delete($backupPath)
} else {
    [System.IO.File]::Move($stagedExe, $exePath)
}

$length = (Get-Item -LiteralPath $exePath).Length
foreach ($directoryName in @("DATA", "RUNTIME")) {
    $directoryPath = Join-Path $outputDir $directoryName
    New-Item -ItemType Directory -Path $directoryPath -Force | Out-Null
}
Remove-Item -LiteralPath $stagingDir -Recurse -Force
Write-Host "Standalone executable ready: $exePath"
Write-Host "Executable size: $length bytes."
Write-Host "Keep adjacent 'DATA' and 'RUNTIME' folders with the executable."
