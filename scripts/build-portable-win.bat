@echo off
REM Build KnowledgePod portable EXE (no installation required)
REM Run: scripts\build-portable-win.bat

powershell -ExecutionPolicy Bypass -File "%~dp0build-portable-win.ps1"
exit /b %ERRORLEVEL%
