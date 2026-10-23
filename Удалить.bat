@echo off
rem Removes the program: closes it, deletes the desktop shortcut, the udl-downloader: link and this folder.
rem All the logic is in uninstall.ps1: cmd misreads batch files with non-ASCII text, so keep this file ASCII only.
rem Runs from %TEMP% (this folder must not be busy); "(goto)" ends the batch first, so cmd never looks for this file, deleted by then.
cd /d "%TEMP%"
(goto) 2>nul & powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
