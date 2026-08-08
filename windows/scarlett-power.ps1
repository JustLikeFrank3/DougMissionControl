# windows/scarlett-power.ps1 - stop the Scarlett dropping off the bus.
# The Windows sibling of linux/scarlett-reset.py, aimed at a different failure:
# Linux needed a software replug after warm dual-boot reboots; Windows drops the
# device MID-SESSION, which is nearly always power management, not stale state.
#
# Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File windows\scarlett-power.ps1
#
# Does, for every Focusrite device (USB vendor 1235):
#   1. EnhancedPowerManagementEnabled=0 - Focusrite's own documented fix; the
#      usbaudio power logic suspends the interface and Scarletts don't wake.
#   2. "Allow the computer to turn off this device" OFF for the device and
#      every USB hub above it.
#   3. USB selective suspend OFF in the active power plan (AC and DC).
#   4. Removes ghost (not-present) Focusrite enumerations, so Windows stops
#      accumulating "2- Scarlett Solo USB" duplicate endpoints that strand
#      any app bound to the old name.
#
# All of it is reversible: 1-2 via Device Manager, 3 via powercfg, and ghosts
# rebuild harmlessly if the device re-enumerates again.

$ErrorActionPreference = 'Stop'
$VENDOR = 'VID_1235'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error 'Run from an elevated PowerShell.'
}

# --- 1. Enhanced Power Management off, per present Focusrite device ---------
$present = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match $VENDOR }
foreach ($dev in $present) {
    $key = "HKLM:\SYSTEM\CurrentControlSet\Enum\$($dev.InstanceId)\Device Parameters"
    if (Test-Path $key) {
        Set-ItemProperty -Path $key -Name 'EnhancedPowerManagementEnabled' -Value 0 -Type DWord
        Write-Host "EPM off : $($dev.InstanceId)"
    }
}

# --- 2. 'Allow the computer to turn off this device' off, device + hubs -----
# MSPower_DeviceEnable.Enable is exactly the Device Manager checkbox.
$targets = @()
foreach ($dev in $present) { $targets += $dev.InstanceId }
$targets += (Get-PnpDevice -PresentOnly -Class USB |
    Where-Object { $_.FriendlyName -match 'Hub' }).InstanceId

foreach ($id in $targets | Sort-Object -Unique) {
    $pm = Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceName -like "$id*" }
    foreach ($p in $pm) {
        if ($p.Enable) {
            $p | Set-CimInstance -Property @{ Enable = $false }
            Write-Host "PM off  : $id"
        }
    }
}

# --- 3. USB selective suspend off in the active plan ------------------------
$SUB_USB = '2a737441-1930-4402-8d77-b2bebba308a3'
$SUSPEND = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'
powercfg /SETACVALUEINDEX SCHEME_CURRENT $SUB_USB $SUSPEND 0
powercfg /SETDCVALUEINDEX SCHEME_CURRENT $SUB_USB $SUSPEND 0
powercfg /SETACTIVE SCHEME_CURRENT
Write-Host 'USB selective suspend: off (AC + DC)'

# --- 4. Sweep ghost enumerations -------------------------------------------
# Not-present Focusrite nodes are corpses of past drops. Removing them lets
# the next enumeration reuse the original endpoint names.
$ghosts = Get-PnpDevice | Where-Object {
    $_.InstanceId -match $VENDOR -and $_.Status -ne 'OK'
}
foreach ($g in $ghosts) {
    try {
        & pnputil /remove-device "$($g.InstanceId)" | Out-Null
        Write-Host "ghost   : removed $($g.InstanceId)"
    } catch { Write-Host "ghost   : could not remove $($g.InstanceId) - skipped" }
}

Write-Host ''
Write-Host 'Done. Unplug and replug the Scarlett (or reboot) so the device'
Write-Host 'tree rebuilds clean under the new power rules.'
