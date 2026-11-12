# Registra la tarea "newsbot watchdog" para que funcione SIN iniciar sesion: al arrancar Windows (p. ej. tras
# un reinicio de Windows Update de madrugada) y cada 5 min. Usa "S4U": no guarda tu contrasena; el bot solo
# usa Internet y archivos locales, que es lo que permite ese modo. Necesita permisos de administrador:
# ejecutalo con arranque_sin_sesion.cmd (pide confirmacion de Windows).
# Volver al modo anterior (solo con sesion iniciada): powershell -ExecutionPolicy Bypass -File deploy\watchdog_task.ps1
$root = Split-Path -Parent $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" `
    -Argument "-m newsbot.watchdog" -WorkingDirectory $root
$boot = New-ScheduledTaskTrigger -AtStartup
$boot.Delay = "PT1M"   # 1 min tras arrancar: deja que la red este lista
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
Register-ScheduledTask -TaskName "newsbot watchdog" -Action $action -Trigger @($boot, $every) `
    -Principal $principal -Settings $settings -Force `
    -Description "Arranca o reinicia newsbot (--loop) al encender el PC y cada 5 min, sin necesidad de sesion" | Out-Null
$t = Get-ScheduledTask -TaskName "newsbot watchdog"
Write-Output ("Tarea 'newsbot watchdog' registrada: al arrancar Windows y cada 5 min, sin iniciar sesion (" +
              $t.Principal.LogonType + ").")
