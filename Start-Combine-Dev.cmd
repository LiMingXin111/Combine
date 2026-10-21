@echo off
title Combine Development
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0desktop\scripts\dev.ps1" %*
if errorlevel 1 pause
