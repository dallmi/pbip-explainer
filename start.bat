@echo off
rem pbip-explainer - double-click to start the local web app (no admin rights needed)
cd /d "%~dp0"
title pbip-explainer
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.9 or newer was not found.
  echo Install it from the Software Center or python.org ^(user install, no admin needed^), then try again.
  pause
  exit /b 1
)
%PY% app\server.py %*
if errorlevel 1 pause
