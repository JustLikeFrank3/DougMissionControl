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

    // The fleet's actual panels: two HP 32f (HDMI x2 + VGA, no DP) and a
    // Samsung G9 arriving (DP x2 + HDMI). Every input is offered on every
    // card; the observed read-back is what says which ones a panel honours.
    public static readonly Dictionary<string, uint> Inputs = new()
    {
        ["vga"] = 0x01,
        ["dp1"] = 0x0F,
        ["dp2"] = 0x10,
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

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool GetCapabilitiesStringLength(IntPtr hMonitor, out uint length);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool CapabilitiesRequestAndCapabilitiesReply(IntPtr hMonitor,
        [Out] byte[] caps, uint length);

    /// <summary>
    /// The inputs a monitor DECLARES, parsed from its MCCS capabilities string
    /// — "60(01 11 12)" means VGA/HDMI1/HDMI2 and nothing else. Null when the
    /// panel won't answer (the caps request is slow and some panels refuse),
    /// in which case the UI falls back to offering everything.
    /// </summary>
    private static List<string>? DeclaredInputs(IntPtr hMonitor)
    {
        if (!GetCapabilitiesStringLength(hMonitor, out var len) || len == 0) return null;
        var buf = new byte[len];
        if (!CapabilitiesRequestAndCapabilitiesReply(hMonitor, buf, len)) return null;
        var caps = System.Text.Encoding.ASCII.GetString(buf).TrimEnd('\0');
        var m = System.Text.RegularExpressions.Regex.Match(caps, @"60\s*\(([^)]*)\)");
        if (!m.Success) return null;
        var declared = new List<string>();
        foreach (var tok in m.Groups[1].Value.Split(' ', StringSplitOptions.RemoveEmptyEntries))
        {
            if (uint.TryParse(tok, System.Globalization.NumberStyles.HexNumber, null, out var code))
            {
                var name = Inputs.FirstOrDefault(kv => kv.Value == code).Key;
                if (name is not null) declared.Add(name);
            }
        }
        return declared.Count > 0 ? declared : null;
    }

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

    /// <summary>Every physical monitor, indexed, with its OBSERVED input.</summary>
    public JsonObject Snapshot()
    {
        var monitors = new JsonArray();
        var idx = 0;
        foreach (var m in Enumerate())
        {
            uint current = 0, max = 0;
            var readable = GetVCPFeatureAndVCPFeatureReply(
                m.hPhysicalMonitor, VCP_INPUT, IntPtr.Zero, out current, out max);
            var name = Inputs.FirstOrDefault(kv => kv.Value == (current & 0xFF)).Key;
            var declared = readable ? DeclaredInputs(m.hPhysicalMonitor) : null;
            monitors.Add(new JsonObject
            {
                ["index"] = idx++,
                ["desc"] = m.szPhysicalMonitorDescription?.Trim() ?? "",
                ["ddc"] = readable,
                // Raw and translated both: a vendor-deviant code stays visible.
                ["input_raw"] = readable ? (current & 0xFF) : null,
                ["input"] = readable ? (name ?? "other") : null,
                // What the panel says it has — null means it would not say,
                // and the UI should offer everything rather than invent.
                ["inputs"] = declared is null ? null
                    : new JsonArray(declared.Select(d => (JsonNode)d).ToArray()),
            });
            DestroyPhysicalMonitor(m.hPhysicalMonitor);
        }
        return new JsonObject { ["monitors"] = monitors };
    }

    /// <summary>
    /// Command ONE monitor (by enumeration index) to the named input, or every
    /// monitor when index is -1. "sent" means the DDC write returned success —
    /// the read-back on the next snapshot is the only proof anything moved.
    /// </summary>
    public JsonObject Switch(string input, int index)
    {
        if (!Inputs.TryGetValue(input, out var code))
            return new JsonObject { ["ok"] = false, ["reason"] = "input must be one of " + string.Join("/", Inputs.Keys) };

        var results = new JsonArray();
        var any = false;
        var idx = 0;
        foreach (var m in Enumerate())
        {
            var mine = index < 0 || idx == index;
            if (mine)
            {
                var sent = SetVCPFeature(m.hPhysicalMonitor, VCP_INPUT, code);
                any |= sent;
                results.Add(new JsonObject
                {
                    ["index"] = idx,
                    ["desc"] = m.szPhysicalMonitorDescription?.Trim() ?? "",
                    ["sent"] = sent,
                });
            }
            idx++;
            DestroyPhysicalMonitor(m.hPhysicalMonitor);
        }
        if (results.Count == 0)
            return new JsonObject { ["ok"] = false, ["reason"] = $"no monitor at index {index}" };
        return new JsonObject { ["ok"] = any, ["input"] = input, ["monitors"] = results };
    }
}
