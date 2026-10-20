@echo off
setlocal
rem Installs Spatial Prediction Studio for the current Windows user. No administrator rights are needed.
rem Extra options are passed on, for example:  Install.cmd -WithMcp
echo Spatial Prediction Studio installer
echo Close ArcMap before you continue. The first installation downloads about 700 MB and takes 10-30 minutes.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\bootstrap.ps1" -Action Install %*
set "SPS_EXIT=%ERRORLEVEL%"
echo.
if "%SPS_EXIT%"=="0" (echo Installation finished. Start ArcMap to use Spatial Prediction Studio.) else (echo Installation failed. Read the message above. The full log is in the logs folder of the installation.)
pause
exit /b %SPS_EXIT%
