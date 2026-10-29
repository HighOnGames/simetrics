@echo off
rem Simetrics: opens the app in your browser. Close this window to stop it.
title Simetrics
set SIMETRICS_SUPERVISED=1
set "ARGS=%*"
:start
py -3.12 "%~dp0app\server.py" %ARGS%
rem exit code 3 = a module was installed, updated, rolled back or removed: start again with it
if "%errorlevel%"=="3" goto restart
if errorlevel 1 pause
goto :eof
:restart
set "ARGS=%* --no-open"
goto start
