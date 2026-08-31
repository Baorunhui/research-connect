param(
    [string]$Python = "",
    [string]$Venv = "",
    [switch]$WithDocling,
    [switch]$Dev,
    [switch]$SkipBrowser
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Venv) { $Venv = Join-Path $RootDir ".venv" }
$ConstraintsFile = Join-Path $RootDir "constraints.txt"
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $Python = "py" }
    else { $Python = "python" }
}
$PythonPrefix = @()
if ($Python -eq "py") { $PythonPrefix = @("-3") }

function Invoke-BasePython {
    param([string[]]$Arguments)
    & $Python @PythonPrefix @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}
if (-not $env:RESEARCH_CONNECT_DATA_DIR) {
    $env:RESEARCH_CONNECT_DATA_DIR = Join-Path $env:USERPROFILE ".research-connect\data"
}
if (-not $env:PLAYWRIGHT_BROWSERS_PATH) {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:RESEARCH_CONNECT_DATA_DIR "browsers"
}

& $Python @PythonPrefix '-c' 'import sys; raise SystemExit(not ((3, 11) <= sys.version_info < (3, 14)))'
if ($LASTEXITCODE -ne 0) {
    throw "Research Connect requires Python 3.11-3.13. Run py -0p to list installed Python versions."
}
if (-not (Test-Path $Venv)) { Invoke-BasePython -Arguments @('-m', 'venv', $Venv) }

$VenvPython = Join-Path $Venv "Scripts\python.exe"
function Invoke-VenvPython {
    param([string[]]$Arguments)
    & $VenvPython @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Virtualenv Python command failed with exit code $LASTEXITCODE" }
}
Invoke-VenvPython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel')
Invoke-VenvPython -Arguments @('-m', 'pip', 'install', '-c', $ConstraintsFile, '-e', (Join-Path $RootDir "packages\research-connect-core"))
$RootSpec = $RootDir
if ($Dev) { $RootSpec = "${RootDir}[dev]" }
Invoke-VenvPython -Arguments @('-m', 'pip', 'install', '-c', $ConstraintsFile, '-e', $RootSpec)
Invoke-VenvPython -Arguments @(
    '-m', 'pip', 'install', '--no-deps',
    '-e', (Join-Path $RootDir "apps\connect-hub"),
    '-e', (Join-Path $RootDir "apps\report-hub"),
    '-e', (Join-Path $RootDir "modules\citationclaw"),
    '-e', (Join-Path $RootDir "modules\xhs-agent")
)

if ($WithDocling) { Invoke-VenvPython -Arguments @('-m', 'pip', 'install', '-c', $ConstraintsFile, '-e', "${RootDir}[docling]") }
if (-not $SkipBrowser) { Invoke-VenvPython -Arguments @('-m', 'playwright', 'install', 'chromium') }
Invoke-VenvPython -Arguments @('-m', 'pip', 'check')
$EnvFile = Join-Path $RootDir "apps\connect-hub\.env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $RootDir "apps\connect-hub\.env.example") $EnvFile
    Write-Host "Created local configuration: $EnvFile"
}
Write-Host "Research Connect environment ready: $Venv"
Write-Host "Shared data root: $env:RESEARCH_CONNECT_DATA_DIR"
Write-Host "Next: edit $EnvFile, then run .\scripts\doctor.ps1"
