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

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct MONITORINFOEX
    {
        public int cbSize;
        public RECT rcMonitor, rcWork;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szDevice;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFOEX info);

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

    // Capabilities are asked ONCE per panel, in the background, and cached
    // forever: the HP 32f never answers the request at all — it hung /monitor
    // for over a minute when this ran inline, which took the whole SCREENS
    // surface down. A panel's input list cannot change, so one attempt is
    // enough, and "unknown" simply means the UI offers every input.
    private static readonly Dictionary<string, List<string>?> CapsCache = new();
    private static readonly HashSet<string> CapsAsked = new();

    private static List<string>? CachedInputs(IntPtr hMonitor, string key)
    {
        lock (CapsCache)
        {
            if (CapsCache.TryGetValue(key, out var hit)) return hit;
            if (!CapsAsked.Add(key)) return null;   // in flight
        }
        // Deliberately fire-and-forget on its own thread: the P/Invoke cannot
        // be cancelled, so a panel that never replies costs one parked thread
        // once, rather than every caller of /monitor forever.
        new Thread(() =>
        {
            var got = DeclaredInputs(hMonitor);
            lock (CapsCache) CapsCache[key] = got;
        })
        { IsBackground = true, Name = "ddc-caps" }.Start();
        return null;
    }

    /// <summary>
    /// The inputs a monitor DECLARES, parsed from its MCCS capabilities string
    /// — "60(01 11 12)" means VGA/HDMI1/HDMI2 and nothing else. Null when the
    /// panel won't answer, in which case the UI offers everything.
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

    // Physical monitor plus where its desktop rectangle sits, so the panel can
    // say "left"/"right" from geometry instead of from a hardcoded guess that
    // rots the moment the desk is rearranged.
    private readonly record struct Found(PHYSICAL_MONITOR Mon, int X, int Y, int W, int H, bool Primary);

    private static List<Found> Enumerate()
    {
        var found = new List<Found>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, (hMon, _, _, _) =>
        {
            if (GetNumberOfPhysicalMonitorsFromHMONITOR(hMon, out var n) && n > 0)
            {
                var phys = new PHYSICAL_MONITOR[n];
                if (GetPhysicalMonitorsFromHMONITOR(hMon, n, phys))
                {
                    var info = new MONITORINFOEX { cbSize = Marshal.SizeOf<MONITORINFOEX>() };
                    var ok = GetMonitorInfo(hMon, ref info);
                    var r = info.rcMonitor;
                    foreach (var p in phys)
                    {
                        found.Add(new Found(p,
                            ok ? r.Left : 0, ok ? r.Top : 0,
                            ok ? r.Right - r.Left : 0, ok ? r.Bottom - r.Top : 0,
                            ok && (info.dwFlags & 1) != 0));   // MONITORINFOF_PRIMARY
                    }
                }
            }
            return true;
        }, IntPtr.Zero);
        // Top row first, then left-to-right within a row — the desk is stacked
        // (a G9 above a pair of HPs), not a single line. Rows are grouped by
        // vertical overlap rather than exact Y, since panels of different
        // heights rarely share a top edge.
        found.Sort((a, b) =>
        {
            var sameRow = a.Y < b.Y + b.H / 2 && b.Y < a.Y + a.H / 2;
            return sameRow ? a.X.CompareTo(b.X) : a.Y.CompareTo(b.Y);
        });
        return found;
    }

    /// <summary>Every physical monitor, indexed, with its OBSERVED input.</summary>
    public JsonObject Snapshot()
    {
        var monitors = new JsonArray();
        var idx = 0;
        var all = Enumerate();
        foreach (var f in all)
        {
            var m = f.Mon;
            uint current = 0, max = 0;
            var readable = GetVCPFeatureAndVCPFeatureReply(
                m.hPhysicalMonitor, VCP_INPUT, IntPtr.Zero, out current, out max);
            var name = Inputs.FirstOrDefault(kv => kv.Value == (current & 0xFF)).Key;
            // Cached/background — never blocks this response. See CachedInputs.
            var declared = readable
                ? CachedInputs(m.hPhysicalMonitor, $"{f.X},{f.Y},{f.W}x{f.H}")
                : null;
            // Position word from geometry, never a guess. Row-aware, because
            // this desk stacks: a monitor alone on its row is TOP/BOTTOM,
            // while panels sharing a row get LEFT/CENTRE/RIGHT among
            // themselves. Rearranging the desk relabels the panel by itself.
            var row = all.Where(o => o.Y < f.Y + f.H / 2 && f.Y < o.Y + o.H / 2)
                         .OrderBy(o => o.X).ToList();
            var rowCount = all.Select(o => (o.Y + o.H / 2) / 400).Distinct().Count();
            var inRow = row.FindIndex(o => o.X == f.X && o.Y == f.Y);
            string pos;
            if (row.Count == 1)
                pos = rowCount <= 1 ? "" : (f.Y + f.H / 2 <= all.Min(o => o.Y + o.H / 2) ? "TOP" : "BOTTOM");
            else
                pos = row.Count == 2 ? (inRow == 0 ? "LEFT" : "RIGHT")
                    : row.Count == 3 ? (inRow == 0 ? "LEFT" : inRow == 1 ? "CENTRE" : "RIGHT")
                    : $"#{inRow + 1}";
            monitors.Add(new JsonObject
            {
                ["index"] = idx++,
                ["desc"] = m.szPhysicalMonitorDescription?.Trim() ?? "",
                ["position"] = pos,
                ["x"] = f.X, ["y"] = f.Y, ["w"] = f.W, ["h"] = f.H,
                ["primary"] = f.Primary,
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
        foreach (var f in Enumerate())
        {
            var m = f.Mon;
            if (index < 0 || idx == index)
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
