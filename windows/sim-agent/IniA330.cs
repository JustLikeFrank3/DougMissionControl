using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace FlightDeckSimAgent;

// iniBuilds A330 bindings, not interchangeable with Headwind/A32NX.
// Control/readback reference: Circuit Avionics' published A330 v1 profile:
// https://circuitxl.co.uk/profiles/autopilot/Autopilot-MSFS-iniBuilds-A330-v1.mcc
// Direct L: access is supported by the MSFS 2024 SimConnect data API.
[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct IniA330Raw
{
    public double Ap1, Ap2, RollMode, VerticalMode, Heading, Fd1, Fd2;
}

internal static class IniA330
{
    // These are the stock streamed-aircraft titles observed with this binding.
    // Do not match every third-party aircraft containing "A330".
    public static bool Matches(string? title) => title is not null
        && (title.StartsWith("A330-200 (", StringComparison.OrdinalIgnoreCase)
         || title.StartsWith("A330-300 (", StringComparison.OrdinalIgnoreCase));

    public static readonly string[] ReadVars =
    ["L:INI_ap1_on", "L:INI_ap2_on", "L:INI_ROLL_MODE", "L:FMGS_vertical_mode",
     "L:INI_HEADING_DIAL", "L:INI_FD1_ON", "L:INI_FD2_ON"];

    public static readonly string[] WriteVars =
    ["L:INI_HEADING_DIAL", "L:INI_FCU_SELECTED_HEADING_BUTTON",
     "L:INI_FCU_MANAGED_HEADING_BUTTON", "L:INI_AP1_BUTTON"];

    public static SimStateRaw Apply(SimStateRaw state, IniA330Raw observed)
    {
        state.AutopilotMaster = observed.Ap1 > .5 || observed.Ap2 > .5 ? 1 : 0;
        state.ApHdgDeg = observed.Heading;
        state.ApHdgLock = observed.RollMode == 2 ? 1 : 0;
        state.ApAltLock = observed.VerticalMode is 10 or 11 ? 1 : 0;
        state.ApVsHold = observed.VerticalMode is 14 or 15 ? 1 : 0;
        return state;
    }

    public static void Describe(JsonObject snapshot, IniA330Raw observed)
    {
        if (snapshot["controls"] is not JsonObject controls) return;
        if (controls["ap_hdg"] is JsonObject heading)
        {
            heading["mode_label"] = "HDG SELECTED";
            heading["source"] = "ini-a330";
            heading["flight_director"] = observed.Fd1 > .5 || observed.Fd2 > .5;
        }
        if (controls["ap_master"] is JsonObject master)
        {
            master["ap1"] = observed.Ap1 > .5;
            master["ap2"] = observed.Ap2 > .5;
            master["source"] = "ini-a330";
        }
    }
}
