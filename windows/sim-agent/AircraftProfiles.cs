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
    int AltitudeStepFt = 0,
    string? CockpitBridgeNote = null);

internal static class AircraftProfiles
{
    private static readonly AircraftProfile Generic = new(
        "generic", "Standard SimConnect", "simconnect", false);

    // Working Title CJ4 controls are designed to answer the standard events;
    // it is called out so a later WT-specific integration has one home.
    private static readonly AircraftProfile Cj4 = new(
        "cj4", "Working Title CJ4", "simconnect", false);

    // These aircraft currently use the standard control path. Keeping named
    // profiles means a real aircraft-specific integration can be introduced
    // later without asking the panel to guess from a model title.
    private static readonly AircraftProfile A320 = new(
        "a320", "Airbus A320 family", "simconnect", false);

    private static readonly AircraftProfile B747 = new(
        "747", "Boeing 747", "simconnect", false);

    private static readonly AircraftProfile B787 = new(
        "787", "Boeing 787", "simconnect", false);

    private static readonly AircraftProfile Longitude = new(
        "longitude", "Citation Longitude", "simconnect", false);

    private static readonly AircraftProfile Fighter = new(
        "fighter", "Military jet", "simconnect", false);

    // The A330's FCU knob inputs are published by the aircraft and verified
    // here. AP1/AP2 and other cockpit-model actions require the in-sim bridge
    // rather than a legacy external SimConnect event.
    private static readonly AircraftProfile A330 = new(
        "a330", "Airbus A330 FCU", "aircraft-input-events", true, 1000,
        "FCU knobs and mode buttons use aircraft inputs. AP1 needs the cockpit bridge.");

    public static AircraftProfile For(string? title)
    {
        var t = title ?? "";
        if (t.Contains("A330", StringComparison.OrdinalIgnoreCase)) return A330;
        if (t.Contains("A320", StringComparison.OrdinalIgnoreCase)
            || t.Contains("A321", StringComparison.OrdinalIgnoreCase)) return A320;
        if (t.Contains("CJ4", StringComparison.OrdinalIgnoreCase)
            || t.Contains("Citation CJ4", StringComparison.OrdinalIgnoreCase)) return Cj4;
        if (t.Contains("Longitude", StringComparison.OrdinalIgnoreCase)) return Longitude;
        if (t.Contains("747", StringComparison.OrdinalIgnoreCase)) return B747;
        if (t.Contains("787", StringComparison.OrdinalIgnoreCase)) return B787;
        if (t.Contains("F/A-18", StringComparison.OrdinalIgnoreCase)
            || t.Contains("F-18", StringComparison.OrdinalIgnoreCase)
            || t.Contains("F-35", StringComparison.OrdinalIgnoreCase)) return Fighter;
        return Generic;
    }

    /// <summary>
    /// A profile marked as bridge-backed must never quietly fall through to a
    /// legacy event for an action that we know its avionics own. That looks
    /// accepted at the HTTP layer but is the exact "no response" failure the
    /// panel is meant to expose.
    /// </summary>
    public static bool IsDirectlySupported(AircraftProfile profile, string control, string action)
    {
        if (profile.Id != "a330") return true;

        // The A330 publishes direct FCU input events for these knobs and
        // their pull/push modes. Leave them available to the deck instead of
        // rejecting an entire autopilot panel merely because AP1 itself needs
        // the in-cockpit bridge. AP master and the generic Mach changeover do
        // not have a verified external FCU path, so those remain explicit.
        return control is "ap_hdg" or "ap_alt" or "ap_vs"
            || (control == "ap_spd" && action is "set" or "mode");
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
            ["bridge_note"] = p.CockpitBridgeNote,
        };
    }
}
