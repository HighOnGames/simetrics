@echo off
rem Check the FH6 telemetry decodes sensibly: live speed, rpm, gear, pedals, steering, position.
py -3.12 "%~dp0fh6_laplogger.py" probe %*
pause
