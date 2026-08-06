// AppsBridge.cs — non-sim application awareness: is Star Wars Squadrons
// running, launching it, and a small open-loop macro deck.
//
// Squadrons has no telemetry API — no SimConnect equivalent, nothing to read
// state back from. The brief's rule applies: keyboard emulation is permitted
// only where no instrumented path exists (true here), and must be labelled
// open-loop. The one guard that keeps macros from being pure keystroke spam:
// a macro fires ONLY when Squadrons owns the foreground window. Anything else
// is refused with a reason, because a keystroke into the wrong window is not
// "open-loop", it is a misdirected input.
//
// Macros map to the game's DEFAULT bindings via
// C:\ProgramData\dualboot\squadrons-macros.json — rebindable there, since the
// game's own settings are the source of truth and nothing here can read them.

using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace FlightDeckSimAgent;

internal sealed class AppsBridge
{
    private const string MacroConfigPath = @"C:\ProgramData\dualboot\squadrons-macros.json";

    // The launcher name is what jarvis-greeting.ps1 already starts; the game
    // process itself is what foreground checks care about.
    private static readonly string[] ProcessNames = { "starwarssquadrons", "starwarssquadrons_launcher" };
    private const string LauncherExe = @"C:\Program Files\EA Games\STAR WARS Squadrons\starwarssquadrons_launcher.exe";

    private Dictionary<string, MacroDef>? _macros;
    private DateTime _macrosLoaded = DateTime.MinValue;

    private sealed record MacroDef(string Label, string Key, string? Hint);

    // ── session ────────────────────────────────────────────────────────────

    public JsonObject Snapshot()
    {
        var proc = Find();
        var snap = new JsonObject
        {
            ["running"] = proc is not null,
            ["focused"] = proc is not null && IsForeground(proc),
        };
        if (proc is not null)
        {
            try { snap["since"] = new DateTimeOffset(proc.StartTime.ToUniversalTime()).ToUnixTimeSeconds(); }
            catch (Exception e) when (e is InvalidOperationException or System.ComponentModel.Win32Exception) { }
        }
        snap["macros"] = MacroList();
        return snap;
    }

    public (bool Ok, string Reason) Launch()
    {
        if (Find() is not null) return (true, "already running");
        if (!File.Exists(LauncherExe)) return (false, "launcher not found on this machine");
        try
        {
            Process.Start(new ProcessStartInfo(LauncherExe) { UseShellExecute = true });
            return (true, "launcher started");
        }
        catch (Exception e) when (e is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            return (false, $"launch failed: {e.Message}");
        }
    }

    // ── macros ─────────────────────────────────────────────────────────────

    /// <summary>
    /// Fire one named macro. Refuses unless the game owns the foreground
    /// window — a keystroke cannot be acknowledged or read back, but it CAN at
    /// least be guaranteed to have gone to the right place. The reply's
    /// open_loop flag is a permanent reminder that "sent" is all we know.
    /// </summary>
    public JsonObject Macro(string id)
    {
        JsonObject Fail(string reason) => new()
        {
            ["sent"] = false, ["open_loop"] = true, ["reason"] = reason,
        };

        var macros = LoadMacros();
        if (!macros.TryGetValue(id, out var def)) return Fail("unknown macro");

        var proc = Find();
        if (proc is null) return Fail("squadrons not running");
        if (!IsForeground(proc)) return Fail("squadrons not focused");

        var vk = ParseKey(def.Key);
        if (vk is null) return Fail($"unmappable key '{def.Key}' in squadrons-macros.json");

        SendKey(vk.Value);
        return new JsonObject { ["sent"] = true, ["open_loop"] = true };
    }

    private JsonArray MacroList()
    {
        var arr = new JsonArray();
        foreach (var (id, def) in LoadMacros())
        {
            arr.Add(new JsonObject
            {
                ["id"] = id, ["label"] = def.Label, ["key"] = def.Key,
                ["hint"] = def.Hint,
            });
        }
        return arr;
    }

    private Dictionary<string, MacroDef> LoadMacros()
    {
        // Reread at most every 5 s so edits to the json land without a restart.
        if (_macros is not null && DateTime.UtcNow - _macrosLoaded < TimeSpan.FromSeconds(5))
            return _macros;

        _macrosLoaded = DateTime.UtcNow;
        try
        {
            if (!File.Exists(MacroConfigPath)) WriteDefaultConfig();
            var root = JsonNode.Parse(File.ReadAllText(MacroConfigPath)) as JsonObject;
            var result = new Dictionary<string, MacroDef>();
            foreach (var (id, node) in root?["macros"] as JsonObject ?? new JsonObject())
            {
                if (node is not JsonObject m) continue;
                result[id] = new MacroDef(
                    m["label"]?.ToString() ?? id,
                    m["key"]?.ToString() ?? "",
                    m["hint"]?.ToString());
            }
            _macros = result;
        }
        catch (Exception e) when (e is IOException or JsonException)
        {
            _macros = new Dictionary<string, MacroDef>();
        }
        return _macros;
    }

    private static void WriteDefaultConfig()
    {
        // These mirror Squadrons' DEFAULT keyboard bindings for power
        // management. The game can be rebound freely, and nothing here can read
        // its settings — if the defaults were changed in game, change them here
        // to match. That responsibility is stated in the file itself.
        const string defaults = """
        {
          "_comment": "Open-loop macros for STAR WARS Squadrons. 'key' must match the IN-GAME binding — the agent cannot read the game's settings, so if a binding was changed there, change it here. Keys: F1-F24, A-Z, 0-9.",
          "macros": {
            "pwr_engines": { "label": "PWR ENGINES", "key": "F1", "hint": "power to engines" },
            "pwr_weapons": { "label": "PWR WEAPONS", "key": "F2", "hint": "power to weapons" },
            "pwr_shields": { "label": "PWR SHIELDS", "key": "F3", "hint": "power to shields" },
            "pwr_balance": { "label": "BALANCE",     "key": "F4", "hint": "reset power balance" },
            "shield_front": { "label": "SHIELD FWD", "key": "F5", "hint": "shields forward" },
            "shield_back":  { "label": "SHIELD AFT", "key": "F6", "hint": "shields aft" }
          }
        }
        """;
        File.WriteAllText(MacroConfigPath, defaults);
    }

    // ── plumbing ───────────────────────────────────────────────────────────

    private static Process? Find()
    {
        foreach (var name in ProcessNames)
        {
            var procs = Process.GetProcessesByName(name);
            // Prefer the game itself over the launcher when both exist.
            if (procs.Length > 0) return procs[0];
        }
        return null;
    }

    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] private static extern uint SendInput(uint n, INPUT[] inputs, int size);

    private static bool IsForeground(Process p)
    {
        GetWindowThreadProcessId(GetForegroundWindow(), out var pid);
        return pid == (uint)p.Id;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT { public uint type; public KEYBDINPUT ki; public long pad; }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr extra; }

    private static void SendKey(ushort vk)
    {
        var down = new INPUT { type = 1, ki = new KEYBDINPUT { wVk = vk } };
        var up = new INPUT { type = 1, ki = new KEYBDINPUT { wVk = vk, dwFlags = 2 /* KEYUP */ } };
        SendInput(1, new[] { down }, Marshal.SizeOf<INPUT>());
        Thread.Sleep(30);   // a held frame, so the game's input loop can't miss it
        SendInput(1, new[] { up }, Marshal.SizeOf<INPUT>());
    }

    private static ushort? ParseKey(string key)
    {
        key = key.Trim().ToUpperInvariant();
        if (key.Length == 1 && key[0] is >= 'A' and <= 'Z' or >= '0' and <= '9') return key[0];
        if (key.StartsWith('F') && int.TryParse(key[1..], out var f) && f is >= 1 and <= 24)
            return (ushort)(0x70 + f - 1);   // VK_F1..F24
        return null;
    }
}
