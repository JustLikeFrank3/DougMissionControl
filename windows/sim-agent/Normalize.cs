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
    /// GEAR TOTAL PCT EXTENDED as a true 0..100 percent.
    ///
    /// [Q1] RESOLVED against a live MSFS 2024 session: requesting "Percent" and
    /// "Percent Over 100" simultaneously returned 1.0000 and 0.0100 — exactly
    /// 100x apart — so SimConnect honours the unit and "Percent" really is
    /// 0..100. No scaling needed.
    ///
    /// This deliberately does NOT try to auto-detect the range. The earlier
    /// "if it is under 1.0 it must be a fraction" guard read a genuine 1.0%
    /// as 100% and published "down" for an almost fully retracted gear —
    /// precisely the lie about observed state this agent exists to prevent.
    /// </summary>
    public static double GearPct(double raw) => raw;

    public static string GearState(double pct) =>
        pct >= DownPct ? "down" : pct <= UpPct ? "up" : "transit";

    /// <summary>Flap detent count. See [Q2]: adjust here, not in SimVars.cs.</summary>
    public static int Detents(double numHandlePositions) =>
        (int)Math.Max(0, Math.Round(numHandlePositions));

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
        ["autothrottle"] = false,   // [Q4] not read from the sim; do not trust yet
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
            var pct = GearPct(s.GearTotalPctExtended);
            controls["gear"] = new JsonObject
            {
                ["state"] = GearState(pct),
                ["pct"] = Math.Round(pct, 1),
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
        GearState(GearPct(a.GearTotalPctExtended)) != GearState(GearPct(b.GearTotalPctExtended))
        || Math.Abs(GearPct(a.GearTotalPctExtended) - GearPct(b.GearTotalPctExtended)) >= 0.5
        || Flag(a.GearHandlePosition) != Flag(b.GearHandlePosition)
        || (int)Math.Round(a.FlapsHandleIndex) != (int)Math.Round(b.FlapsHandleIndex)
        || Math.Abs(a.TrailingEdgeFlapsLeftAngleDeg - b.TrailingEdgeFlapsLeftAngleDeg) >= 0.5
        || Flag(a.BrakeParkingPosition) != Flag(b.BrakeParkingPosition)
        || Flag(a.LightLanding) != Flag(b.LightLanding)
        || Flag(a.AutopilotMaster) != Flag(b.AutopilotMaster);
}
