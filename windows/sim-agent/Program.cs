// Program.cs — flightdeck-sim-agent.
//
// Owns the current snapshot and the sequence number, maps Flight Deck commands
// onto sim events, and publishes state to anyone holding the token.
//
// The rule this file exists to enforce: `accepted` means the event was
// transmitted, never that it worked. Nothing here ever writes a commanded value
// into the published state. The only thing that moves gear.state is the sim
// telling us the gear moved. A command MSFS ignores — wrong aircraft, paused
// sim, gear already up — therefore shows on the panel as a command that got no
// response, instead of as a lie about where the gear is.
//
// Losing the sim is expected. The workstation reboots into Linux, this process
// vanishes, deck-api reads that as SIM OFFLINE and the panel returns to the
// wallboard. No retry alarms, no failure state.

using System.Text.Json.Nodes;

namespace FlightDeckSimAgent;

internal static class Program
{
    private const string Version = "0.1.0";
    private const int Port = 9109;   // not 9108: under Linux this workstation
                                     // already serves 9108 as a liveness probe
    private const string TokenPath = @"C:\ProgramData\dualboot\sim-agent.token";

    // Readouts must reach the panel at >= 4 Hz even when nothing is moving.
    private static readonly TimeSpan ReadoutInterval = TimeSpan.FromMilliseconds(200);

    private static readonly DateTime Started = DateTime.UtcNow;

    private static readonly object PublishLock = new();
    private static JsonObject? _snapshot;
    private static long _seq;
    private static SimStateRaw _lastPublished;
    private static DateTime _lastPublishedAt = DateTime.MinValue;

    private static SimBridge _sim = null!;
    private static HttpApi _api = null!;

    private static int Main(string[] args)
    {
        // Step 0 instrument: dump every SimVar live and exit. No token, no
        // listener — it only reads, so it can run while the agent is running.
        if (args.Contains("--probe")) return Probe.Run();
        if (args.Contains("--probe-events")) return Probe.RunEvents();
        if (args.Contains("--try-event"))
        {
            var i = Array.IndexOf(args, "--try-event");
            var name = i + 1 < args.Length ? args[i + 1] : "";
            uint data = 0;
            var w = Array.IndexOf(args, "--watch");
            var watch = w >= 0 && w + 1 < args.Length ? args[w + 1] : "AUTOPILOT MASTER";
            var d = Array.IndexOf(args, "--data");
            if (d >= 0 && d + 1 < args.Length) uint.TryParse(args[d + 1], out data);
            if (name.Length == 0) { Console.Error.WriteLine("usage: --try-event NAME [--data N] [--watch \"SIMVAR\"]"); return 1; }
            return Probe.TryEvent(name, data, watch, "Bool");
        }

        string token;
        try
        {
            token = (File.ReadLines(TokenPath).FirstOrDefault() ?? "").Trim();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            Console.Error.WriteLine($"cannot read {TokenPath}: {ex.Message}");
            Console.Error.WriteLine("run windows/setup-sim-agent.ps1 to create it");
            return 1;
        }

        if (token.Length == 0)
        {
            Console.Error.WriteLine($"{TokenPath} is empty; refusing to serve an unguarded endpoint");
            return 1;
        }

        _sim = new SimBridge();
        _sim.StateReceived += OnStateReceived;

        var media = new MediaBridge();
        var ddc = new DdcBridge();

        _api = new HttpApi(Port, token)
        {
            Health = BuildHealth,
            State = CurrentSnapshot,
            Command = HandleCommandAsync,
            Media = async () => { await media.RefreshAsync(); return media.Snapshot(); },
            MediaArt = media.Art,
            MediaCommand = async a => new JsonObject
            {
                ["sent"] = await media.CommandAsync(a),
            },
            Monitor = ddc.Snapshot,
            MonitorSwitch = ddc.Switch,
        };

        _api.Start();
        _sim.Start();
        Console.WriteLine($"flightdeck-sim-agent {Version} listening on :{Port}");

        using var stop = new ManualResetEventSlim(false);
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Set(); };
        stop.Wait();

        _sim.Dispose();
        _api.Dispose();
        return 0;
    }

    private static JsonObject BuildHealth() => new()
    {
        ["agent"] = "flightdeck-sim-agent",
        ["version"] = Version,
        ["sim"] = new JsonObject
        {
            // connected means a live SimConnect session, not "the process is running"
            ["connected"] = _sim.Connected,
            ["name"] = _sim.SimName,
            ["aircraft"] = _sim.Aircraft,
        },
        ["uptime_s"] = (int)(DateTime.UtcNow - Started).TotalSeconds,
    };

    /// <summary>
    /// Called on the pump thread for every state frame. Publishes when a control
    /// has actually moved, and otherwise at the readout cadence, so "on change"
    /// keeps meaning something while IAS still updates smoothly.
    /// </summary>
    private static void OnStateReceived(SimStateRaw state, SimCapsRaw caps, SimGpsRaw gps, string aircraft)
    {
        lock (PublishLock)
        {
            var due = DateTime.UtcNow - _lastPublishedAt >= ReadoutInterval;
            var moved = _lastPublishedAt == DateTime.MinValue || Normalize.ControlsDiffer(state, _lastPublished);
            if (!due && !moved) return;

            var ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            var snapshot = Normalize.State(++_seq, ts, aircraft, state, caps, gps);

            _lastPublished = state;
            _lastPublishedAt = DateTime.UtcNow;
            _snapshot = snapshot;
            _api.Broadcast(snapshot);
        }
    }

    /// <summary>The latest published snapshot, or null before the first frame.</summary>
    private static JsonObject? CurrentSnapshot()
    {
        lock (PublishLock) return _snapshot;
    }

    private static async Task<JsonObject> HandleCommandAsync(JsonObject body)
    {
        var cmdId = body["cmd_id"]?.ToString() ?? "";
        var control = body["control"]?.ToString() ?? "";
        var action = (body["action"]?.ToString() ?? "").ToLowerInvariant();
        var value = body["value"]?.ToString();

        JsonObject Reject(string reason) => new()
        {
            ["cmd_id"] = cmdId,
            ["accepted"] = false,
            ["reason"] = reason,
        };

        if (!_sim.Connected || !_sim.TryGetState(out var state, out var caps))
            return Reject("sim not connected");

        // A control this airframe does not have is absent from /state, so a
        // command for it is rejected with a reason the panel can show rather
        // than transmitted into the void.
        var available = Normalize.Controls(state, caps);
        if (!available.ContainsKey(control))
            return Reject(available.Count == 0 ? "sim not connected" : "control not available on this aircraft");

        var resolved = Resolve(control, action, value, state, caps);
        if (resolved.Reason is not null) return Reject(resolved.Reason);

        // Already where it was asked to be. Nothing is transmitted, and `noop`
        // tells the panel not to wait for movement that will never come.
        if (resolved.Event is null)
        {
            return new JsonObject
            {
                ["cmd_id"] = cmdId,
                ["accepted"] = true,
                ["noop"] = true,
                ["seq"] = Interlocked.Read(ref _seq),
            };
        }

        var sent = _sim.TransmitAsync(resolved.Event.Value, resolved.Data);
        var done = await Task.WhenAny(sent, Task.Delay(TimeSpan.FromMilliseconds(500)));
        if (done != sent) return Reject("agent busy");
        if (!await sent) return Reject("sim not connected");

        return new JsonObject
        {
            ["cmd_id"] = cmdId,
            ["accepted"] = true,
            ["seq"] = Interlocked.Read(ref _seq),
        };
    }

    private readonly record struct Resolved(Event? Event, uint Data, string? Reason);

    private static Resolved Invalid(string reason) => new(null, 0, reason);
    private static Resolved Send(Event e, uint data = 0) => new(e, data, null);
    private static readonly Resolved Noop = new(null, 0, null);

    /// <summary>
    /// Flight Deck vocabulary in, one sim event out.
    ///
    /// PARKING_BRAKES and AP_MASTER are toggles, so explicit set/off semantics
    /// come from comparing against observed state first: transmit only when the
    /// two differ. A toggle that gets dropped can then only fail to move the
    /// state, never invert it. Landing lights get real explicit events, which is
    /// why they are preferred over LANDING_LIGHTS_TOGGLE.
    /// </summary>
    private static Resolved Resolve(string control, string action, string? value, SimStateRaw s, SimCapsRaw c)
    {
        bool On(double v) => v > 0.5;

        switch (control)
        {
            case "gear":
                if (action == "toggle") return Send(Event.GearToggle);
                if (action != "set") return Invalid("unsupported action");
                return value switch
                {
                    "up" => Send(Event.GearUp),
                    "down" => Send(Event.GearDown),
                    _ => Invalid("invalid value"),
                };

            case "flaps":
                if (action == "incr") return Send(Event.FlapsIncr);
                if (action == "decr") return Send(Event.FlapsDecr);
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var index)) return Invalid("invalid value");
                var detents = Normalize.Detents(c.FlapsNumHandlePositions);
                if (index < 0 || index >= Math.Max(detents, 1)) return Invalid("invalid value");
                return Send(Event.FlapsSet, (uint)index);

            case "parking_brake":
                if (action == "toggle") return Send(Event.ParkingBrakes);
                if (action != "set") return Invalid("unsupported action");
                // Explicit, not the toggle-with-differ-guard this used to be:
                // PARKING_BRAKE_SET was confirmed to honour a 1/0 parameter on
                // an A320neo (--probe-events). Same reasoning as the landing
                // lights — a dropped frame can then fail to move the brake, but
                // never invert it.
                return value switch
                {
                    "set" => Send(Event.ParkingBrakeSet, 1),
                    "off" => Send(Event.ParkingBrakeSet, 0),
                    _ => Invalid("invalid value"),
                };

            case "landing_lights":
                if (action == "toggle")
                    return Send(On(s.LightLanding) ? Event.LandingLightsOff : Event.LandingLightsOn);
                if (action != "set") return Invalid("unsupported action");
                return value switch
                {
                    "on" => Send(Event.LandingLightsOn),
                    "off" => Send(Event.LandingLightsOff),
                    _ => Invalid("invalid value"),
                };

            case "ap_master":
                if (action == "toggle") return Send(Event.ApMaster);
                if (action != "set") return Invalid("unsupported action");
                return value switch
                {
                    "engaged" => On(s.AutopilotMaster) ? Noop : Send(Event.ApMaster),
                    "off" => On(s.AutopilotMaster) ? Send(Event.ApMaster) : Noop,
                    _ => Invalid("invalid value"),
                };

            case "ap_hdg":
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var hdg)) return Invalid("invalid value");
                return Send(Event.HeadingBugSet, (uint)(((hdg % 360) + 360) % 360));

            case "ap_alt":
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var alt) || alt < 0 || alt > 60000) return Invalid("invalid value");
                return Send(Event.ApAltVarSet, (uint)alt);

            case "ap_vs":
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var vs) || Math.Abs(vs) > 8000) return Invalid("invalid value");
                // Negative climbs ride as two's complement — the sim reads the
                // event's DWORD back as signed.
                return Send(Event.ApVsVarSet, unchecked((uint)vs));

            case "ap_spd":
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var spd) || spd < 0 || spd > 900) return Invalid("invalid value");
                return Send(Event.ApSpdVarSet, (uint)spd);

            case "com1" or "com2":
            {
                var (set, swap) = control == "com1"
                    ? (Event.Com1StbySet, Event.Com1Swap)
                    : (Event.Com2StbySet, Event.Com2Swap);
                if (action == "swap") return Send(swap);
                if (action != "set_sby") return Invalid("unsupported action");
                if (!double.TryParse(value, System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out var com)
                    || com < 118.0 || com > 136.99) return Invalid("invalid value");
                return Send(set, (uint)Math.Round(com * 1_000_000));
            }

            case "nav1" or "nav2":
            {
                var (set, swap) = control == "nav1"
                    ? (Event.Nav1StbySet, Event.Nav1Swap)
                    : (Event.Nav2StbySet, Event.Nav2Swap);
                if (action == "swap") return Send(swap);
                if (action != "set_sby") return Invalid("unsupported action");
                if (!double.TryParse(value, System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out var nav)
                    || nav < 108.0 || nav > 117.95) return Invalid("invalid value");
                return Send(set, (uint)Math.Round(nav * 1_000_000));
            }

            case "xpdr":
                if (action == "ident") return Send(Event.XpdrIdent);
                if (action != "set") return Invalid("unsupported action");
                // Four octal digits, sent as BCD16 — squawk 4321 rides as 0x4321.
                if (value is null || value.Length != 4 || value.Any(ch => ch < '0' || ch > '7'))
                    return Invalid("invalid value");
                var bcd = value.Aggregate(0u, (acc, ch) => (acc << 4) | (uint)(ch - '0'));
                return Send(Event.XpdrSet, bcd);

            case "baro":
                if (action != "set") return Invalid("unsupported action");
                if (!double.TryParse(value, System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out var inhg)
                    || inhg < 26.0 || inhg > 32.0) return Invalid("invalid value");
                // KOHLSMAN_SET wants millibars * 16.
                return Send(Event.BaroSet, (uint)Math.Round(inhg * 33.8639 * 16));

            default:
                return Invalid("unknown control");
        }
    }
}
