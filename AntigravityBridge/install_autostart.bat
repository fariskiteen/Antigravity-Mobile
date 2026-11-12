@echo off
setlocal
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET_VBS=%~dp0start_bridge_hidden.vbs"
echo Set WshShell = CreateObject("WScript.Shell") > "%STARTUP_DIR%\AntigravityMobileBridge.vbs"
echo WshShell.Run """" ^& "%TARGET_VBS%" ^& """", 0, False >> "%STARTUP_DIR%\AntigravityMobileBridge.vbs"
echo Installed startup launcher to: %STARTUP_DIR%\AntigravityMobileBridge.vbs
pause
