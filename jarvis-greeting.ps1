# jarvis-greeting.ps1 — spoken Jarvis-style welcome at Windows logon.
# Installed to C:\ProgramData\jobcontext\ by windows-setup.ps1 alongside
# gpu-exporter.ps1, registered as the JarvisGreeting logon task.
#
# Voice: Microsoft neural TTS via edge-tts (pip install edge-tts) — the
# built-in SAPI voices are unlistenable. Renders fresh each logon (keeps
# the live GPU line), caches the mp3 for offline boots, SAPI only as the
# last resort.
#
# Whether the sim launches after the greeting is decided by the boot
# intent recorded on the Pi ("flight sim bootup" = sim, "boot into
# Windows" = plain); manual boots follow $SimOnManualBoot.

$ErrorActionPreference = 'SilentlyContinue'

$Voice = 'en-GB-RyanNeural'   # try en-GB-ThomasNeural / en-US-GuyNeural
$Rate  = '-5%'
$CacheDir = Join-Path $env:ProgramData 'jobcontext\jarvis'

# MSFS 2020 (Store):  explorer.exe shell:AppsFolder\Microsoft.FlightSimulator_8wekyb3d8bbwe!App
# MSFS 2024 (Store):  explorer.exe shell:AppsFolder\Microsoft.Limitless_8wekyb3d8bbwe!App
# MSFS (Steam):       Start-Process "steam://rungameid/1250410"
$SimCommand = { explorer.exe shell:AppsFolder\Microsoft.Limitless_8wekyb3d8bbwe!App }
$SimOnManualBoot = $false   # sim on power-button boots with no voice intent

# Which Alexa trigger caused this boot? flightsim-boot.sh on the Pi records
# "sim <epoch>" ("flight sim bootup") or "plain <epoch>" ("boot into
# Windows"). Stale/absent (manual boot, Pi down) falls back to
# $SimOnManualBoot.
$LaunchSim = $SimOnManualBoot
$intentRaw = & ssh -o BatchMode=yes -o ConnectTimeout=4 user@192.168.1.51 'cat /tmp/flightsim-intent 2>/dev/null'
if ($intentRaw -match '^(sim|plain) (\d+)$') {
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$Matches[2]
    if ($age -lt 1800) { $LaunchSim = ($Matches[1] -eq 'sim') }
}

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

$closing = if ($LaunchSim) { 'The flight deck is ready when you are.' }
           else { 'Ready when you are.' }
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

# Fresh neural render (needs network — usually up by logon). python -m
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
    # Never rendered anything yet and offline — better robotic than silent.
    Add-Type -AssemblyName System.Speech
    $v = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $v.Rate = -1
    $v.Speak($greeting)
}

if ($LaunchSim) { & $SimCommand }
