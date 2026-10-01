# Starts Forge's conductor as an invisible background task (D-007, D-018, D-019, D-024).
#   - runs at logon and every 5 minutes (a watchdog: if it's already running, the new copy exits at once)
#   - a second task, "Forge watchdog", restarts it if it hangs (heartbeat stops while the task still runs)
#   - no window, below-normal priority, no time limit
#   - PC never sleeps while plugged in (screen can still turn off)
#   - "Stop Forge" and "Start Forge" shortcuts on the desktop
#   - R58 lanes: one more task, "Forge conductor <lane>", per lane listed in state\lanes.json (re-run after
#     `python -m core.bootstrap init --lane <lane> ...` to add it). Stop Forge stops every lane.
# Safe to re-run.
$ErrorActionPreference = 'Stop'
$forge = Split-Path -Parent $PSScriptRoot
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = Join-Path (Split-Path (Get-Command python.exe).Source) 'pythonw.exe' }
$state = Join-Path $forge 'state\bootstrap'
New-Item -ItemType Directory -Force $state | Out-Null

# 1. Background tasks: "Forge conductor" (the main lane) and "Forge conductor <lane>" for each lane in state\lanes.json
$shared = Join-Path $forge 'state\shared'
New-Item -ItemType Directory -Force $shared | Out-Null
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
function Register-Conductor([string]$name, [string]$runArgs) {
    $action = New-ScheduledTaskAction -Execute $pyw -Argument $runArgs -WorkingDirectory $forge
    Register-ScheduledTask -TaskName $name -Action $action -Trigger @($logon, $every5) -Settings $settings `
        -Principal $principal -Description 'Forge: runs the build team in the background. Stop: Stop Forge on the desktop, or reply STOP to any Forge email.' -Force | Out-Null
    Write-Host "[OK] Background task `"$name`" registered (hidden, low priority, restarts itself)." -ForegroundColor Green
}
Register-Conductor 'Forge conductor' '-m core.bootstrap run'
$lanesFile = Join-Path $forge 'state\lanes.json'
$laneNames = @()
if (Test-Path $lanesFile) { $laneNames = Get-Content $lanesFile -Raw | ConvertFrom-Json }
foreach ($lane in $laneNames) {
    # same rule as core.lanes.name_problem (reserved names are refused by init, so they never reach lanes.json)
    if ($lane -is [string] -and $lane -cmatch '^[a-z][a-z0-9_]{0,23}$' -and $lane -ne 'main') {
        Register-Conductor "Forge conductor $lane" "-m core.bootstrap run --lane $lane"
    } else { Write-Host "[!!] Skipped a bad lane name in state\lanes.json: $lane" -ForegroundColor Yellow }
}

# 1b. Watchdog: every 5 minutes, python -m core.service watchdog (starts a dead conductor, restarts a hung one; every lane)
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
# Stop writes the global KILL (every lane) and main's KILL; Start clears every KILL, starts main, and runs the watchdog,
# which starts every other lane.
$stop = "@echo off`r`necho stopped by Stop Forge shortcut> `"$shared\KILL`"`r`necho stopped by Stop Forge shortcut> `"$state\KILL`"`r`necho Forge is stopping: a running agent is ended within seconds and nothing new starts. Run Start Forge to resume.`r`ntimeout /t 5`r`n"
$start = "@echo off`r`ndel /q `"$shared\KILL`" 2>nul`r`ndel /q `"$state\KILL`" 2>nul`r`nfor /d %%d in (`"$forge\state\lanes\*`") do del /q `"%%d\KILL`" 2>nul`r`nschtasks /Run /TN `"Forge conductor`" >nul`r`nschtasks /Run /TN `"Forge watchdog`" >nul`r`necho Forge is running again in the background.`r`ntimeout /t 5`r`n"
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
