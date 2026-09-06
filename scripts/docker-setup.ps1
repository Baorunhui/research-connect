$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConfigDir = Join-Path $RootDir "docker\config"
$ConfigFile = Join-Path $ConfigDir "connect-hub.env"
$DailyConfigFile = Join-Path $ConfigDir "daily-paper.yaml"
$ComposeEnv = Join-Path $RootDir ".env"

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
if (-not (Test-Path $ConfigFile)) {
    Copy-Item (Join-Path $RootDir "apps\connect-hub\.env.example") $ConfigFile
    Write-Host "Created Docker configuration: $ConfigFile"
} else {
    Write-Host "Keeping existing Docker configuration: $ConfigFile"
}
if (-not (Test-Path $DailyConfigFile)) {
    Copy-Item (Join-Path $RootDir "modules\daily-paper-reader\docs_init\config.yaml") $DailyConfigFile
    Write-Host "Created Daily Paper configuration: $DailyConfigFile"
}

$Lines = @()
if (Test-Path $ComposeEnv) {
    $Lines = @(Get-Content $ComposeEnv | Where-Object {
        $_ -notmatch '^(RESEARCH_CONNECT_UID|RESEARCH_CONNECT_GID)='
    })
}
$Lines += "RESEARCH_CONNECT_UID=1000"
$Lines += "RESEARCH_CONNECT_GID=1000"
Set-Content -Path $ComposeEnv -Value $Lines -Encoding utf8

Write-Host "Docker Compose identity saved in $ComposeEnv"
Write-Host "Next: edit $ConfigFile, then run docker compose build"
