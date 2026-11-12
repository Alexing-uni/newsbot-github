@echo off
rem Instalacion en un Windows nuevo (el portatil). Doble clic dentro de la carpeta newsbot.
rem 1) crea el entorno de Python  2) prueba en seco (no opera)  3) arranque automatico al iniciar sesion
cd /d "%~dp0"
python --version >nul 2>nul || (echo Falta Python: instalalo desde python.org marcando "Add python.exe to PATH". & pause & exit /b 1)
if not exist ".env" (echo Falta el fichero .env con tus claves en esta carpeta. & pause & exit /b 1)

echo === 1/3 Instalando (unos minutos) ===
python -m venv .venv || (pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -e . || (pause & exit /b 1)

echo.
echo === 2/3 Prueba en seco: lee tu cuenta y decide, pero NO opera ===
".venv\Scripts\python.exe" -m newsbot.bot --dry-run || (echo La prueba ha fallado: revisa el mensaje. & pause & exit /b 1)

echo.
echo === 3/3 Arranque automatico ===
echo IMPORTANTE: antes de seguir, el bot del otro PC tiene que estar DESACTIVADO.
echo Si no lo esta, cierra esta ventana ahora. Si ya lo esta, pulsa una tecla.
pause
echo @call "%~dp0run_loop.cmd" > "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\newsbot.cmd"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\watchdog_task.ps1"
call run_loop.cmd
echo.
echo Bot en marcha: cada 5 minutos lee noticias y precios y decide. Arrancara solo al iniciar sesion.
echo Para ver lo que hace: notepad data\newsbot.log
pause
