@echo off
rem Log Forza Horizon 6 laps. FH6: Settings > HUD and Gameplay > Data Out On, 127.0.0.1, port 5300.
rem Extra arguments are passed through, e.g.  start_logger.bat --car "Adam Rocks"
py -3.12 "%~dp0fh6_laplogger.py" %*
pause
