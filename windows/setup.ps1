# windows/setup.ps1 — one-time prep on the workstation's WINDOWS boot.
# Run from an elevated PowerShell in the repo root:
#
#   powershell -ExecutionPolicy Bypass -File windows\setup.ps1 `
#       -PiHost user@192.168.1.51 -AdapterName Ethernet
#
# Does:
#   * arms Wake-on-Magic-Packet on the wired NIC (driver + OS wake grant)
#   * keeps Fast Startup off (hybrid shutdown breaks WOL from S5)
#   * installs jarvis-greeting.ps1 + boot-agent.ps1 to $dest and registers
#     their scheduled tasks (logon / startup)
# Then prints the manual checklist (BIOS, auto-logon).
#
# -PiHost is baked into the installed greeting so it can read back which
# voice trigger caused this boot. Re-running with a different value
# repoints it; omitting it keeps whatever the installed copy already has.
param(
    [string]$PiHost = '',
    [string]$AdapterName = 'Ethernet'
)

$ErrorActionPreference = 'Stop'
$adapterName = $AdapterName
$dest = 'C:\ProgramData\dualboot'
# Pre-extraction home; the token is migrated from here so the Pi's copy
# stays valid across the move.
$legacyDest = 'C:\ProgramData\jobcontext'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error 'Run from an elevated PowerShell.'
}

# --- Wake-on-LAN -----------------------------------------------------------
foreach ($prop in 'Wake on Magic Packet', 'Wake from S0ix on Magic Packet') {
    try {
        Set-NetAdapterAdvancedProperty -Name $adapterName -DisplayName $prop `
            -DisplayValue 'Enabled' -NoRestart
        Write-Host "NIC: '$prop' enabled"
    } catch { Write-Host "NIC: '$prop' not offered by this driver - skipped" }
}
# OS-level wake grant (Device Manager > Power Management checkbox), but only
# when Windows actually lists this NIC as wake-programmable. On a box that
# only offers Modern Standby (S0 low power idle, no firmware S3) the wired
# NIC can be absent from that list entirely, and powercfg then answers
#   "You do not have permission to enable or disable device wake."
# even from an elevated shell. That message is a capability refusal wearing
# a privilege refusal's clothes: no amount of elevation, driver setting, or
# Device Manager checkbox will make the grant stick. Say so plainly here
# rather than let a re-run print an error that sends the next debugging
# session after the wrong problem.
$pnpId = (Get-NetAdapter -Name $adapterName).PnPDeviceID
$nicName = (Get-PnpDevice -InstanceId $pnpId).FriendlyName
if ((powercfg -devicequery wake_programmable) -contains $nicName) {
    powercfg /deviceenablewake $nicName
    Write-Host "NIC: OS wake grant enabled for '$nicName'"
} else {
    Write-Host "NIC: '$nicName' is NOT wake-programmable on this system -"
    Write-Host '     skipping the OS wake grant (powercfg would only fail).'
    Write-Host '     Wake-on-LAN from FULL POWER OFF (S5) is unaffected and'
    Write-Host '     still works; it is wake from SLEEP that cannot be armed.'
}

# Modern Standby is the usual reason for the above, and it also decides
# whether a scheduled WOL can ever land: under S0 low power idle the magic
# packet is ignored while the machine sleeps, so an idle timeout silently
# turns a working WOL setup into a dead one at 4am.
$sleepStates = (powercfg /a) -join "`n"
if ($sleepStates -match 'Standby \(S0 Low Power Idle\)') {
    Write-Host ''
    Write-Host 'POWER: this system reports Modern Standby (S0 Low Power Idle).'
    # Only look at the block BEFORE "not available" - S3 is named in both
    # halves of powercfg's output, so a whole-text match always "finds" it.
    $available = ($sleepStates -split 'The following sleep states are not available')[0]
    if ($available -match 'Standby \(S3\)') {
        Write-Host '       S3 is also available - preferring it in the BIOS makes'
        Write-Host '       wake-from-sleep reliable.'
    } else {
        Write-Host '       S3 is NOT available, so wake-from-sleep is unreliable by'
        Write-Host '       design. To keep a scheduled WOL working, stop the machine'
        Write-Host '       sleeping:  powercfg /change standby-timeout-ac 0'
    }
}

# --- Fast Startup stays off ------------------------------------------------
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
    -Name HiberbootEnabled -Value 0
Write-Host 'Fast Startup: off'

# --- Jarvis greeting logon task -------------------------------------------
New-Item -ItemType Directory -Force $dest | Out-Null
# Carry forward the installed PiHost when this run didn't supply one, so a
# plain re-run never silently reverts it to the repo's example address.
if (-not $PiHost -and (Test-Path "$dest\jarvis-greeting.ps1")) {
    $existing = Select-String -Path "$dest\jarvis-greeting.ps1" `
        -Pattern "^\`$PiHost\s*=\s*'([^']+)'" | Select-Object -First 1
    if ($existing) { $PiHost = $existing.Matches[0].Groups[1].Value }
}
Copy-Item "$PSScriptRoot\jarvis-greeting.ps1" $dest -Force
Copy-Item "$PSScriptRoot\boot-agent.ps1" $dest -Force
# Dot-sourced by the greeting from $PSScriptRoot, so it has to land beside it.
Copy-Item "$PSScriptRoot\set-primary-display.ps1" $dest -Force
if ($PiHost) {
    (Get-Content "$dest\jarvis-greeting.ps1") `
        -replace "^\`$PiHost\s*=\s*'[^']*'", "`$PiHost = '$PiHost'" |
        Set-Content "$dest\jarvis-greeting.ps1"
    Write-Host "Greeting reads boot intent from: $PiHost"
} else {
    Write-Host 'WARNING: no -PiHost given — the greeting cannot read the boot intent,'
    Write-Host '         so no game will launch. Re-run with -PiHost user@pi-address.'
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dest\jarvis-greeting.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName 'JarvisGreeting' -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Write-Host 'JarvisGreeting logon task registered'

# --- Boot agent (Pi-triggered reboot into Linux) --------------------------
# Runs as SYSTEM at startup so the Pi can reboot Windows into the GRUB
# default (Linux) even before anyone logs on. Token-guarded; the same
# token goes into /etc/flightsim/boot.env on the Pi.
$tokenFile = "$dest\boot-agent.token"
if (-not (Test-Path $tokenFile)) {
    $legacyToken = "$legacyDest\boot-agent.token"
    if (Test-Path $legacyToken) {
        Copy-Item $legacyToken $tokenFile
        Write-Host "Migrated the existing boot-agent token from $legacyDest"
    } else {
        [guid]::NewGuid().ToString('N') | Set-Content $tokenFile -Encoding ascii
    }
}
if (-not (Get-NetFirewallRule -DisplayName 'FlightSim BootAgent 9107' -ErrorAction SilentlyContinue)) {
    # Profile Any: this LAN shows as Public in Windows' eyes, and the
    # agent is token-guarded — a Private-only rule silently blocked the Pi.
    New-NetFirewallRule -DisplayName 'FlightSim BootAgent 9107' -Direction Inbound `
        -Protocol TCP -LocalPort 9107 -Action Allow -Profile Any | Out-Null
}
$agentAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dest\boot-agent.ps1`""
$agentTrigger = New-ScheduledTaskTrigger -AtStartup
$agentPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$agentSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'FlightSimBootAgent' -Action $agentAction -Trigger $agentTrigger `
    -Principal $agentPrincipal -Settings $agentSettings -Force | Out-Null
# Bounce so a rerun loads the freshly copied script, not the old instance.
Stop-ScheduledTask -TaskName 'FlightSimBootAgent' -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName 'FlightSimBootAgent'
Write-Host 'FlightSimBootAgent startup task registered and started'
Write-Host "Boot-agent token (put in /etc/flightsim/boot.env on the Pi as WIN_AGENT_TOKEN=): $(Get-Content $tokenFile -TotalCount 1)"

Write-Host @'

Manual checklist (cannot be scripted from here):
  1. BIOS: enable "Resume by PCI-E/PME" (a.k.a. Wake on LAN) and make sure
     ErP/EuP deep-off is DISABLED, or the NIC loses standby power at S5.
  2. Auto sign-in (optional but recommended - the greeting, gpu-exporter,
     and sim launch all fire at logon): netplwiz > uncheck "Users must
     enter a user name and password", or Sysinternals Autologon.
  3. Test the greeting now:  Start-ScheduledTask JarvisGreeting
'@
