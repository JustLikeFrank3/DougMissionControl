// AircraftProfiles.cs — aircraft identity belongs at the avionics boundary.
//
// MSFS exposes a common SimConnect surface, but "common" is not the same as
// "the controls in this cockpit".  A Baron or CJ4 can use the standard events;
// modern airliners often route their real MCP/FCU controls through their own
// avionics.  This registry makes that distinction explicit and gives the
// Flight Deck a stable profile identifier instead of scattering title checks
// through the UI and command resolver.

using System.Text.Json.Nodes;

namespace FlightDeckSimAgent;

internal sealed record AircraftProfile(
    string Id,
    string Label,
    string ControlPath,
    bool RequiresCockpitBridge,
    int AltitudeStepFt = 0);

internal static class AircraftProfiles
{
    private static readonly AircraftProfile Generic = new(
        "generic", "Standard SimConnect", "simconnect", false);

    // Working Title CJ4 controls are designed to answer the standard events;
    // it is called out so a later WT-specific integration has one home.
    private static readonly AircraftProfile Cj4 = new(
        "cj4", "Working Title CJ4", "simconnect", false);

    // The 747 uses the generic MCP events for the controls we expose today.
    private static readonly AircraftProfile B747 = new(
        "747", "Boeing 747", "simconnect", false);

    // The A330's FCU knob inputs are published by the aircraft and verified
    // here. AP1/AP2 and other cockpit-model actions require the in-sim bridge
    // rather than a legacy external SimConnect event.
    private static readonly AircraftProfile A330 = new(
        "a330", "Airbus A330 FCU", "aircraft-input-events", true, 1000);

    public static AircraftProfile For(string? title)
    {
        var t = title ?? "";
        if (t.Contains("A330", StringComparison.OrdinalIgnoreCase)) return A330;
        if (t.Contains("CJ4", StringComparison.OrdinalIgnoreCase)
            || t.Contains("Citation CJ4", StringComparison.OrdinalIgnoreCase)) return Cj4;
        if (t.Contains("747", StringComparison.OrdinalIgnoreCase)) return B747;
        return Generic;
    }

    public static JsonObject Describe(string? title)
    {
        var p = For(title);
        return new JsonObject
        {
            ["id"] = p.Id,
            ["label"] = p.Label,
            ["control_path"] = p.ControlPath,
            ["bridge_required"] = p.RequiresCockpitBridge,
            ["altitude_step_ft"] = p.AltitudeStepFt == 0 ? null : p.AltitudeStepFt,
        };
    }
}
