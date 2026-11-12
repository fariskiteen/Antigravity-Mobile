@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_bridge.ps1"
if errorlevel 1 pause
