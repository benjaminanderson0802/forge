# Forge progress in the Windows taskbar's notification area (system tray).
# Icon shows the % to the next checkpoint; hover for both numbers; click opens the status page.
# Reads only Forge's files; never touches the conductor. Started at logon by the "Forge tray" task.
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$forge = Split-Path -Parent $PSScriptRoot
$mutex = New-Object System.Threading.Mutex($false, 'Global\ForgeTray')
if (-not $mutex.WaitOne(0)) { exit }   # one tray icon only

function Get-ForgeProgress {
    try { $q = Get-Content "$forge\state\bootstrap\queue.json" -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $q = $null }
    try { $p = Get-Content "$forge\docs\progress.json" -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $p = $null }
    $tasks = @(); if ($q) { $tasks = @($q.tasks | Where-Object { $_.status -ne 'superseded' }) }
    $done = @($tasks | Where-Object { $_.status -eq 'done' }).Count
    $ckpt = if ($tasks.Count) { $done / $tasks.Count } else { 0 }
    $phases = if ($p) { @($p.phases) } else { @() }
    $pdone = @($phases | Where-Object { $_.done }).Count
    $cur = ($phases | Where-Object { -not $_.done } | Select-Object -First 1).name
    $prod = if ($phases.Count) { ($pdone + $(if ($pdone -lt $phases.Count) { $ckpt } else { 0 })) / $phases.Count } else { 0 }
    $state = if (Test-Path "$forge\state\bootstrap\KILL") { 'stopped' } elseif (Test-Path "$forge\state\bootstrap\PAUSED") { 'paused' } else { 'running' }
    [pscustomobject]@{ Ckpt = [int][math]::Floor($ckpt * 100); Prod = [int][math]::Floor($prod * 100); Done = $done;
        Total = $tasks.Count; Phase = $cur; State = $state }
}

function New-NumberIcon([int]$n, [string]$state) {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $bg = switch ($state) { 'stopped' { [System.Drawing.Color]::FromArgb(179, 38, 30) } 'paused' { [System.Drawing.Color]::FromArgb(138, 90, 0) } default { [System.Drawing.Color]::FromArgb(30, 107, 52) } }
    $g.FillRectangle((New-Object System.Drawing.SolidBrush $bg), 0, 0, 32, 32)
    $txt = if ($n -ge 100) { [string][char]0x2713 } else { "$n" }
    $font = New-Object System.Drawing.Font 'Segoe UI', $(if ($txt.Length -ge 2) { 15 } else { 18 }), ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
    $fmt = New-Object System.Drawing.StringFormat; $fmt.Alignment = 'Center'; $fmt.LineAlignment = 'Center'
    $g.DrawString($txt, $font, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF 0, 0, 32, 32), $fmt)
    $g.Dispose()
    [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

$tray = New-Object System.Windows.Forms.NotifyIcon
$menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add('Open Forge status', $null, { Start-Process 'http://127.0.0.1:8765/' })
[void]$menu.Items.Add('Hide this icon', $null, { $tray.Visible = $false; [System.Windows.Forms.Application]::Exit() })
$tray.ContextMenuStrip = $menu
$tray.add_MouseClick({ if ($_.Button -eq 'Left') { Start-Process 'http://127.0.0.1:8765/' } })

function Update-Tray {
    $p = Get-ForgeProgress
    $old = $tray.Icon
    $tray.Icon = New-NumberIcon $p.Ckpt $p.State
    if ($old) { $old.Dispose() }
    $t = "Forge $($p.State): checkpoint $($p.Ckpt)% ($($p.Done)/$($p.Total)), build $($p.Prod)%"
    $tray.Text = $t.Substring(0, [math]::Min(63, $t.Length))   # Windows limits tray tooltips to 63 characters
    $tray.Visible = $true
}
Update-Tray
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 30000
$timer.add_Tick({ Update-Tray })
$timer.Start()
[System.Windows.Forms.Application]::Run()
