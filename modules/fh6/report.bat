@echo off
rem Analyse the most recent FH6 session and open the report.
rem Extra arguments are passed through, e.g.  report.bat --combo circuit-1 --lap 3
py -3.12 "%~dp0..\..\analysis\lap_analysis.py" --game fh6 report --open %*
pause
