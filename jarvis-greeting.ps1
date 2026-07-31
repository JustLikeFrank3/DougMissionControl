# jarvis-greeting.ps1 — spoken Jarvis-style welcome at Windows logon.
# Installed to C:\ProgramData\jobcontext\ by windows-setup.ps1 alongside
# gpu-exporter.ps1, registered as the JarvisGreeting logon task.
#
# Set $LaunchSim = $true (and pick the right AppId/URI below) to also start
# the simulator after the greeting.

$ErrorActionPreference = 'SilentlyContinue'

$LaunchSim = $false
# MSFS 2020 (Store):  explorer.exe shell:AppsFolder\Microsoft.FlightSimulator_8wekyb3d8bbwe!App
# MSFS 2024 (Store):  explorer.exe shell:AppsFolder\Microsoft.Limitless_8wekyb3d8bbwe!App
# MSFS (Steam):       Start-Process "steam://rungameid/1250410"
$SimCommand = { explorer.exe shell:AppsFolder\Microsoft.FlightSimulator_8wekyb3d8bbwe!App }

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

$greeting = "$timeOfDay, sir. Boot sequence complete. All systems are online.$gpuLine The flight deck is ready when you are."

Add-Type -AssemblyName System.Speech
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
# A British voice sells the Jarvis bit if one is installed (Settings >
# Time & Language > Speech > Add voices > English (United Kingdom)).
$george = $voice.GetInstalledVoices() |
    Where-Object { $_.VoiceInfo.Culture.Name -eq 'en-GB' -and $_.VoiceInfo.Gender -eq 'Male' } |
    Select-Object -First 1
if ($george) { $voice.SelectVoice($george.VoiceInfo.Name) }
$voice.Rate = -1
$voice.Speak($greeting)

if ($LaunchSim) { & $SimCommand }
