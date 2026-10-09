@echo off
title VM Configurator — Lancement
echo.
echo  Demarrage de VM Configurator...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if %ERRORLEVEL% neq 0 pause
