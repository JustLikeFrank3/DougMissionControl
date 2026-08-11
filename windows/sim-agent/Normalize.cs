// Normalize.cs — raw SimConnect values in, Flight Deck state out.
//
// This is the membrane. Above it: MSFS names, units and event ids (SimVars.cs).
// Below it: the shape in docs/SIM-AGENT-BRIEF.md and nothing else. If a SimVar
// name ever appears in a JSON key produced here, the membrane has leaked.
//
// A control the aircraft does not have is ABSENT from `controls`, not present
// and false — that is what lets the panel grey the button with a reason instead
// of rendering a dead one.

using System.Text.Json.Nodes;

namespace FlightDeckSimAgent;

internal static class Normalize
{
    // Gear percent thresholds. Anything strictly between them is transit, which
    // is the single most important value this agent produces.
    private const double DownPct = 99.5;
    private const double UpPct = 0.5;

    /// <summary>
    /// Published gear percent: the LEAST extended leg. Conservative on purpose —
    /// the panel should show the gear as still moving until the slowest leg has
    /// finished, never the other way round.
    /// </summary>
    public static double GearPct(SimStateRaw s) =>
        Math.Min(s.GearLeftPct, Math.Min(s.GearCenterPct, s.GearRightPct));

    /// <summary>
    /// Gear state from the three leg positions. Down requires EVERY leg down,
    /// up requires every leg up, and anything else is transit — including the
    /// asymmetric case a single total percentage could never represent.
    ///
    /// See [Q1] in SimVars.cs for why the brief's GEAR TOTAL PCT EXTENDED is
    /// not used: it reports ~1% on an aircraft whose gear is fully down.
    ///
    /// Caveat worth re-probing per airframe: this assumes all three variables
    /// are meaningful. Should an aircraft with no centre gear report a constant
    /// 0 there, this reads transit forever — visibly wrong, and safe, rather
    /// than a confident lie. Verified correct on the Beechcraft King Air.
    /// </summary>
    public static string GearState(SimStateRaw s)
    {
        var min = GearPct(s);
        var max = Math.Max(s.GearLeftPct, Math.Max(s.GearCenterPct, s.GearRightPct));
        return min >= DownPct ? "down" : max <= UpPct ? "up" : "transit";
    }

    /// <summary>Single-percentage form, for the probe's candidate comparison.</summary>
    public static string GearStateFromPct(double pct) =>
        pct >= DownPct ? "down" : pct <= UpPct ? "up" : "transit";

    /// <summary>
    /// Number of flap handle positions, counting the clean position.
    ///
    /// FLAPS NUM HANDLE POSITIONS reports the HIGHEST INDEX, not the count, so
    /// this adds one. Observed directly on an A320neo: the variable read 4
    /// while FLAPS_INCR walked the handle 0,1,2,3,4 and stopped — five
    /// positions. A King Air reads 2 for UP/APPROACH/DOWN, and the brief
    /// expects 3 detents on a Baron that reports 2. All three agree.
    ///
    /// Left raw this understates every aircraft by one, which also made
    /// Resolve reject the last detent as an invalid value.
    /// </summary>
    public static int Detents(double numHandlePositions) =>
        (int)Math.Max(0, Math.Round(numHandlePositions)) + 1;

    public static double Heading(double deg)
    {
        var h = deg % 360.0;
        return h < 0 ? h + 360.0 : h;
    }

    private static bool Flag(double v) => v > 0.5;

    public static JsonObject Capabilities(SimCapsRaw c) => new()
    {
        ["gear_retractable"] = Flag(c.IsGearRetractable),
        ["flap_detents"] = Flag(c.FlapsAvailable) ? Detents(c.FlapsNumHandlePositions) : 0,
        ["autopilot"] = Flag(c.AutopilotAvailable),
        // [Q4] RESOLVED as unanswerable: MSFS 2024 exposes no capability
        // variable for autothrottle. AUTOTHROTTLE ACTIVE, AUTOPILOT THROTTLE
        // ARM and AUTOPILOT MANAGED THROTTLE ACTIVE are all recognised but all
        // describe present engagement, not fitment — each reads false on an
        // aircraft that has one and simply is not using it. null says unknown
        // rather than asserting absence, and stays falsy for any consumer that
        // gates a control on it.
        ["autothrottle"] = null,
        ["speedbrake"] = Flag(c.SpoilerAvailable),
    };

    /// <summary>
    /// Which controls this airframe actually exposes. Parking brake and landing
    /// lights are assumed universal; the other three are gated on capabilities.
    /// </summary>
    public static JsonObject Controls(SimStateRaw s, SimCapsRaw c)
    {
        var controls = new JsonObject();

        if (Flag(c.IsGearRetractable))
        {
            controls["gear"] = new JsonObject
            {
                ["state"] = GearState(s),
                ["pct"] = Math.Round(GearPct(s), 1),
                ["handle"] = Flag(s.GearHandlePosition) ? "down" : "up",
            };
        }

        if (Flag(c.FlapsAvailable))
        {
            controls["flaps"] = new JsonObject
            {
                ["index"] = (int)Math.Round(s.FlapsHandleIndex),
                ["detents"] = Detents(s.FlapsNumHandlePositions),
                ["angle_deg"] = Math.Round(s.TrailingEdgeFlapsLeftAngleDeg, 1),
            };
        }

        controls["parking_brake"] = new JsonObject
        {
            ["state"] = Flag(s.BrakeParkingPosition) ? "set" : "off",
        };

        controls["landing_lights"] = new JsonObject
        {
            ["state"] = Flag(s.LightLanding) ? "on" : "off",
        };

        if (Flag(c.AutopilotAvailable))
        {
            controls["ap_master"] = new JsonObject
            {
                ["state"] = Flag(s.AutopilotMaster) ? "engaged" : "off",
            };
            // One entry per bug: the command gate is ContainsKey(control), so
            // each settable thing must be its own key.
            //
            // `mode` is the half that was missing. A bug is a target and
            // nothing more — the aeroplane only chases it once the mode that
            // reads it is engaged, so a panel that shows the number without
            // the mode is showing half the autopilot and looking broken for
            // the other half. Speed reads either flag: FLC is what modern
            // autopilots use, IAS hold is the older one, and both fly the
            // same bug.
            string Mode(double v) => Flag(v) ? "on" : "off";
            controls["ap_hdg"] = new JsonObject
                { ["deg"] = (int)Math.Round(Heading(s.ApHdgDeg)) % 360, ["mode"] = Mode(s.ApHdgLock) };
            controls["ap_alt"] = new JsonObject
                { ["ft"] = (int)Math.Round(s.ApAltFt), ["mode"] = Mode(s.ApAltLock) };
            controls["ap_vs"] = new JsonObject
                { ["fpm"] = (int)Math.Round(s.ApVsFpm), ["mode"] = Mode(s.ApVsHold) };
            controls["ap_spd"] = new JsonObject
                { ["kt"] = (int)Math.Round(s.ApSpdKt),
                  ["mach"] = Math.Round(s.ApMachVar, 3),
                  // Which reference the speed window is flying — the MCP
                  // changeover, observed, so the panel never guesses units.
                  ["ref"] = Flag(s.ApSpeedIsMach) ? "mach" : "kt",
                  ["mode"] = Flag(s.ApFlcActive) || Flag(s.ApIasHold) ? "on" : "off" };
        }

        // Comms. COM1 and the altimeter are assumed universal; everything else
        // is gated on what the airframe declares, so a Cub never shows a NAV2.
        JsonObject Radio(double act, double sby) => new()
        {
            ["act"] = Math.Round(act, 3),
            ["sby"] = Math.Round(sby, 3),
        };
        controls["com1"] = Radio(s.Com1ActMHz, s.Com1SbyMHz);
        if (Flag(c.Com2Available)) controls["com2"] = Radio(s.Com2ActMHz, s.Com2SbyMHz);
        if (Flag(c.Nav1Available)) controls["nav1"] = Radio(s.Nav1ActMHz, s.Nav1SbyMHz);
        if (Flag(c.Nav2Available)) controls["nav2"] = Radio(s.Nav2ActMHz, s.Nav2SbyMHz);
        if (Flag(c.XpdrAvailable))
        {
            controls["xpdr"] = new JsonObject
            {
                // BCO16: the double holds a BCD word — 0x1200 for squawk 1200.
                ["code"] = ((int)s.XpdrCodeBcd).ToString("X4"),
                ["mode"] = (int)s.XpdrState switch
                {
                    0 => "off", 1 => "stby", 2 => "test", 3 => "on", 4 => "alt",
                    _ => "unknown",
                },
            };
        }
        controls["baro"] = new JsonObject
        {
            ["inhg"] = Math.Round(s.BaroInHg, 2),
            ["hpa"] = (int)Math.Round(s.BaroInHg * 33.8639),
        };

        return controls;
    }

    public static JsonObject Readouts(SimStateRaw s) => new()
    {
        ["ias_kt"] = (int)Math.Round(s.AirspeedIndicatedKt),
        ["mach"] = Math.Round(s.AirspeedMach, 3),
        ["alt_ft"] = (int)Math.Round(s.IndicatedAltitudeFt),
        ["hdg_mag"] = (int)Math.Round(Heading(s.PlaneHeadingMagneticDeg)) % 360,
        // East positive. Lets the panel place a true bearing on the magnetic
        // rose without inventing a variation of its own.
        ["magvar_deg"] = Math.Round(s.MagVarDeg, 1),
        // NAV. 5 decimal places is ~1 m — plenty for a wall map, small enough
        // not to churn the JSON with sub-metre noise every frame.
        ["lat"] = Math.Round(s.PlaneLatDeg, 5),
        ["lon"] = Math.Round(s.PlaneLonDeg, 5),
        ["gs_kt"] = (int)Math.Round(s.GroundSpeedKt),
        // True track, not magnetic: bearings computed from lat/lon are true,
        // and mixing references on one instrument is how people get lost.
        ["trk_true"] = (int)Math.Round(Heading(s.GpsTrueTrackDeg)) % 360,
        ["vs_fpm"] = (int)Math.Round(s.VerticalSpeedFpm),
        ["agl_ft"] = Math.Max(0, (int)Math.Round(s.PlaneAltitudeAboveGroundFt)),
        ["rpm_1"] = (int)Math.Round(s.EngRpm1),
        ["rpm_2"] = (int)Math.Round(s.EngRpm2),
        // One entry per engine, same convention as throttles below — rpm_1/2
        // stay for panels that predate quads.
        ["rpms"] = new JsonArray(
            new[] { s.EngRpm1, s.EngRpm2, s.EngRpm3, s.EngRpm4 }
                .Take(Math.Clamp((int)Math.Round(s.EngineCount), 1, 4))
                .Select(v => (JsonNode)(int)Math.Round(v))
                .ToArray()),
        // One entry per engine the airframe has, in order. An array rather
        // than throttle_1..4 so the panel renders what it is given and a
        // single, a twin and a quad need no special cases anywhere.
        ["throttles"] = new JsonArray(
            new[] { s.Throttle1Pct, s.Throttle2Pct, s.Throttle3Pct, s.Throttle4Pct }
                .Take(Math.Clamp((int)Math.Round(s.EngineCount), 1, 4))
                .Select(v => (JsonNode)(int)Math.Round(v))
                .ToArray()),
        ["fuel_gal"] = Math.Round(s.FuelTotalGal, 1),
        // MSFS body-frame signs are positive nose-DOWN / bank-LEFT. The panel
        // draws standard EFIS signs (nose-up / right-wing-down positive), so
        // the flip happens here at the boundary. Step-0: verify on the Baron.
        ["pitch_deg"] = Math.Round(-s.PlanePitchDeg, 1),
        ["bank_deg"] = Math.Round(-s.PlaneBankDeg, 1),
        // Phase detection needs ground truth, not a guess from altitude.
        ["on_ground"] = Flag(s.OnGround),
        // Built-in ATC's actual handoff, not a frequency inferred from
        // geography. Invalid/idle values become null and never reach the cue.
        ["atc_next_mhz"] = s.AtcFutureAgentMHz is >= 118.0 and <= 136.99
            ? Math.Round(s.AtcFutureAgentMHz, 3) : null,
    };

    public static JsonObject Warnings(SimStateRaw s)
    {
        var fires = new[] { s.EngOnFire1, s.EngOnFire2, s.EngOnFire3, s.EngOnFire4 };
        var engineCount = Math.Clamp((int)Math.Round(s.EngineCount), 0, fires.Length);
        var fireEngines = Enumerable.Range(0, engineCount)
            .Where(i => Flag(fires[i]))
            .Select(i => (JsonNode)(i + 1))
            .ToArray();
        var gearWarning = (int)Math.Round(s.GearWarning) switch
        {
            1 => "gear_up",
            2 => "amphibious_gear_up",
            3 => "amphibious_gear_down",
            4 => "on_ground_handle_up",
            _ => null,
        };
        return new JsonObject
        {
            ["overspeed"] = Flag(s.OverspeedWarning),
            ["stall"] = Flag(s.StallWarning),
            ["engine_fire"] = new JsonArray(fireEngines),
            ["gear_warning"] = gearWarning,
            ["gear_damage"] = Flag(s.GearDamageBySpeed),
            ["gear_speed_exceeded"] = Flag(s.GearSpeedExceeded),
        };
    }

    /// <summary>
    /// The GPS's active leg. Null when no flight plan is running — absence,
    /// not zeros, so the panel never plots a waypoint at 0,0 off Ghana.
    /// </summary>
    public static JsonObject? Gps(SimGpsRaw g, IReadOnlyList<FlightPlanWaypoint>? flightPlan = null,
        string flightPlanSource = "")
    {
        if (!Flag(g.FlightPlanActive) || g.WpCount < 1) return null;
        JsonObject Wp(string id, double lat, double lon, int idx) => new()
        {
            ["id"] = string.IsNullOrWhiteSpace(id) ? null : id.Trim().ToUpperInvariant(),
            ["lat"] = Math.Round(lat, 5),
            ["lon"] = Math.Round(lon, 5),
            ["i"] = idx,
        };
        var i = (int)g.WpIndex;
        var gps = new JsonObject
        {
            ["count"] = (int)g.WpCount,
            ["index"] = i,
            ["prev"] = i >= 1 ? Wp(g.PrevId, g.PrevLatDeg, g.PrevLonDeg, i - 1) : null,
            ["next"] = Wp(g.NextId, g.NextLatDeg, g.NextLonDeg, i),
        };
        if (flightPlan is { Count: > 0 })
        {
            gps["plan"] = FlightPlan.Json(flightPlan);
            gps["plan_source"] = flightPlanSource;
        }
        return gps;
    }

    public static JsonObject State(long seq, double ts, string aircraft, SimStateRaw s, SimCapsRaw c,
        SimGpsRaw g, IReadOnlyList<FlightPlanWaypoint>? flightPlan = null,
        string flightPlanSource = "") => new()
    {
        ["ts"] = Math.Round(ts, 1),
        ["seq"] = seq,
        ["aircraft"] = aircraft,
        ["capabilities"] = Capabilities(c),
        ["controls"] = Controls(s, c),
        ["readouts"] = Readouts(s),
        ["warnings"] = Warnings(s),
        ["gps"] = Gps(g, flightPlan, flightPlanSource),
    };

    /// <summary>
    /// True when anything a human would see as a control position has moved.
    /// Readouts are deliberately excluded: they change every frame and would
    /// make "on change" meaningless.
    /// </summary>
    public static bool ControlsDiffer(SimStateRaw a, SimStateRaw b) =>
        GearState(a) != GearState(b)
        || Math.Abs(GearPct(a) - GearPct(b)) >= 0.5
        || Flag(a.GearHandlePosition) != Flag(b.GearHandlePosition)
        || (int)Math.Round(a.FlapsHandleIndex) != (int)Math.Round(b.FlapsHandleIndex)
        || Math.Abs(a.TrailingEdgeFlapsLeftAngleDeg - b.TrailingEdgeFlapsLeftAngleDeg) >= 0.5
        || Flag(a.BrakeParkingPosition) != Flag(b.BrakeParkingPosition)
        || Flag(a.LightLanding) != Flag(b.LightLanding)
        || Flag(a.AutopilotMaster) != Flag(b.AutopilotMaster)
        || Flag(a.OverspeedWarning) != Flag(b.OverspeedWarning)
        || Flag(a.StallWarning) != Flag(b.StallWarning)
        || Flag(a.EngOnFire1) != Flag(b.EngOnFire1)
        || Flag(a.EngOnFire2) != Flag(b.EngOnFire2)
        || Flag(a.EngOnFire3) != Flag(b.EngOnFire3)
        || Flag(a.EngOnFire4) != Flag(b.EngOnFire4)
        || (int)Math.Round(a.GearWarning) != (int)Math.Round(b.GearWarning)
        || Flag(a.GearDamageBySpeed) != Flag(b.GearDamageBySpeed)
        || Flag(a.GearSpeedExceeded) != Flag(b.GearSpeedExceeded)
        // Bug turns are control moves: publish them at once or the panel's
        // steppers feel a half-second behind the finger.
        || (int)Math.Round(a.ApHdgDeg) != (int)Math.Round(b.ApHdgDeg)
        || (int)Math.Round(a.ApAltFt) != (int)Math.Round(b.ApAltFt)
        || (int)Math.Round(a.ApVsFpm) != (int)Math.Round(b.ApVsFpm)
        || (int)Math.Round(a.ApSpdKt) != (int)Math.Round(b.ApSpdKt)
        || Flag(a.ApHdgLock) != Flag(b.ApHdgLock)
        || Flag(a.ApAltLock) != Flag(b.ApAltLock)
        || Flag(a.ApVsHold) != Flag(b.ApVsHold)
        || Flag(a.ApFlcActive) != Flag(b.ApFlcActive)
        || Flag(a.ApIasHold) != Flag(b.ApIasHold)
        // Same reasoning for the comms drawer — a swap must land on the next frame.
        || Math.Abs(a.Com1ActMHz - b.Com1ActMHz) >= 0.001
        || Math.Abs(a.Com1SbyMHz - b.Com1SbyMHz) >= 0.001
        || Math.Abs(a.Com2ActMHz - b.Com2ActMHz) >= 0.001
        || Math.Abs(a.Com2SbyMHz - b.Com2SbyMHz) >= 0.001
        || Math.Abs(a.Nav1ActMHz - b.Nav1ActMHz) >= 0.001
        || Math.Abs(a.Nav1SbyMHz - b.Nav1SbyMHz) >= 0.001
        || Math.Abs(a.Nav2ActMHz - b.Nav2ActMHz) >= 0.001
        || Math.Abs(a.Nav2SbyMHz - b.Nav2SbyMHz) >= 0.001
        || (int)a.XpdrCodeBcd != (int)b.XpdrCodeBcd
        || (int)a.XpdrState != (int)b.XpdrState
        || Math.Abs(a.BaroInHg - b.BaroInHg) >= 0.005;
}
