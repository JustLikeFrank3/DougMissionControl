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
        }

        return controls;
    }

    public static JsonObject Readouts(SimStateRaw s) => new()
    {
        ["ias_kt"] = (int)Math.Round(s.AirspeedIndicatedKt),
        ["alt_ft"] = (int)Math.Round(s.IndicatedAltitudeFt),
        ["hdg_mag"] = (int)Math.Round(Heading(s.PlaneHeadingMagneticDeg)) % 360,
    };

    public static JsonObject State(long seq, double ts, string aircraft, SimStateRaw s, SimCapsRaw c) => new()
    {
        ["ts"] = Math.Round(ts, 1),
        ["seq"] = seq,
        ["aircraft"] = aircraft,
        ["capabilities"] = Capabilities(c),
        ["controls"] = Controls(s, c),
        ["readouts"] = Readouts(s),
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
        || Flag(a.AutopilotMaster) != Flag(b.AutopilotMaster);
}
