# Starts Forge's conductor as an invisible background task (D-007, D-018, D-019, D-024).
#   - runs at logon and every 5 minutes (a watchdog: if it's already running, the new copy exits at once)
#   - a second task, "Forge watchdog", restarts it if it hangs (heartbeat stops while the task still runs)
#   - no window, below-normal priority, no time limit
#   - PC never sleeps while plugged in (screen can still turn off)
#   - "Stop Forge" and "Start Forge" shortcuts on the desktop
# Safe to re-run.
$ErrorActionPreference = 'Stop'
$forge = Split-Path -Parent $PSScriptRoot
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = Join-Path (Split-Path (Get-Command python.exe).Source) 'pythonw.exe' }
$state = Join-Path $forge 'state\bootstrap'
New-Item -ItemType Directory -Force $state | Out-Null

# 1. Background task
$action = New-ScheduledTaskAction -Execute $pyw -Argument '-m core.bootstrap run' -WorkingDirectory $forge
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'Forge conductor' -Action $action -Trigger @($logon, $every5) -Settings $settings `
    -Principal $principal -Description 'Forge: runs the build team in the background. Stop: Stop Forge on the desktop, or reply STOP to any Forge email.' -Force | Out-Null
Write-Host '[OK] Background task "Forge conductor" registered (hidden, low priority, restarts itself).' -ForegroundColor Green

# 1b. Watchdog: every 5 minutes, python -m core.service watchdog (starts a dead conductor, restarts a hung one)
$wdAction = New-ScheduledTaskAction -Execute $pyw -Argument '-m core.service watchdog' -WorkingDirectory $forge
$wdEvery5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(3) -RepetitionInterval (New-TimeSpan -Minutes 5)
$wdSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 -Hidden
Register-ScheduledTask -TaskName 'Forge watchdog' -Action $wdAction -Trigger $wdEvery5 -Settings $wdSettings `
    -Principal $principal -Description 'Forge: restarts the conductor if it hangs. Does nothing while Forge is stopped.' -Force | Out-Null
Write-Host '[OK] Background task "Forge watchdog" registered.' -ForegroundColor Green

# 1c. Status page: http://127.0.0.1:8765 (loopback only), started at logon, restarted every 5 minutes if it died
$spAction = New-ScheduledTaskAction -Execute $pyw -Argument '-m core.status_page' -WorkingDirectory $forge
$spLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$spEvery5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$spSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 -Hidden
Register-ScheduledTask -TaskName 'Forge status page' -Action $spAction -Trigger @($spLogon, $spEvery5) -Settings $spSettings `
    -Principal $principal -Description 'Forge: the local status page at http://127.0.0.1:8765 (progress, usage, questions, Stop).' -Force | Out-Null
Start-ScheduledTask -TaskName 'Forge status page'
Write-Host '[OK] Status page registered: http://127.0.0.1:8765' -ForegroundColor Green

# 2. Never sleep while plugged in (screen may still turn off)
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null
Write-Host '[OK] PC will not sleep while plugged in.' -ForegroundColor Green

# 3. Desktop shortcuts
$desk = [Environment]::GetFolderPath('Desktop')
$stop = "@echo off`r`necho stopped by Stop Forge shortcut> `"$state\KILL`"`r`necho Forge is stopping: a running agent is ended within seconds and nothing new starts. Run Start Forge to resume.`r`ntimeout /t 5`r`n"
$start = "@echo off`r`ndel /q `"$state\KILL`" 2>nul`r`nschtasks /Run /TN `"Forge conductor`" >nul`r`necho Forge is running again in the background.`r`ntimeout /t 5`r`n"
[IO.File]::WriteAllText((Join-Path $desk 'Stop Forge.cmd'), $stop)
[IO.File]::WriteAllText((Join-Path $desk 'Start Forge.cmd'), $start)
[IO.File]::WriteAllText((Join-Path $desk 'Forge status.url'), "[InternetShortcut]`r`nURL=http://127.0.0.1:8765/`r`n")
Write-Host '[OK] "Stop Forge" and "Start Forge" are on your desktop.' -ForegroundColor Green

# 4. Start now
Start-ScheduledTask -TaskName 'Forge conductor'
Start-Sleep 5
$hb = Join-Path $state 'conductor.heartbeat'
if (Test-Path $hb) { Write-Host '[OK] Conductor is running. You will get a "conductor started" email.' -ForegroundColor Green }
else { Write-Host '[..] Conductor is starting; check your email in a minute.' -ForegroundColor Yellow }
