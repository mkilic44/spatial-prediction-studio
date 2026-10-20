@echo off
setlocal
rem Removes Spatial Prediction Studio for the current Windows user. Your projects and results are kept.
echo Spatial Prediction Studio uninstaller
echo Close ArcMap before you continue.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\bootstrap.ps1" -Action Uninstall %*
set "SPS_EXIT=%ERRORLEVEL%"
echo.
if "%SPS_EXIT%"=="0" (echo Uninstall finished.) else (echo Uninstall failed. Read the message above.)
pause
exit /b %SPS_EXIT%
