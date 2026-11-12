@echo off
rem Lanzador para el Programador de tareas de Windows (tarea "newsbot diario", 09:05).
rem Una decisión al día; el guard diario evita duplicados si la tarea se repite.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m newsbot.bot >> "data\scheduler.log" 2>&1
