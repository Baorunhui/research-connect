param(
    [string]$Venv = "",
    [string]$EnvFile = ""
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Venv) { $Venv = Join-Path $RootDir ".venv" }
if (-not $EnvFile) { $EnvFile = Join-Path $RootDir "apps\connect-hub\.env" }
$Command = Join-Path $Venv "Scripts\connect-hub.exe"

if (-not (Test-Path $Command)) { throw "Research Connect is not installed. Run .\scripts\setup.ps1 first." }
if (-not (Test-Path $EnvFile)) { throw "Missing configuration: $EnvFile" }

& $Command --env-file $EnvFile doctor
exit $LASTEXITCODE
