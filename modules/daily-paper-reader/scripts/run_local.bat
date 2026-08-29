@echo off
rem 本地化部署启动器（Windows 原生，无需 WSL/bash）
rem 用法：双击本文件，或在 PowerShell 中运行  scripts\run_local.bat
cd /d "%~dp0.."
python src\local_server.py --serve
if errorlevel 1 pause