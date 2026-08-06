// Probe.cs — Step 0 instrument.  flightdeck-sim-agent --probe
//
// Connects, requests every row of SimVars.cs, and prints them live until Ctrl+C.
// Stands in for the SDK's SimvarWatcher, which ships only in the Samples package
// and is C# source you would have to build. This is the better tool for the job
// anyway: SimvarWatcher would tell us what MSFS reports, whereas Step 0 needs to
// know whether *our* names and *our* units work through *our* code path.
//
// Each variable gets its own data definition and request id. The agent proper
// packs all eleven into one definition for efficiency, but one bad name there
// takes the whole struct down; here a bad name isolates itself and is reported
// against the row that caused it, via GetLastSentPacketID correlation.
//
// Read the derived column: it shows what Normalize would publish. That is what
// settles [Q1] (is gear percent 0..100 or 0..1) and [Q2] (detent count) —
// cycle the gear and the flaps with this running.

using System.Runtime.InteropServices;
using Microsoft.FlightSimulator.SimConnect;

namespace FlightDeckSimAgent;

internal enum ProbeId { Base = 1000 }

internal static class Probe
{
    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    private struct OneDouble { public double Value; }

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    private struct OneString
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Value;
    }

    // Two of our units are ambiguous from a single sample: a gear percent of
    // 1.0 could be 1% or 100%, and a heading of 1.67 could be degrees or
    // radians. Requesting the SAME variable in both candidate units settles it
    // without guessing — if the two readings are identical, SimConnect ignored
    // the unit and we are getting the native one; if they differ by the
    // conversion factor, the unit is being honoured.
    private static readonly (string Name, string Unit)[] Ambiguous =
    {
        ("GEAR TOTAL PCT EXTENDED",        "Percent Over 100"),
        ("PLANE HEADING DEGREES MAGNETIC", "Radians"),
    };

    // GEAR TOTAL PCT EXTENDED reported 1.0% on an aircraft sitting on the
    // runway with its gear plainly down, and the dual-unit check ruled out a
    // unit error. These are the alternative gear readouts, probed together so
    // whichever one actually tracks the gear can be picked on evidence.
    // Unrecognised names show as ERROR on their own row, which is itself an
    // answer. Whatever wins here replaces the variable in SimVars.cs.
    private static readonly (string Name, string Unit)[] Candidates =
    {
        ("GEAR TOTAL PCT EXTENDED",  "Percent"),
        ("GEAR TOTAL PCT EXTENDED",  "Number"),
        ("GEAR POSITION",            "Enum"),
        ("GEAR ANIMATION POSITION",  "Percent"),
    };

    private static readonly (string Name, string Unit)[] Rows =
        SimVars.StateVars.Concat(SimVars.CapsVars).Concat(Ambiguous).Concat(Candidates).ToArray();

    private static double? Find(string name, string unit)
    {
        for (int i = 0; i < Rows.Length; i++)
            if (Rows[i].Name == name && Rows[i].Unit == unit) return Values[i];
        return null;
    }

    private static readonly double?[] Values = new double?[Rows.Length];
    private static readonly string?[] Errors = new string?[Rows.Length];
    private static readonly Dictionary<uint, int> SendIdToRow = new();
    private static string _title = "";
    private static bool _open;

    public static int Run()
    {
        using var wait = new EventWaitHandle(false, EventResetMode.AutoReset);
        SimConnect sim;
        try
        {
            sim = new SimConnect("FlightDeckProbe", IntPtr.Zero, 0, wait, 0);
        }
        catch (COMException ex)
        {
            Console.Error.WriteLine($"cannot reach a simulator: {ex.Message}");
            Console.Error.WriteLine("start MSFS 2024 and load a flight, then run --probe again");
            return 1;
        }

        sim.OnRecvOpen += (_, d) =>
        {
            _open = true;
            Console.WriteLine($"connected to: {d.szApplicationName}\n");
        };
        sim.OnRecvQuit += (_, _) => { _open = false; Console.WriteLine("\nsim closed the session"); };
        sim.OnRecvException += OnException;
        sim.OnRecvSimobjectData += OnData;

        for (int i = 0; i < Rows.Length; i++)
        {
            var (name, unit) = Rows[i];
            var def = (ProbeId)((int)ProbeId.Base + i);
            sim.AddToDataDefinition(def, name, unit, SIMCONNECT_DATATYPE.FLOAT64, 0.0f, SimConnect.SIMCONNECT_UNUSED);
            // Correlate any NAME_UNRECOGNIZED back to the row that caused it.
            SendIdToRow[sim.GetLastSentPacketID()] = i;
            sim.RegisterDataDefineStruct<OneDouble>(def);
            sim.RequestDataOnSimObject(def, def, SimConnect.SIMCONNECT_OBJECT_ID_USER,
                SIMCONNECT_PERIOD.SIM_FRAME, SIMCONNECT_DATA_REQUEST_FLAG.DEFAULT, 0, 5, 0);
        }

        var titleDef = (ProbeId)((int)ProbeId.Base + Rows.Length);
        sim.AddToDataDefinition(titleDef, SimVars.TitleVar, null, SIMCONNECT_DATATYPE.STRING256, 0.0f, SimConnect.SIMCONNECT_UNUSED);
        sim.RegisterDataDefineStruct<OneString>(titleDef);
        sim.RequestDataOnSimObject(titleDef, titleDef, SimConnect.SIMCONNECT_OBJECT_ID_USER,
            SIMCONNECT_PERIOD.SECOND, SIMCONNECT_DATA_REQUEST_FLAG.CHANGED, 0, 0, 0);

        using var stop = new ManualResetEventSlim(false);
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Set(); };

        Console.WriteLine("probing — cycle the gear and the flaps, then Ctrl+C\n");
        var nextPrint = DateTime.UtcNow;
        while (!stop.IsSet)
        {
            if (wait.WaitOne(100))
            {
                try { sim.ReceiveMessage(); }
                catch (COMException) { break; }
            }
            if (DateTime.UtcNow >= nextPrint)
            {
                Print();
                nextPrint = DateTime.UtcNow.AddSeconds(1);
            }
        }

        sim.Dispose();
        Print();
        return Errors.Any(e => e is not null) ? 2 : 0;
    }

    private static void OnException(SimConnect sender, SIMCONNECT_RECV_EXCEPTION data)
    {
        var name = (SIMCONNECT_EXCEPTION)data.dwException;
        if (SendIdToRow.TryGetValue(data.dwSendID, out var row)) Errors[row] = name.ToString();
        else Console.Error.WriteLine($"unattributed exception: {name} (send id {data.dwSendID})");
    }

    private static void OnData(SimConnect sender, SIMCONNECT_RECV_SIMOBJECT_DATA data)
    {
        var idx = (int)data.dwRequestID - (int)ProbeId.Base;
        if (idx == Rows.Length) { _title = ((OneString)data.dwData[0]).Value?.Trim() ?? ""; return; }
        if (idx >= 0 && idx < Rows.Length) Values[idx] = ((OneDouble)data.dwData[0]).Value;
    }

    private static void Print()
    {
        // Redirected output has no console buffer to clear, and asking for one
        // throws. Piping --probe to a file is a normal thing to want.
        if (!Console.IsOutputRedirected)
        {
            try { Console.Clear(); } catch (IOException) { }
        }

        Console.WriteLine($"aircraft : {(_title.Length > 0 ? _title : "(none)")}");
        Console.WriteLine($"session  : {(_open ? "open" : "not open")}\n");
        Console.WriteLine($"{"simvar",-32} {"unit",-10} {"value",12}   derived");
        Console.WriteLine(new string('-', 92));

        for (int i = 0; i < Rows.Length; i++)
        {
            var (name, unit) = Rows[i];
            string value, derived;
            if (Errors[i] is not null) { value = "ERROR"; derived = Errors[i]!; }
            else if (Values[i] is null) { value = "—"; derived = "no data yet"; }
            else { value = Values[i]!.Value.ToString("F3"); derived = Derive(name, Values[i]!.Value); }
            Console.WriteLine($"{name,-32} {unit,-10} {value,12}   {derived}");
        }

        Console.WriteLine();
        Verdict("[Q1] gear percent", "GEAR TOTAL PCT EXTENDED", "Percent", "Percent Over 100", 100.0,
                "unit honoured: \"Percent\" gives 0..100, keep SimVars.cs as is",
                "unit IGNORED: value is native 0..1, set SimVars.cs to \"Percent Over 100\"");
        Verdict("[Q2] heading", "PLANE HEADING DEGREES MAGNETIC", "Degrees", "Radians", 180.0 / Math.PI,
                "unit honoured: \"Degrees\" is real degrees, keep SimVars.cs as is",
                "unit IGNORED: value is native radians, convert in Normalize.Heading");
        Console.WriteLine("[Q3] FLAPS NUM HANDLE POSITIONS should read 3 on the Baron G58");
    }

    /// <summary>
    /// Compares one variable read in two units. Identical readings mean
    /// SimConnect ignored the unit string and handed us the native value;
    /// readings separated by the conversion factor mean it honoured it.
    /// </summary>
    private static void Verdict(string label, string name, string unitA, string unitB, double factor,
                                string honoured, string ignored)
    {
        var a = Find(name, unitA);
        var b = Find(name, unitB);
        if (a is null || b is null) { Console.WriteLine($"{label}: no data yet"); return; }

        var same = Math.Abs(a.Value - b.Value) < 1e-9;
        var scaled = Math.Abs(a.Value - b.Value * factor) < Math.Max(1e-6, Math.Abs(b.Value * factor) * 1e-6);
        var call = same ? ignored : scaled ? honoured : "INCONCLUSIVE — neither identical nor the expected factor";
        Console.WriteLine($"{label}: {unitA}={a.Value:F4}  {unitB}={b.Value:F4}  ->  {call}");
    }

    /// <summary>What Normalize would publish for this value — the actual answer Step 0 wants.</summary>
    private static string Derive(string name, double v) => name switch
    {
        "GEAR HANDLE POSITION" => v > 0.5 ? "handle down" : "handle up",
        "GEAR TOTAL PCT EXTENDED" => $"pct {v:F1}  -> would read \"{Normalize.GearStateFromPct(v)}\"  (REJECTED, see [Q1])",
        "FLAPS NUM HANDLE POSITIONS" => $"detents {Normalize.Detents(v)}",
        // No "looks like radians" hints here: a small value is not evidence of
        // anything, as [Q2] proved. The Verdict block below settles units by
        // reading the same variable in two of them.
        "PLANE HEADING DEGREES MAGNETIC" => $"hdg_mag {Normalize.Heading(v):F0}",
        "TRAILING EDGE FLAPS LEFT ANGLE" => $"angle_deg {v:F1}",
        "BRAKE PARKING POSITION" => v > 0.5 ? "set" : "off",
        "LIGHT LANDING" => v > 0.5 ? "on" : "off",
        "AUTOPILOT MASTER" => v > 0.5 ? "engaged" : "off",
        "AIRSPEED INDICATED" => $"ias_kt {v:F0}",
        "INDICATED ALTITUDE" => $"alt_ft {v:F0}",
        "GEAR CENTER POSITION" or "GEAR LEFT POSITION" or "GEAR RIGHT POSITION"
            or "GEAR ANIMATION POSITION" => $"pct {v:F1}  -> would read \"{Normalize.GearStateFromPct(v)}\"",
        "GEAR POSITION" => $"enum {v:F0}  (0=unknown 1=up 2=down, per docs)",
        _ => v > 0.5 ? "true" : "false",
    };
}
