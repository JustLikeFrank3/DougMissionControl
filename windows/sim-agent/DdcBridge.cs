// DdcBridge.cs — monitor input switching over DDC/CI, VCP code 0x60.
//
// The shared monitor hangs off this machine's GPU on one input and the Mac
// mini on another; DDC/CI rides the video cable, so whichever host is
// CONNECTED can command the switch — it does not need to be the ACTIVE input.
// (Most monitors honour DDC on inactive inputs; some do not. If this monitor
// stops answering once the Mac owns the screen, the switch-back button will
// report sent-but-nothing-happened, which the panel already knows how to say.)
//
// Same honesty contract as every other control: "sent" means SetVCPFeature
// returned success, never "the monitor obeyed". The observed current input is
// read back with GetVCPFeature and published alongside, so the panel renders
// what the monitor reports, not what was asked of it.
//
// MCCS input source codes (VCP 0x60): 0x11 HDMI1, 0x12 HDMI2. Vendors deviate;
// the raw observed value is passed through so a deviant monitor is visible
// rather than mistranslated.

using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace FlightDeckSimAgent;

internal sealed class DdcBridge
{
    private const byte VCP_INPUT = 0x60;

    public static readonly Dictionary<string, uint> Inputs = new()
    {
        ["hdmi1"] = 0x11,
        ["hdmi2"] = 0x12,
    };

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PHYSICAL_MONITOR
    {
        public IntPtr hPhysicalMonitor;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string szPhysicalMonitorDescription;
    }

    private delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, IntPtr rect, IntPtr data);

    [DllImport("user32.dll")]
    private static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc proc, IntPtr data);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool GetNumberOfPhysicalMonitorsFromHMONITOR(IntPtr hMonitor, out uint count);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool GetPhysicalMonitorsFromHMONITOR(IntPtr hMonitor, uint count,
        [Out] PHYSICAL_MONITOR[] monitors);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool SetVCPFeature(IntPtr hMonitor, byte code, uint value);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool GetVCPFeatureAndVCPFeatureReply(IntPtr hMonitor, byte code,
        IntPtr type, out uint current, out uint max);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool DestroyPhysicalMonitor(IntPtr hMonitor);

    private static List<PHYSICAL_MONITOR> Enumerate()
    {
        var found = new List<PHYSICAL_MONITOR>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, (hMon, _, _, _) =>
        {
            if (GetNumberOfPhysicalMonitorsFromHMONITOR(hMon, out var n) && n > 0)
            {
                var phys = new PHYSICAL_MONITOR[n];
                if (GetPhysicalMonitorsFromHMONITOR(hMon, n, phys)) found.AddRange(phys);
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    /// <summary>Every physical monitor with its OBSERVED current input.</summary>
    public JsonObject Snapshot()
    {
        var monitors = new JsonArray();
        foreach (var m in Enumerate())
        {
            uint current = 0, max = 0;
            var readable = GetVCPFeatureAndVCPFeatureReply(
                m.hPhysicalMonitor, VCP_INPUT, IntPtr.Zero, out current, out max);
            var name = Inputs.FirstOrDefault(kv => kv.Value == (current & 0xFF)).Key;
            monitors.Add(new JsonObject
            {
                ["desc"] = m.szPhysicalMonitorDescription?.Trim() ?? "",
                ["ddc"] = readable,
                // Raw and translated both: a vendor-deviant code stays visible.
                ["input_raw"] = readable ? (current & 0xFF) : null,
                ["input"] = readable ? (name ?? "other") : null,
            });
            DestroyPhysicalMonitor(m.hPhysicalMonitor);
        }
        return new JsonObject { ["monitors"] = monitors };
    }

    /// <summary>
    /// Command every monitor to the named input. "sent" per monitor means the
    /// DDC write returned success — the read-back on the next snapshot is the
    /// only proof the monitor actually moved.
    /// </summary>
    public JsonObject Switch(string input)
    {
        if (!Inputs.TryGetValue(input, out var code))
            return new JsonObject { ["ok"] = false, ["reason"] = "input must be hdmi1 or hdmi2" };

        var results = new JsonArray();
        var any = false;
        foreach (var m in Enumerate())
        {
            var sent = SetVCPFeature(m.hPhysicalMonitor, VCP_INPUT, code);
            any |= sent;
            results.Add(new JsonObject
            {
                ["desc"] = m.szPhysicalMonitorDescription?.Trim() ?? "",
                ["sent"] = sent,
            });
            DestroyPhysicalMonitor(m.hPhysicalMonitor);
        }
        return new JsonObject { ["ok"] = any, ["input"] = input, ["monitors"] = results };
    }
}
