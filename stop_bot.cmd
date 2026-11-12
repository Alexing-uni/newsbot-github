@echo off
rem Para el bot de verdad: deja data\stop para que el vigilante no lo vuelva a arrancar.
rem Para arrancarlo otra vez: run_loop.cmd (borra data\stop).
cd /d "%~dp0"
if not exist "data" mkdir "data"
type nul > "data\stop"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*newsbot.bot*--loop*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Bot parado. Para volver a arrancarlo: run_loop.cmd
