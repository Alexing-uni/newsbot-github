@echo off
rem Doble clic: el bot arrancara solo al encender Windows aunque no inicies sesion (p. ej. tras un reinicio
rem de Windows Update de madrugada). Windows te pedira confirmacion de administrador: pulsa "Si".
cd /d "%~dp0"
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0deploy\watchdog_task_boot.ps1\"; Start-Sleep 4'"
echo.
echo Hecho. Comprueba en el Programador de tareas que "newsbot watchdog" tiene el desencadenador "Al iniciar el sistema".
pause
