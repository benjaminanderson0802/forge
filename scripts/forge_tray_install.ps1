$forge = 'C:\Users\benja\Forge'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -STA -File `"$forge\scripts\forge_tray.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Days 3650) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'Forge tray' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Forge: progress % in the taskbar notification area. Click opens the status page.' -Force | Out-Null
Start-ScheduledTask -TaskName 'Forge tray'
Start-Sleep 4
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'forge_tray' } | Select-Object ProcessId | Format-Table -HideTableHeaders
