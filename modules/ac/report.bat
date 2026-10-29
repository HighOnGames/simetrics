@echo off
rem Analyse the most recent Assetto Corsa session and open the report.
rem Extra arguments are passed through, e.g.  report.bat --combo silverstone --lap 7
py -3.12 "%~dp0..\..\analysis\lap_analysis.py" --game ac report --open %*
pause
