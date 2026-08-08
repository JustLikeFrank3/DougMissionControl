# jarvis-greeting.ps1 - spoken Jarvis-style welcome at Windows logon.
# Installed to C:\ProgramData\dualboot\ by windows/setup.ps1 and
# registered as the JarvisGreeting logon task.
#
# Voice: Microsoft neural TTS via edge-tts (pip install edge-tts) - the
# built-in SAPI voices are unlistenable. Renders fresh each logon (keeps
# the live GPU line), caches the mp3 for offline boots, SAPI only as the
# last resort.
#
# What launches after the greeting is decided by the boot intent recorded
# on the Pi by whichever voice trigger fired; manual boots follow
# $ManualBootProfile.

$ErrorActionPreference = 'SilentlyContinue'

$Voice = 'en-GB-RyanNeural'   # try en-GB-ThomasNeural / en-US-GuyNeural
$Rate  = '-5%'
$CacheDir = Join-Path $env:ProgramData 'dualboot\jarvis'
$PiHost = 'user@192.168.1.51'   # where the boot intent is recorded

# Launch profiles, keyed by the boot intent each fauxmo device records on
# the Pi. "plain" (or anything unknown) greets without launching.
# MSFS 2020 (Store): explorer.exe shell:AppsFolder\Microsoft.FlightSimulator_8wekyb3d8bbwe!App
#
# `display` names the monitor the profile wants primary, and is optional -
# a profile without one leaves the desk exactly as it found it. Both of these
# games take the primary display and nothing else (Squadrons is Frostbite and
# has no monitor picker at all), so "launch on the ultrawide" is really "make
# the ultrawide primary first". The string is matched by
# set-primary-display.ps1 against the device name, PnP id, description and
# resolution; run that script with -List to find one that identifies a panel.
#
# `input` is optional too: the DDC input to put every monitor on before
# launching, through the sim agent already running on this machine. It is the
# other half of the same thought - the monitor has to be showing this PC, not
# whatever else is wired into it.
$LaunchProfiles = @{
    sim = @{
        cmd     = { explorer.exe shell:AppsFolder\Microsoft.Limitless_8wekyb3d8bbwe!App }
        display = '3840x1080'
        closing = 'The flight deck is ready when you are.'
    }
    squadrons = @{
        cmd     = { Start-Process 'C:\Program Files\EA Games\STAR WARS Squadrons\starwarssquadrons_launcher.exe' }
        display = '3840x1080'
        closing = 'Flight controls booting. May the Force be with you.'
    }
}
$ManualBootProfile = ''   # profile for power-button boots with no voice intent ('' = none)

# Which Alexa trigger caused this boot? flightsim-boot.sh on the Pi
# records "<intent> <epoch>". Stale/absent (manual boot, Pi down) falls
# back to $ManualBootProfile.
$bootProfile = $ManualBootProfile
$intentRaw = & ssh -o BatchMode=yes -o ConnectTimeout=4 $PiHost 'cat /tmp/flightsim-intent 2>/dev/null'
if ($intentRaw -match '^(\w+) (\d+)$') {
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$Matches[2]
    if ($age -lt 1800) { $bootProfile = $Matches[1] }
}
$launch = if ($bootProfile) { $LaunchProfiles[$bootProfile] } else { $null }

# Phase callbacks to deck-api, so the panel's rail can show LOGON and
# LAUNCHED as observed fact instead of stopping at OS UP. Authenticated with
# the boot-agent token this machine already holds; failures are silent - the
# greeting must never break because the Pi is down.
function Send-Phase($phase) {
    try {
        $deckHost = ($PiHost -split '@')[-1]
        $tok = (Get-Content 'C:\ProgramData\dualboot\boot-agent.token' -TotalCount 1).Trim()
        if (-not $tok) { return }
        $body = (@{ token = $tok; phase = $phase } | ConvertTo-Json -Compress)
        Invoke-RestMethod -Uri "http://${deckHost}:8088/api/phase" -Method Post `
            -ContentType 'application/json' -Body $body -TimeoutSec 3 | Out-Null
    } catch { }
}
# NOT $input as the parameter name: that is a PowerShell automatic variable
# holding the pipeline enumerator, and shadowing it inside a function is a
# quiet way to get behaviour nobody can explain later.
function Set-MonitorInput($InputName) {
    <#  Put every monitor on one DDC input, through the sim agent already
        running on this machine - the same endpoint the SCREENS surface drives,
        so there is exactly one implementation of DDC on this box. index -1 is
        "all monitors". Slow by nature (dxva2 waits on the panel), hence the
        generous timeout; the agent answers with what it sent, and the
        read-back on the panel is the only proof anything moved. #>
    $tok = (Get-Content 'C:\ProgramData\dualboot\sim-agent.token' -TotalCount 1).Trim()
    if (-not $tok) { throw 'sim-agent.token is empty' }
    $body = (@{ input = $InputName; index = -1 } | ConvertTo-Json -Compress)
    Invoke-RestMethod -Uri "http://127.0.0.1:9109/monitor?token=$tok" -Method Post `
        -ContentType 'application/json' -Body $body -TimeoutSec 25 | Out-Null
}

Send-Phase 'logon'

# Let the audio stack finish coming up before speaking.
Start-Sleep -Seconds 8

$hour = (Get-Date).Hour
$timeOfDay = if ($hour -lt 12) { 'Good morning' }
             elseif ($hour -lt 18) { 'Good afternoon' }
             else { 'Good evening' }

# Live GPU readout makes it feel like a real systems check.
$gpuLine = ''
$temp = (& nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits | Select-Object -First 1)
if ($temp) { $gpuLine = " G P U thermals at $($temp.Trim()) degrees and nominal." }

$closing = if ($launch) { $launch.closing } else { 'Ready when you are.' }
$greeting = "$timeOfDay, sir. Boot sequence complete. All systems are online.$gpuLine $closing"

New-Item -ItemType Directory -Force $CacheDir | Out-Null
$cached = Join-Path $CacheDir 'greeting.mp3'
$fresh  = Join-Path $CacheDir 'greeting-new.mp3'

function Play-Mp3($path) {
    Add-Type -AssemblyName PresentationCore
    $p = New-Object System.Windows.Media.MediaPlayer
    $p.Open([uri]$path)
    for ($i = 0; $i -lt 20 -and -not $p.NaturalDuration.HasTimeSpan; $i++) {
        Start-Sleep -Milliseconds 250
    }
    $p.Play()
    $secs = if ($p.NaturalDuration.HasTimeSpan) {
        $p.NaturalDuration.TimeSpan.TotalSeconds } else { 15 }
    Start-Sleep -Seconds ([math]::Ceiling($secs) + 1)
    $p.Close()
}

# Fresh neural render (needs network - usually up by logon). python -m
# beats relying on the pip Scripts dir being on the task's PATH.
Remove-Item $fresh -Force
& python -m edge_tts --voice $Voice --rate=$Rate --text $greeting --write-media $fresh 2>$null
if ((Test-Path $fresh) -and (Get-Item $fresh).Length -gt 1kb) {
    Move-Item $fresh $cached -Force
    Play-Mp3 $cached
} elseif (Test-Path $cached) {
    # Offline: replay the last successful render (stale temp beats robot voice).
    Play-Mp3 $cached
} else {
    # Never rendered anything yet and offline - better robotic than silent.
    Add-Type -AssemblyName System.Speech
    $v = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $v.Rate = -1
    $v.Speak($greeting)
}

if ($launch) {
    # The desk, before the game. Both of these are best-effort and neither may
    # stop a launch: a monitor that will not move is a worse evening than a
    # game on the wrong screen, and a greeting that ends in a red error and no
    # simulator is the worst of the three.
    if ($launch.ContainsKey('display') -and $launch.display) {
        try {
            . "$PSScriptRoot\set-primary-display.ps1"
            Write-Host (Set-PrimaryDisplay -Match $launch.display)
        } catch {
            Write-Warning "primary display unchanged: $_"
        }
    }
    if ($launch.ContainsKey('input') -and $launch.input) {
        try { Set-MonitorInput $launch.input }
        catch { Write-Warning "monitor input unchanged: $_" }
    }

    & $launch.cmd
    # Reported AFTER Start-Process returns: "the launch command was issued",
    # which is all this script can honestly observe about the app.
    Send-Phase 'launched'
}
