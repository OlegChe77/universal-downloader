@echo off
rem Checks and updates the components in a visible window, then starts the program as a tray icon.
rem All the logic is in start.ps1: cmd misreads batch files with non-ASCII text, so keep this file ASCII only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
