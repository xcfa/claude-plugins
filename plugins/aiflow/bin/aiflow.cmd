@echo off
setlocal
set "AIFLOW=%~dp0..\scripts\aiflow.py"
where py >nul 2>nul && (py -3 "%AIFLOW%" %* & exit /b %ERRORLEVEL%)
where python >nul 2>nul && (python "%AIFLOW%" %* & exit /b %ERRORLEVEL%)
echo aiflow: Python 3.9+ not found in PATH 1>&2
exit /b 127
