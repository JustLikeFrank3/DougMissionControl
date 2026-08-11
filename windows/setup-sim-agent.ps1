# windows/setup-sim-agent.ps1 - one-time prep for flightdeck-sim-agent.
# Run from an elevated PowerShell in the repo root:
#
#   powershell -ExecutionPolicy Bypass -File windows\setup-sim-agent.ps1
#
# Does:
#   * builds windows\sim-agent into a versioned directory under
#     C:\ProgramData\dualboot\sim-agent\releases
#   * creates sim-agent.token if it does not exist and prints it for the Pi
#   * opens TCP 9109 inbound on Profile Any
#   * registers the FlightDeckSimAgent logon task and starts it
#
# Separate from windows\setup.ps1 on purpose: nothing here touches the boot
# agent, the greeting, or Wake-on-LAN. Requires the .NET 8 SDK and the MSFS
# SDK; pass -MsfsSdk if yours is not at C:\MSFS 2024 SDK.
#
# Unlike FlightSimBootAgent, this task runs AS THE LOGGED-ON USER at logon
# rather than as SYSTEM at startup. The boot agent must answer before anyone
# signs in; this one only has anything to talk to once MSFS is running, and
# MSFS runs in the user's session.
param(
    [string]$MsfsSdk = 'C:\MSFS 2024 SDK'
)

$ErrorActionPreference = 'Stop'
$dest = 'C:\ProgramData\dualboot'
$appDir = "$dest\sim-agent"
$releaseRoot = "$appDir\releases"
$releaseId = '{0}-{1}' -f (Get-Date -Format 'yyyyMMddHHmmss'), $PID
$releaseDir = "$releaseRoot\$releaseId"
$port = 9109

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error 'Run from an elevated PowerShell.'
}

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    Write-Error 'The .NET 8 SDK is not installed (no dotnet on PATH). winget install Microsoft.DotNet.SDK.8'
}
if (-not (Test-Path "$MsfsSdk\SimConnect SDK\lib\managed\Microsoft.FlightSimulator.SimConnect.dll")) {
    Write-Error "MSFS SDK not found at '$MsfsSdk'. In MSFS 2024: Options > General > Developers > Developer Mode, then the Dev Mode menu > Help > SDK Installer."
}

# --- Build -----------------------------------------------------------------
# Stop the previous instance before publishing. Each build goes into a fresh
# release directory so an unrelated process retaining an old DLL handle cannot
# block upgrades. The task is repointed to the new release below.
if (Get-ScheduledTask -TaskName 'FlightDeckSimAgent' -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName 'FlightDeckSimAgent' -ErrorAction SilentlyContinue
    # The task reports stopped before the process has actually exited.
    for ($i = 0; $i -lt 20 -and (Get-Process flightdeck-sim-agent -ErrorAction SilentlyContinue); $i++) {
        Start-Sleep -Milliseconds 250
    }
    Get-Process flightdeck-sim-agent -ErrorAction SilentlyContinue |
        Where-Object Path -Like "$appDir\*" |
        Stop-Process -Force -ErrorAction Stop
    Write-Host 'Stopped the running FlightDeckSimAgent task'
}

New-Item -ItemType Directory -Force $releaseDir | Out-Null
& dotnet publish "$PSScriptRoot\sim-agent\FlightDeckSimAgent.csproj" `
    -c Release -o $releaseDir -p:MsfsSdk="$MsfsSdk"
if ($LASTEXITCODE -ne 0) { Write-Error 'dotnet publish failed.' }
Write-Host "Published to $releaseDir"

# --- Token -----------------------------------------------------------------
# Its own token, not the boot agent's: the two endpoints grant different
# things, and one leaking should not hand over the other.
$tokenFile = "$dest\sim-agent.token"
if (-not (Test-Path $tokenFile)) {
    [guid]::NewGuid().ToString('N') | Set-Content $tokenFile -Encoding ascii
}

# --- Firewall --------------------------------------------------------------
# Profile Any: Windows classifies this LAN as Public, and a Private-only rule
# fails silently - the same trap the boot agent's 9107 rule already documents.
if (-not (Get-NetFirewallRule -DisplayName "FlightDeck SimAgent $port" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "FlightDeck SimAgent $port" -Direction Inbound `
        -Protocol TCP -LocalPort $port -Action Allow -Profile Any | Out-Null
}
Write-Host "Firewall: TCP $port inbound allowed on all profiles"

# --- Logon task ------------------------------------------------------------
$action = New-ScheduledTaskAction -Execute "$releaseDir\flightdeck-sim-agent.exe" -WorkingDirectory $releaseDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'FlightDeckSimAgent' -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
# Bounce so a rerun runs the freshly published exe, not the old instance.
Stop-ScheduledTask -TaskName 'FlightDeckSimAgent' -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName 'FlightDeckSimAgent'
Write-Host 'FlightDeckSimAgent logon task registered and started'

# Old releases are no longer task targets. A process may still retain a handle
# briefly; leave that release for the next installer run instead of failing an
# otherwise successful deployment.
Get-ChildItem $releaseRoot -Directory | Where-Object FullName -ne $releaseDir | ForEach-Object {
    try { Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop }
    catch { Write-Warning "Could not remove old release '$($_.FullName)': $($_.Exception.Message)" }
}

Write-Host "Sim-agent token (put on the Pi as SIM_AGENT_TOKEN=): $(Get-Content $tokenFile -TotalCount 1)"
Write-Host @"

Check it from the Pi:
  curl -s http://192.168.68.50:$port/                       # -> sim-agent
  curl -s "http://192.168.68.50:$port/health?token=<token>"

The agent answering with sim.connected=false is normal - it means MSFS is not
running. It disappearing entirely is also normal: the workstation rebooted
into Linux.
"@
