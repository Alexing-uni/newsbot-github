@echo off
rem Arranca el bucle continuo (un tick cada general.tick_minutes) a traves del vigilante:
rem si ya esta en marcha no hace nada, asi que nunca hay dos. Todo queda en data\newsbot.log.
rem Para pararlo de verdad (y que el vigilante no lo vuelva a arrancar): stop_bot.cmd
cd /d "%~dp0"
if exist "data\stop" del "data\stop"
start "" ".venv\Scripts\pythonw.exe" -m newsbot.watchdog --quiet
