# windows-setup.ps1 — one-time prep on the workstation's WINDOWS boot.
# Run from an elevated PowerShell in the repo root:
#
#   powershell -ExecutionPolicy Bypass -File scripts\flightsim\windows-setup.ps1
#
# Does:
#   * arms Wake-on-Magic-Packet on the wired NIC (driver + OS wake grant)
#   * keeps Fast Startup off (hybrid shutdown breaks WOL from S5)
#   * installs jarvis-greeting.ps1 to C:\ProgramData\jobcontext\ and
#     registers the JarvisGreeting logon task (same home + pattern as
#     gpu-exporter.ps1)
# Then prints the manual checklist (BIOS, auto-logon).

$ErrorActionPreference = 'Stop'
$adapterName = 'Ethernet'   # Intel I226-V, MAC AA-BB-CC-DD-EE-FF, 192.168.1.50
$dest = 'C:\ProgramData\jobcontext'

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
# OS-level wake grant (Device Manager > Power Management checkbox).
$pnpId = (Get-NetAdapter -Name $adapterName).PnPDeviceID
powercfg /deviceenablewake ((Get-PnpDevice -InstanceId $pnpId).FriendlyName)

# --- Fast Startup stays off ------------------------------------------------
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
    -Name HiberbootEnabled -Value 0
Write-Host 'Fast Startup: off'

# --- Jarvis greeting logon task -------------------------------------------
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item "$PSScriptRoot\jarvis-greeting.ps1" $dest -Force

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dest\jarvis-greeting.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName 'JarvisGreeting' -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Write-Host 'JarvisGreeting logon task registered'

Write-Host @'

Manual checklist (cannot be scripted from here):
  1. BIOS: enable "Resume by PCI-E/PME" (a.k.a. Wake on LAN) and make sure
     ErP/EuP deep-off is DISABLED, or the NIC loses standby power at S5.
  2. Auto sign-in (optional but recommended - the greeting, gpu-exporter,
     and sim launch all fire at logon): netplwiz > uncheck "Users must
     enter a user name and password", or Sysinternals Autologon.
  3. Test the greeting now:  Start-ScheduledTask JarvisGreeting
'@
