<#
.SYNOPSIS
    Make one monitor the primary display.

.DESCRIPTION
    Both games this desk launches take the primary display and nothing else:
    Squadrons is Frostbite and has no monitor picker at all, and MSFS honours
    its own setting only until something else moves the desktop around. So
    "launch on the ultrawide" is really "make the ultrawide primary first",
    which is what this does.

    Windows has no cmdlet for it. The supported route is ChangeDisplaySettingsEx
    with CDS_SET_PRIMARY, and the part that is easy to get wrong is that the
    primary display defines the origin: whichever monitor becomes primary must
    be moved to (0,0) and EVERY other monitor shifted by the same delta, or the
    desktop ends up with a gap in it and windows land off-screen. Each display
    is staged with CDS_NORESET and the whole arrangement applied in one call at
    the end, so the desktop rearranges once rather than flickering per monitor.

    Idempotent by design: a monitor that is already primary is left alone
    rather than re-applied, because this runs on every boot and a needless
    mode set costs a second of black screen and re-shuffled windows.

.PARAMETER Match
    Case-insensitive substring identifying the monitor, wildcards allowed.
    Tested against the device name, the monitor's PnP id, its description, the
    adapter, and its current resolution - so "SAM", "3840x1080" and "DISPLAY3"
    all work. Ambiguity is an error, never a guess: two identical 1920x1080
    panels both match "1920x1080", and picking the wrong one to be primary
    rearranges the whole desktop.

.PARAMETER List
    Print what this machine has and exit. Run this first to find a Match
    string that identifies the panel you mean.

.EXAMPLE
    .\set-primary-display.ps1 -List
    .\set-primary-display.ps1 -Match 3840x1080

.NOTES
    Must run as the logged-on interactive user. A scheduled task running as
    SYSTEM has no desktop to rearrange and will fail with DISP_CHANGE_BADPARAM.
#>
[CmdletBinding()]
param(
    [string]$Match,
    [switch]$List
)

# Deliberately no Set-StrictMode and no $ErrorActionPreference here. This file
# is DOT-SOURCED by jarvis-greeting.ps1, and dot-sourcing runs in the caller's
# scope - both would leak into the rest of the greeting and change how it
# handles its own errors, several of which it survives on purpose. Every
# failure path below throws explicitly instead of relying on a preference.

# Guard the Add-Type: this script is dot-sourced by jarvis-greeting.ps1, and a
# second definition in the same session is a hard error rather than a no-op.
if (-not ('DualBoot.Display' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace DualBoot {

[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct DISPLAY_DEVICE {
    public int cb;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]  public string DeviceName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
    public int StateFlags;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
}

[StructLayout(LayoutKind.Sequential)]
public struct POINTL { public int x; public int y; }

// The display flavour of DEVMODEW. The printer union (dmOrientation through
// dmPrintQuality, eight shorts) occupies the same sixteen bytes as
// dmPosition + dmDisplayOrientation + dmDisplayFixedOutput, so laying out the
// display members directly is correct and keeps the overall size right.
[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct DEVMODE {
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
    public short dmSpecVersion;
    public short dmDriverVersion;
    public short dmSize;
    public short dmDriverExtra;
    public int   dmFields;
    public POINTL dmPosition;
    public int   dmDisplayOrientation;
    public int   dmDisplayFixedOutput;
    public short dmColor;
    public short dmDuplex;
    public short dmYResolution;
    public short dmTTOption;
    public short dmCollate;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
    public short dmLogPixels;
    public int   dmBitsPerPel;
    public int   dmPelsWidth;
    public int   dmPelsHeight;
    public int   dmDisplayFlags;
    public int   dmDisplayFrequency;
    public int   dmICMMethod;
    public int   dmICMIntent;
    public int   dmMediaType;
    public int   dmDitherType;
    public int   dmReserved1;
    public int   dmReserved2;
    public int   dmPanningWidth;
    public int   dmPanningHeight;
}

public static class Display {
    public const int ENUM_CURRENT_SETTINGS = -1;
    public const int DM_POSITION = 0x00000020;
    public const int CDS_UPDATEREGISTRY = 0x00000001;
    public const int CDS_SET_PRIMARY = 0x00000010;
    public const int CDS_NORESET = 0x10000000;
    public const int DISP_CHANGE_SUCCESSFUL = 0;
    public const int ATTACHED_TO_DESKTOP = 0x00000001;
    public const int PRIMARY_DEVICE = 0x00000004;
    // Makes the second-level enumeration return the device interface path
    // rather than a registry key - that path carries the EDID vendor id, which
    // is the only place "SAM" appears for a Samsung panel that otherwise
    // insists it is a Generic PnP Monitor.
    public const int EDD_GET_DEVICE_INTERFACE_NAME = 0x00000001;

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool EnumDisplayDevices(string lpDevice, uint iDevNum,
        ref DISPLAY_DEVICE lpDisplayDevice, uint dwFlags);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool EnumDisplaySettings(string lpszDeviceName,
        int iModeNum, ref DEVMODE lpDevMode);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int ChangeDisplaySettingsEx(string lpszDeviceName,
        ref DEVMODE lpDevMode, IntPtr hwnd, int dwflags, IntPtr lParam);

    // The final apply: a null device and a null mode commit everything that
    // was staged with CDS_NORESET.
    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "ChangeDisplaySettingsExW")]
    public static extern int ApplyStagedChanges(IntPtr lpszDeviceName,
        IntPtr lpDevMode, IntPtr hwnd, int dwflags, IntPtr lParam);

    // Sized from the TYPE, never from an instance: PowerShell hands these
    // around as boxed structs inside a PSObject, and asking Marshal to size
    // one of those is a question with more than one plausible answer.
    public static int DevModeSize() { return Marshal.SizeOf(typeof(DEVMODE)); }
    public static int DisplayDeviceSize() { return Marshal.SizeOf(typeof(DISPLAY_DEVICE)); }
}

}
'@
}

function Get-AttachedDisplay {
    <#  Every monitor currently part of the desktop, with where it sits, what
        it is called, and whether it is primary. #>
    # Enumerated through System.Windows.Forms.Screen, not EnumDisplayDevices.
    #
    # The interop version returned FALSE at index 0 on a machine with three
    # displays plainly attached. The reason is PowerShell, not Windows: binding
    # $null to a [string] parameter yields [string]::Empty, so the call that
    # was meant to say "enumerate the adapters" asked for the adapter named ""
    # instead, and got told there is no such thing. The reported Win32 error
    # was 203, ERROR_ENVVAR_NOT_FOUND, which is nothing to do with displays and
    # was simply whatever the thread had lying around.
    #
    # Screen.AllScreens is a managed API that needs no NULL, was verified
    # working on that exact machine, and gives everything the matching and the
    # origin-shift arithmetic need. The interop that remains is the one call
    # with no managed equivalent: actually setting the primary.
    Add-Type -AssemblyName System.Windows.Forms

    $out = @()
    foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
        # The monitor behind the output, for its PnP id - that path carries the
        # EDID vendor code, which is the only place "SAM" appears on a Samsung
        # that otherwise insists it is a Generic PnP Monitor. This call passes a
        # REAL device name, so the empty-string trap above does not apply, and
        # it is best-effort regardless.
        $mon = New-Object DualBoot.DISPLAY_DEVICE
        $mon.cb = [DualBoot.Display]::DisplayDeviceSize()
        $monId = ''
        $monName = ''
        if ([DualBoot.Display]::EnumDisplayDevices($s.DeviceName, 0, [ref]$mon,
                [DualBoot.Display]::EDD_GET_DEVICE_INTERFACE_NAME)) {
            $monId = $mon.DeviceID
            $monName = $mon.DeviceString
        }

        $out += [pscustomobject]@{
            Device    = $s.DeviceName
            Adapter   = ''
            Monitor   = $monName
            MonitorId = $monId
            Width     = $s.Bounds.Width
            Height    = $s.Bounds.Height
            X         = $s.Bounds.X
            Y         = $s.Bounds.Y
            Primary   = $s.Primary
        }
    }
    return $out
}

function Set-PrimaryDisplay {
    <#  Make the monitor identified by $Match primary, moving it to the origin
        and carrying every other monitor with it. Returns a one-line summary of
        what happened; throws only on a genuine failure. #>
    param([Parameter(Mandatory)][string]$Match)

    $displays = @(Get-AttachedDisplay)
    if ($displays.Count -eq 0) { throw 'no displays attached to the desktop' }

    $hits = @($displays | Where-Object {
        $_.Device -like "*$Match*" -or $_.MonitorId -like "*$Match*" -or
        $_.Monitor -like "*$Match*" -or $_.Adapter -like "*$Match*" -or
        "$($_.Width)x$($_.Height)" -like "*$Match*"
    })

    if ($hits.Count -eq 0) {
        throw "no display matches '$Match' - run set-primary-display.ps1 -List to see what this machine has"
    }
    # Never guess. Choosing the wrong monitor here rearranges the whole desktop.
    if ($hits.Count -gt 1) {
        $names = ($hits | ForEach-Object { "$($_.Device) ($($_.Width)x$($_.Height))" }) -join ', '
        throw "'$Match' matches more than one display: $names"
    }

    $target = $hits[0]
    if ($target.Primary) {
        return "$($target.Device) ($($target.Width)x$($target.Height)) is already primary"
    }

    # The primary display IS the origin, so everything shifts by however far
    # the new primary was from it.
    $dx = -$target.X
    $dy = -$target.Y

    # New primary first, then everyone else. The ordering is not incidental:
    # CDS_SET_PRIMARY is what re-defines the origin, and staging the followers
    # against an origin that has not moved yet is how you end up with a desktop
    # arranged around a monitor that is no longer at (0,0).
    $ordered = @($target) + @($displays | Where-Object { $_.Device -ne $target.Device })

    foreach ($d in $ordered) {
        # Built rather than read back. ChangeDisplaySettingsEx honours exactly
        # the fields named in dmFields and ignores the rest, so a DEVMODE
        # carrying nothing but a position cannot disturb anyone's resolution or
        # refresh rate on its way past - which matters on a panel whose 120 Hz
        # took an evening to win. It also removes the second EnumDisplaySettings
        # dependency from this path entirely.
        $mode = New-Object DualBoot.DEVMODE
        $mode.dmSize = [short][DualBoot.Display]::DevModeSize()
        $pos = New-Object DualBoot.POINTL
        $pos.x = $d.X + $dx
        $pos.y = $d.Y + $dy
        $mode.dmPosition = $pos
        # Position only: everything else in the mode stays exactly as found, so
        # this cannot quietly change anyone's resolution or refresh rate.
        $mode.dmFields = [DualBoot.Display]::DM_POSITION

        $flags = [DualBoot.Display]::CDS_UPDATEREGISTRY -bor [DualBoot.Display]::CDS_NORESET
        if ($d.Device -eq $target.Device) {
            $flags = $flags -bor [DualBoot.Display]::CDS_SET_PRIMARY
        }
        $rc = [DualBoot.Display]::ChangeDisplaySettingsEx(
            $d.Device, [ref]$mode, [IntPtr]::Zero, $flags, [IntPtr]::Zero)
        if ($rc -ne [DualBoot.Display]::DISP_CHANGE_SUCCESSFUL) {
            throw "staging $($d.Device) failed (DISP_CHANGE $rc)"
        }
    }

    $rc = [DualBoot.Display]::ApplyStagedChanges(
        [IntPtr]::Zero, [IntPtr]::Zero, [IntPtr]::Zero, 0, [IntPtr]::Zero)
    if ($rc -ne [DualBoot.Display]::DISP_CHANGE_SUCCESSFUL) {
        throw "applying the new arrangement failed (DISP_CHANGE $rc)"
    }
    return "$($target.Device) ($($target.Width)x$($target.Height)) is now primary"
}

# Dot-sourced by jarvis-greeting.ps1 for the functions above; run directly it
# is the operator's tool. $MyInvocation.InvocationName is '.' when sourced.
if ($MyInvocation.InvocationName -ne '.') {
    if ($List -or -not $Match) {
        $found = @(Get-AttachedDisplay)
        # Never print nothing. Format-Table on an empty list outputs not one
        # character, which is indistinguishable from the script having failed
        # to run at all - and that is exactly how this first landed on the
        # workstation: a bare prompt, and no way to tell which half was wrong.
        if ($found.Count -eq 0) {
            Write-Host 'No displays enumerated.'
            Write-Host "  display devices seen : $script:DiagSeen"
            Write-Host "  attached to desktop  : $script:DiagAttached"
            Write-Host "  refused a mode read  : $script:DiagNoMode"
            if ($script:DiagErr) {
                Write-Host "  EnumDisplayDevices failed immediately, Win32 error $script:DiagErr"
            }
            Write-Host ''
            Write-Host 'Cross-check with a path that uses none of this script''s interop:'
            Write-Host '  Add-Type -AssemblyName System.Windows.Forms'
            Write-Host '  [System.Windows.Forms.Screen]::AllScreens | Format-Table DeviceName, Bounds, Primary'
            Write-Host 'If that lists your monitors, the fault is in here and not in Windows.'
            exit 1
        }
        $found | Format-Table Device, Width, Height, Refresh, X, Y,
            Primary, Monitor, MonitorId -AutoSize
        if (-not $List -and -not $Match) {
            Write-Host 'Pass -Match with something from the table above, e.g. -Match 3840x1080'
        }
        exit 0
    }
    try {
        Write-Host (Set-PrimaryDisplay -Match $Match)
        exit 0
    } catch {
        Write-Error $_
        exit 1
    }
}
