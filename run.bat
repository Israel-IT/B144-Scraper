@echo off
rem B144 scraper for Windows. Double-click this file, or run: run.bat [start^|stop^|status]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
if errorlevel 1 (
  echo.
  echo Something went wrong - see the messages above.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo.
  echo The scraper keeps running in the background. You can close this window.
  echo To stop it later: run.bat stop
  timeout /t 10 >nul
)
