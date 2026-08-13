# efi-entry.ps1 - find the firmware boot entry that lands in GRUB, so this
# machine can hand back to Linux. Dot-sourced by setup.ps1 (to report and
# cache it) and by boot-agent.ps1 (to arm it on a /reboot request).
#
# Why this exists: Windows cannot write ext4, so it can neither run
# grub-reboot nor edit grubenv - it has no way to choose a GRUB menu entry.
# It used to get away with a plain reboot because GRUB's saved default WAS
# Linux. That stopped being enough once the firmware boot order was pointed
# at Windows (linux/set-boot-order.sh) to make a cold "boot Windows"
# request land in one boot instead of two: a plain reboot now comes back to
# Windows. UEFI variables are the switch both sides can throw, so this asks
# the firmware for one boot of the GRUB entry, and GRUB's saved default
# (still Linux) takes it from there.
#
# `bcdedit /set {fwbootmgr} bootsequence {guid}` is the one-shot form - the
# firmware clears it after that boot, so nothing has to be undone later.

$script:FlightSimEntryCache = "$env:ProgramData\dualboot\linux-firmware-entry.txt"

function Get-LinuxFirmwareEntryFromText {
    # Split out for testability: takes `bcdedit /enum firmware` text, returns
    # the identifier of the GRUB entry, or '' when there is not one.
    #
    # Blocks look like:
    #   Firmware Application (101fffff)
    #   -------------------------------
    #   identifier              {7619dcc8-fafe-11d9-b411-000476eba25f}
    #   device                  partition=\Device\HarddiskVolume1
    #   path                    \EFI\ubuntu\shimx64.efi
    #   description             ubuntu
    param([string]$Text)

    $fallback = ''
    foreach ($block in ($Text -split '(?:\r?\n){2,}')) {
        if ($block -notmatch '(?im)^identifier\s+(\{[^}]+\})') { continue }
        $id = $Matches[1]
        # {bootmgr} is Windows itself and {fwbootmgr} is the list, not a
        # target; neither is ever the answer.
        if ($id -match '(?i)^\{(fwbootmgr|bootmgr|current|default)\}$') { continue }

        $desc = ''
        if ($block -match '(?im)^description\s+(.+?)\s*$') { $desc = $Matches[1] }
        $path = ''
        if ($block -match '(?im)^path\s+(.+?)\s*$') { $path = $Matches[1] }

        # Booting Windows when asked for Linux is the exact failure this
        # file exists to remove, so exclude it before matching anything.
        if ($desc -match '(?i)windows' -or $path -match '(?i)bootmgfw\.efi') { continue }

        if ($desc -match '(?i)ubuntu|debian|fedora|grub|linux' -or
            $path -match '(?i)\\EFI\\(ubuntu|debian|fedora)\\' -or
            $path -match '(?i)(shimx64|grubx64)\.efi') {
            return $id
        }
        # A fallback-path GRUB install shows up under the firmware's own
        # generic label. Second choice, never first.
        if ($desc -match '(?i)^UEFI OS$' -and -not $fallback) { $fallback = $id }
    }
    return $fallback
}

function Get-LinuxFirmwareEntryId {
    # Cached value if setup.ps1 already worked it out, otherwise derive it
    # now and cache. Deriving on demand means a boot agent that started
    # before setup wrote the file still works, and a bootloader reinstall
    # that changes the GUID only needs the cache file deleted.
    param([switch]$Refresh)

    if (-not $Refresh -and (Test-Path $script:FlightSimEntryCache)) {
        $cached = (Get-Content $script:FlightSimEntryCache -TotalCount 1)
        if ($cached) { $cached = $cached.Trim() }
        if ($cached -match '^\{[^}]+\}$') { return $cached }
    }

    # try/catch as well as 2>$null: a native command writing to stderr is a
    # terminating error under setup.ps1's $ErrorActionPreference = 'Stop',
    # and "the firmware entry is unknown" must never fail an install.
    $text = ''
    try { $text = (& bcdedit /enum firmware 2>$null) -join "`n" } catch { }
    if (-not $text) { return '' }
    $id = Get-LinuxFirmwareEntryFromText -Text $text
    if ($id) {
        try {
            New-Item -ItemType Directory -Force (Split-Path $script:FlightSimEntryCache) | Out-Null
            Set-Content $script:FlightSimEntryCache $id -Encoding ascii
        } catch { }
    }
    return $id
}

function Set-NextBootLinux {
    # $true only when the firmware accepted the one-shot. The caller reboots
    # either way: on a machine where linux/set-boot-order.sh never ran, the
    # GRUB saved default is still Linux and a plain reboot gets there.
    $id = Get-LinuxFirmwareEntryId
    if (-not $id) { return $false }
    try {
        & bcdedit /set '{fwbootmgr}' bootsequence $id 2>$null | Out-Null
    } catch { return $false }
    return ($LASTEXITCODE -eq 0)
}
