param(
    [string]$Python = "python",
    [string]$Venv = "",
    [switch]$WithDocling
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Venv) { $Venv = Join-Path $RootDir ".venv" }
$ConstraintsFile = Join-Path $RootDir "constraints.txt"
if (-not $env:RESEARCH_CONNECT_DATA_DIR) {
    $env:RESEARCH_CONNECT_DATA_DIR = Join-Path $env:USERPROFILE ".research-connect\data"
}
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:RESEARCH_CONNECT_DATA_DIR "browsers"

& $Python -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else "Research Connect requires Python 3.11-3.13")'
if (-not (Test-Path $Venv)) { & $Python -m venv $Venv }

$VenvPython = Join-Path $Venv "Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip setuptools wheel
& $VenvPython -m pip install -c $ConstraintsFile -e (Join-Path $RootDir "packages\research-connect-core")
& $VenvPython -m pip install -c $ConstraintsFile -e "$RootDir[dev]"
& $VenvPython -m pip install --no-deps `
    -e (Join-Path $RootDir "apps\connect-hub") `
    -e (Join-Path $RootDir "apps\report-hub") `
    -e (Join-Path $RootDir "modules\citationclaw") `
    -e (Join-Path $RootDir "modules\xhs-agent")

if ($WithDocling) { & $VenvPython -m pip install -c $ConstraintsFile -e "$RootDir[docling]" }
& $VenvPython -m playwright install chromium
& $VenvPython -m pip check
Write-Host "Research Connect environment ready: $Venv"
Write-Host "Shared data root: $env:RESEARCH_CONNECT_DATA_DIR"
