@echo off
rem Link the AC in-game LapLogger app to this folder and save its laps to data\ac.
py -3.12 "%~dp0install_ac_app.py" %*
pause
