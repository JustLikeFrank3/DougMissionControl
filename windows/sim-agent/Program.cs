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
        if (args.Contains("--probe-input-events")) return Probe.InputEvents();
        if (args.Contains("--probe-input-event-params")) return Probe.InputEventParams();
        if (args.Contains("--probe-vars"))
            return Probe.ReadVariables(args.Skip(Array.IndexOf(args, "--probe-vars") + 1).ToArray());
        if (args.Contains("--try-variable"))
        {
            var i = Array.IndexOf(args, "--try-variable");
            if (i + 2 >= args.Length || !double.TryParse(args[i + 2],
                System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var variableValue))
            { Console.Error.WriteLine("usage: --try-variable L:NAME VALUE"); return 1; }
            return Probe.TryVariable(args[i + 1], variableValue);
        }
        if (args.Contains("--try-input-event"))
        {
            var i = Array.IndexOf(args, "--try-input-event");
            var name = i + 1 < args.Length ? args[i + 1] : "";
            var d = Array.IndexOf(args, "--data");
            var value = d >= 0 && d + 1 < args.Length
                && double.TryParse(args[d + 1], System.Globalization.NumberStyles.Float,
                    System.Globalization.CultureInfo.InvariantCulture, out var parsed) ? parsed : 0;
            if (name.Length == 0) { Console.Error.WriteLine("usage: --try-input-event NAME [--data NUMBER]"); return 1; }
            return Probe.TryInputEvent(name, value);
        }
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
        // Both sample on their own threads and are independent of SimConnect:
        // they must keep answering with MSFS closed, the same way MEDIA and
        // SCREENS do, because neither has anything to do with the simulator.
        var gpu = new GpuBridge();
        var audio = new AudioBridge();
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
            Metrics = gpu.Exposition,
            Audio = () =>
            {
                var (active, bands, peak, why) = audio.Snapshot();
                return new JsonObject
                {
                    ["active"] = active,
                    ["peak"] = Math.Round(peak, 4),
                    ["bands"] = new JsonArray(bands
                        .Select(b => (JsonNode)Math.Round(b, 4)).ToArray()),
                    // Empty while capturing. The panel shows a flat spectrum
                    // for silence and this line for no capture session at all,
                    // which are different facts and look identical otherwise.
                    ["reason"] = why.Length == 0 ? null : why,
                };
            },
        };

        // The control link is the safety-critical surface. Start it before
        // optional telemetry pumps: a slow audio device, media framework, or
        // GPU driver must never make the panel look as if the flight agent is
        // absent.
        _api.Start();
        _sim.Start();
        gpu.Start();
        audio.Start();
        Console.WriteLine($"flightdeck-sim-agent {Version} listening on :{Port}");

        using var stop = new ManualResetEventSlim(false);
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Set(); };
        stop.Wait();

        _sim.Dispose();
        _api.Dispose();
        audio.Dispose();
        gpu.Dispose();
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
    private static void OnStateReceived(SimStateRaw state, SimCapsRaw caps, SimGpsRaw gps, string aircraft,
        IReadOnlyList<FlightPlanWaypoint> flightPlan, string flightPlanSource)
    {
        lock (PublishLock)
        {
            var due = DateTime.UtcNow - _lastPublishedAt >= ReadoutInterval;
            var moved = _lastPublishedAt == DateTime.MinValue || Normalize.ControlsDiffer(state, _lastPublished);
            if (!due && !moved) return;

            var ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            var snapshot = Normalize.State(++_seq, ts, aircraft, state, caps, gps,
                flightPlan, flightPlanSource);
            _sim.DescribeAircraftState(snapshot);

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

        var profile = AircraftProfiles.For(_sim.Aircraft);
        if (_sim.IsIniA330 && control is "ap_hdg" or "ap_master" && !_sim.HaveIniA330)
            return Reject("waiting for A330 avionics status");
        if (control.StartsWith("ap_", StringComparison.Ordinal)
            && !AircraftProfiles.IsDirectlySupported(profile, control, action))
        {
            return Reject(profile.CockpitBridgeNote ??
                $"{profile.Label}: this control needs the cockpit bridge");
        }

        var resolved = Resolve(control, action, value, state, caps);
        if (resolved.Reason is not null) return Reject(resolved.Reason);

        // Already where it was asked to be. Nothing is transmitted, and `noop`
        // tells the panel not to wait for movement that will never come.
        if (resolved.Event is null && resolved.InputEvent is null && resolved.Variable is null)
        {
            return new JsonObject
            {
                ["cmd_id"] = cmdId,
                ["accepted"] = true,
                ["noop"] = true,
                ["seq"] = Interlocked.Read(ref _seq),
            };
        }

        var sent = TransmitResolvedAsync(resolved);
        var done = await Task.WhenAny(sent, Task.Delay(TimeSpan.FromMilliseconds(500)));
        if (done != sent)
        {
            // A multi-detent FCU movement is deliberately paced so the loaded
            // avionics can see every click. The queue owns the remainder; it
            // is already a valid SimConnect command, so report it as accepted
            // and let the observed target remain the confirmation.
            if (resolved.InputEvent is not null && resolved.InputRepeat > 1)
            {
                return new JsonObject
                {
                    ["cmd_id"] = cmdId,
                    ["accepted"] = true,
                    ["queued"] = true,
                    ["seq"] = Interlocked.Read(ref _seq),
                };
            }
            return Reject("agent busy");
        }
        if (!await sent) return Reject("sim not connected");

        return new JsonObject
        {
            ["cmd_id"] = cmdId,
            ["accepted"] = true,
            ["seq"] = Interlocked.Read(ref _seq),
        };
    }

    private static async Task<bool> TransmitResolvedAsync(Resolved resolved)
    {
        if (resolved.PreEvent is not null
            && !await _sim.TransmitAsync(resolved.PreEvent.Value)) return false;
        var sent = resolved.Variable is not null
            ? await _sim.WriteIniVariableAsync(resolved.Variable, resolved.InputValue)
            : resolved.InputEvent is not null
            ? await _sim.TransmitInputAsync(resolved.InputEvent, resolved.InputValue, resolved.InputRepeat)
            : await _sim.TransmitAsync(resolved.Event!.Value, resolved.Data0, resolved.Data1);
        if (!sent) return false;
        return resolved.FollowupEvent is null
            || await _sim.TransmitAsync(resolved.FollowupEvent.Value, resolved.FollowupData);
    }

    private readonly record struct Resolved(
        Event? Event, uint Data0, uint Data1, string? Reason,
        Event? FollowupEvent = null, uint FollowupData = 0, Event? PreEvent = null,
        string? InputEvent = null, double InputValue = 0, int InputRepeat = 1, string? Variable = null);

    private static Resolved Invalid(string reason) => new(null, 0, 0, reason);
    private static Resolved Send(Event e, uint data0 = 0, uint data1 = 0,
        Event? followupEvent = null, uint followupData = 0, Event? preEvent = null) =>
        new(e, data0, data1, null, followupEvent, followupData, preEvent);
    private static Resolved SendInput(string name, double value = 0, int repeat = 1) =>
        new(null, 0, 0, null, InputEvent: name, InputValue: value, InputRepeat: repeat);
    private static Resolved WriteIni(string name, double value) =>
        new(null, 0, 0, null, Variable: name, InputValue: value);
    private static Resolved AirlinerInput(string name, double value, Resolved fallback) =>
        _sim?.HasInputEvent(name) == true ? SendInput(name, value) : fallback;
    private static readonly Resolved Noop = new(null, 0, 0, null);

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

        if (_sim?.IsIniA330 == true)
        {
            if (control == "ap_hdg")
            {
                if (action == "mode") return value switch
                {
                    "on" => WriteIni("L:INI_FCU_SELECTED_HEADING_BUTTON", 1),
                    "off" => WriteIni("L:INI_FCU_MANAGED_HEADING_BUTTON", 1),
                    _ => Invalid("invalid value"),
                };
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var target)) return Invalid("invalid value");
                // Absolute aircraft dial: repeated detents accelerate and can
                // overshoot. Both this dial and the sim heading bug read back
                // the exact value after a direct write (verified in flight).
                return WriteIni("L:INI_HEADING_DIAL", ((target % 360) + 360) % 360);
            }
            if (control == "ap_master")
            {
                if (action == "toggle") return Invalid("use explicit engage or off");
                if (action != "set") return Invalid("unsupported action");
                return value switch
                {
                    "engaged" => On(s.AutopilotMaster) ? Noop : WriteIni("L:INI_AP1_BUTTON", 1),
                    "off" => On(s.AutopilotMaster) ? Send(Event.AutopilotOff) : Noop,
                    _ => Invalid("invalid value"),
                };
            }
        }

        // Engage or disengage an autopilot mode. Explicit events, guarded
        // against the state already being right so a redundant tap transmits
        // nothing and the panel is told not to wait for movement.
        Resolved Mode(string? want, double observed, Event engage, Event disengage) => want switch
        {
            "on" => On(observed) ? Noop : Send(engage),
            "off" => On(observed) ? Send(disengage) : Noop,
            _ => Invalid("invalid value"),
        };

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
                // incr/decr are kept resolvable but the panel no longer uses
                // them: they are relative, so nothing can confirm what was
                // asked for, and airframes that route the flap handle through
                // their own systems ignore both. `set` is the path now.
                if (action == "incr") return Send(Event.FlapsIncr);
                if (action == "decr") return Send(Event.FlapsDecr);
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var index)) return Invalid("invalid value");
                var detents = Normalize.Detents(c.FlapsNumHandlePositions);
                if (index < 0 || index >= Math.Max(detents, 1)) return Invalid("invalid value");
                // FLAPS_SET does NOT take a detent index, whatever the shape of
                // the number suggests. The SDK defines its parameter as a
                // handle POSITION from 0 to 16383 — "sets flaps handle to
                // closest increment" — so handing it 0..3 asks for 0.02% of
                // full travel and the handle rounds back to UP every time.
                // Scaling the index across the detents is also what keeps this
                // right for a Cub with two positions and an airliner with six.
                var span = Math.Max(detents - 1, 1);
                return Send(Event.FlapsSet, (uint)Math.Round(index * 16383.0 / span));

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
                    // The Airbus AP buttons are not legacy AP_MASTER events.
                    // AIRLINER_AP1_PUSH is the same control the flight deck
                    // exposes; use it when this airframe publishes it and
                    // retain the generic event for conventional aircraft.
                    "engaged" => On(s.AutopilotMaster) ? Noop
                        : AirlinerInput("AIRLINER_AP1_PUSH", 0, Send(Event.ApMaster)),
                    "off" => On(s.AutopilotMaster)
                        ? AirlinerInput("AIRLINER_AP1_PUSH", 0, Send(Event.ApMaster)) : Noop,
                    _ => Invalid("invalid value"),
                };

            case "ap_hdg":
                if (action == "mode")
                {
                    if (_sim?.HasInputEvent("AIRLINER_MCU_HDG_PULL") == true)
                    {
                        if (value == "on") return On(s.ApHdgLock) ? Noop : SendInput("AIRLINER_MCU_HDG_PULL");
                        if (value == "off") return On(s.ApHdgLock) ? SendInput("AIRLINER_MCU_HDG_PUSH") : Noop;
                        return Invalid("invalid value");
                    }
                    // Not differ-guarded like the toggles: stock Boeings report
                    // HEADING LOCK while LNAV owns the roll, so the guard read
                    // real taps as already-done and transmitted nothing. These
                    // events are explicit and idempotent — send regardless, and
                    // point the heading slot back at the selected bug, which is
                    // where airliner MCPs park it while a managed mode flies.
                    return value switch
                    {
                        "on" => Send(Event.ApHdgHoldOn,
                            followupEvent: Event.HeadingSlotSet, followupData: 1),
                        "off" => Send(Event.ApHdgHoldOff),
                        _ => Invalid("invalid value"),
                    };
                }
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var hdg)) return Invalid("invalid value");
                var airlinerHdg = ((hdg % 360) + 360) % 360;
                if (_sim?.HasInputEvent("AIRLINER_MCU_HDG") == true)
                {
                    // The A330's input is an FCU detent, not an absolute
                    // heading. Positive/negative values turn it one degree
                    // each; choose the shorter way around the compass and
                    // pace the required detents on the SimConnect pump.
                    var current = ((int)Math.Round(s.ApHdgDeg) % 360 + 360) % 360;
                    var clockwise = (airlinerHdg - current + 360) % 360;
                    var counterClockwise = (current - airlinerHdg + 360) % 360;
                    if (clockwise == 0) return Noop;
                    return SendInput("AIRLINER_MCU_HDG",
                        clockwise <= counterClockwise ? 1 : -1,
                        Math.Min(clockwise, counterClockwise));
                }
                return Send(Event.HeadingBugSet, (uint)airlinerHdg);

            case "ap_alt":
                if (action == "mode")
                {
                    if (_sim?.HasInputEvent("AIRLINER_MCU_ALT_PULL") == true)
                    {
                        if (value == "on") return On(s.ApAltLock) ? Noop : SendInput("AIRLINER_MCU_ALT_PULL");
                        if (value == "off") return On(s.ApAltLock) ? SendInput("AIRLINER_MCU_ALT_PUSH") : Noop;
                        return Invalid("invalid value");
                    }
                    return Mode(value, s.ApAltLock, Event.ApAltHoldToggle, Event.ApAltHoldToggle);
                }
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var alt) || alt < 0 || alt > 60000) return Invalid("invalid value");
                // The event's second argument selects the altitude slot.  A
                // modern airliner may be tracking slots 1–3 instead of the
                // legacy default (0); writing only the first argument can then
                // be accepted by SimConnect while leaving the visible AP bug
                // untouched.
                var slot = (int)Math.Round(s.ApAltitudeSlotIndex);
                if (slot < 0 || slot > 3) slot = 0;
                if (_sim?.HasInputEvent("AIRLINER_MCU_ALT") == true)
                {
                    // The A330 FCU accepts a direction, not an altitude. Its
                    // altitude increment is 1,000 ft, confirmed live here;
                    // emit the exact count of knob steps for the absolute
                    // target the Flight Deck API received.
                    var delta = alt - (int)Math.Round(s.ApAltFt);
                    if (delta == 0) return Noop;
                    var steps = (int)Math.Round(Math.Abs(delta) / 1000.0,
                        MidpointRounding.AwayFromZero);
                    if (steps == 0) return Invalid("A330 altitude increments are 1000 ft");
                    return SendInput("AIRLINER_MCU_ALT", Math.Sign(delta), steps);
                }
                return Send(Event.ApAltVarSet, (uint)alt, (uint)slot);

            case "ap_vs":
                if (action == "mode")
                {
                    if (_sim?.HasInputEvent("AIRLINER_MCU_VS_PULL") == true)
                    {
                        if (value == "on") return On(s.ApVsHold) ? Noop : SendInput("AIRLINER_MCU_VS_PULL");
                        if (value == "off") return On(s.ApVsHold) ? SendInput("AIRLINER_MCU_VS_PUSH") : Noop;
                        return Invalid("invalid value");
                    }
                    if (value == "on" && !On(s.ApVsHold))
                        return Send(Event.ApVsHoldToggle, followupEvent: Event.ApVsVarSet,
                            followupData: unchecked((uint)Math.Round(s.ApVsFpm)),
                            preEvent: On(s.ApFlcActive) || On(s.ApIasHold) ? Event.ApFlcToggle
                                : On(s.ApAltLock) ? Event.ApAltHoldToggle : null);
                    return Mode(value, s.ApVsHold, Event.ApVsHoldToggle, Event.ApVsHoldToggle);
                }
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var vs) || Math.Abs(vs) > 8000) return Invalid("invalid value");
                // Negative climbs ride as two's complement — the sim reads the
                // event's DWORD back as signed.
                return AirlinerInput("AIRLINER_MCU_VS", vs,
                    Send(Event.ApVsVarSet, unchecked((uint)vs)));

            case "ap_spd":
                // Either flag counts as engaged, so switching FLC off when the
                // aircraft was actually in IAS hold still reads as a change.
                if (action == "mode")
                {
                    var active = On(s.ApFlcActive) || On(s.ApIasHold) || On(s.ApMachHold);
                    if (_sim?.HasInputEvent("AIRLINER_MCU_SPEED_PULL") == true)
                    {
                        if (value == "on") return active ? Noop : SendInput("AIRLINER_MCU_SPEED_PULL");
                        if (value == "off") return active ? SendInput("AIRLINER_MCU_SPEED_PUSH") : Noop;
                        return Invalid("invalid value");
                    }
                    if (value == "on" && !active)
                        return Send(Event.ApFlcToggle, followupEvent: Event.ApSpdVarSet,
                            followupData: (uint)Math.Max(0, Math.Round(s.ApSpdKt)),
                            preEvent: On(s.ApVsHold) ? Event.ApVsHoldToggle
                                : On(s.ApAltLock) ? Event.ApAltHoldToggle : null);
                    return Mode(value, active ? 1 : 0, Event.ApFlcToggle, Event.ApFlcToggle);
                }
                if (action == "ref")
                    return value switch
                    {
                        // Explicit and idempotent, same posture as ap_hdg.
                        "mach" => Send(Event.ApMachRefOn),
                        "kt" => Send(Event.ApMachRefOff),
                        // From observed state, so a dropped frame can only
                        // fail to change over — never invert it.
                        "toggle" => Send(On(s.ApSpeedIsMach) ? Event.ApMachRefOff : Event.ApMachRefOn),
                        _ => Invalid("invalid value"),
                    };
                if (action == "set_mach")
                {
                    if (!double.TryParse(value, System.Globalization.NumberStyles.Float,
                            System.Globalization.CultureInfo.InvariantCulture, out var machBug)
                        || machBug < 0.1 || machBug > 0.99) return Invalid("invalid value");
                    // AP_MACH_VAR_SET takes hundredths. Step-0: verify on the 747-8i.
                    return Send(Event.ApMachVarSet, (uint)Math.Round(machBug * 100));
                }
                if (action != "set") return Invalid("unsupported action");
                if (!int.TryParse(value, out var spd) || spd < 0 || spd > 900) return Invalid("invalid value");
                return AirlinerInput("AIRLINER_MCU_SPEED", spd, Send(Event.ApSpdVarSet, (uint)spd));

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
