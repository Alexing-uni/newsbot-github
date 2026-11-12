# Registra (o actualiza) la tarea "newsbot watchdog": cada 5 min comprueba que el bucle sigue vivo
# y lo arranca o reinicia si no (newsbot\watchdog.py). Corre con tu usuario y solo con la sesion
# iniciada (sin guardar contrasena). Ejecutar: powershell -ExecutionPolicy Bypass -File deploy\watchdog_task.ps1
# Quitarla: Unregister-ScheduledTask -TaskName "newsbot watchdog" -Confirm:$false
$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" `
    -Argument "-m newsbot.watchdog" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
Register-ScheduledTask -TaskName "newsbot watchdog" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "Arranca o reinicia newsbot (--loop) si no esta en marcha o no da latido" | Out-Null
Write-Output "Tarea 'newsbot watchdog' registrada: cada 5 min."
