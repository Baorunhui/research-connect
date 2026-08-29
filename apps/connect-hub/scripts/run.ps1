$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
& "$ProjectDir\.venv\Scripts\connect-hub.exe" feishu

