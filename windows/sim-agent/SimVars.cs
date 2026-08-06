// SimVars.cs — the entire MSFS vocabulary, and the only file that holds it.
//
// Step 0 of docs/SIM-AGENT-BRIEF.md verifies every row below against the SDK's
// SimvarWatcher with the Beechcraft Baron G58 loaded. MSFS DevSupport carries
// live threads about GEAR POSITION not matching its own documentation, so these
// are a starting point, not gospel. When a name, unit or event id turns out to
// be wrong, this file is the only thing that changes: Normalize, HttpApi,
// deck-api and the panel never see a SimVar name, a unit string or an event id.
//
// OPEN STEP 0 QUESTIONS, flagged where they bite:
//   [Q1] RESOLVED, and the brief's variable lost. GEAR TOTAL PCT EXTENDED is
//        BROKEN in MSFS 2024: on a King Air parked on the runway with the gear
//        plainly down it read 1.0 Percent, 0.0100 Percent Over 100, and 0.010
//        as a raw Number — so its native value really is 1%, not a unit or
//        conversion artifact. Meanwhile GEAR LEFT / CENTER / RIGHT POSITION all
//        read a correct 100.0 Percent. This is the discrepancy the brief warned
//        about on MSFS DevSupport, met in the wild.
//        GEAR POSITION (Enum) read 1 with the gear down, which the docs call
//        "up", so it is unreliable too. GEAR ANIMATION POSITION read 76.2 and
//        is meaningless without a leg index. Both rejected.
//        Per-leg is also the better contract: "down" now means EVERY leg is
//        down, which a single total can never express. See Normalize.GearState.
//   [Q2] RESOLVED — PLANE HEADING DEGREES MAGNETIC as "Degrees" really is
//        degrees. Same dual-unit read gave 1.6699 vs 0.0291 radians, a ratio of
//        180/pi. Native units are radians, but SimConnect converts on request,
//        so the conversion happens at the agent boundary as required.
//   [Q3] FLAPS NUM HANDLE POSITIONS — the Baron G58 should read 3 (UP /
//        APPROACH / DOWN). If it reads 4 because UP is counted separately,
//        subtract one in Normalize.Detents, not here. STILL OPEN.
//   [Q3] PARKING_BRAKES is toggle-only in this list. The agent gets explicit
//        set/off semantics by comparing against observed state before
//        transmitting (SimBridge callers use the differ-guard in Program).
//        If SimvarWatcher confirms PARKING_BRAKE_SET accepts a 1/0 parameter,
//        add it below and switch parking brake to the explicit path.
//   [Q4] There is no capability SimVar for autothrottle on this airframe class.
//        capabilities.autothrottle is reported false and is not read from the
//        sim. Confirm or find the right variable before any autothrottle work.

using System.Runtime.InteropServices;

namespace FlightDeckSimAgent;

internal enum Definition { State = 0, Caps = 1, Title = 2 }

internal enum Request { State = 0, Caps = 1, Title = 2 }

internal enum Group { Main = 0 }

internal enum Event
{
    // System events the sim pushes at us.
    SimStart = 100,
    SimStop = 101,

    // Client events we transmit. Ids are arbitrary but must be distinct.
    GearUp = 200,
    GearDown,
    GearToggle,
    FlapsIncr,
    FlapsDecr,
    FlapsSet,
    ParkingBrakes,
    LandingLightsOn,
    LandingLightsOff,
    ApMaster,
}

// Field order MUST match the order of StateVars below: SimConnect fills the
// struct positionally, so a reordered row silently shifts every value after it.
[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct SimStateRaw
{
    public double GearHandlePosition;
    public double GearLeftPct;
    public double GearCenterPct;
    public double GearRightPct;
    public double FlapsHandleIndex;
    public double FlapsNumHandlePositions;
    public double TrailingEdgeFlapsLeftAngleDeg;
    public double BrakeParkingPosition;
    public double LightLanding;
    public double AutopilotMaster;
    public double AirspeedIndicatedKt;
    public double IndicatedAltitudeFt;
    public double PlaneHeadingMagneticDeg;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct SimCapsRaw
{
    public double IsGearRetractable;
    public double FlapsAvailable;
    public double FlapsNumHandlePositions;
    public double SpoilerAvailable;
    public double AutopilotAvailable;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct SimTitleRaw
{
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
    public string Title;
}

internal static class SimVars
{
    // PLANE HEADING DEGREES MAGNETIC is radians natively. We ask SimConnect for
    // "Degrees" so the conversion happens here, at the agent boundary, and never
    // in the UI — which is what the brief requires. Same for the flap angle.
    public static readonly (string Name, string Unit)[] StateVars =
    {
        ("GEAR HANDLE POSITION",           "Bool"),
        // Per-leg, NOT GEAR TOTAL PCT EXTENDED — see [Q1].
        ("GEAR LEFT POSITION",             "Percent"),
        ("GEAR CENTER POSITION",           "Percent"),
        ("GEAR RIGHT POSITION",            "Percent"),
        ("FLAPS HANDLE INDEX",             "Number"),
        ("FLAPS NUM HANDLE POSITIONS",     "Number"),           // [Q2]
        ("TRAILING EDGE FLAPS LEFT ANGLE", "Degrees"),          // radians natively
        ("BRAKE PARKING POSITION",         "Bool"),
        ("LIGHT LANDING",                  "Bool"),
        ("AUTOPILOT MASTER",               "Bool"),
        ("AIRSPEED INDICATED",             "Knots"),
        ("INDICATED ALTITUDE",             "Feet"),
        ("PLANE HEADING DEGREES MAGNETIC", "Degrees"),          // radians natively
    };

    public static readonly (string Name, string Unit)[] CapsVars =
    {
        ("IS GEAR RETRACTABLE",        "Bool"),
        ("FLAPS AVAILABLE",            "Bool"),
        ("FLAPS NUM HANDLE POSITIONS", "Number"),
        ("SPOILER AVAILABLE",          "Bool"),
        ("AUTOPILOT AVAILABLE",        "Bool"),
    };

    public const string TitleVar = "TITLE";

    public static readonly (Event Id, string Name)[] ClientEvents =
    {
        (Event.GearUp,           "GEAR_UP"),
        (Event.GearDown,         "GEAR_DOWN"),
        (Event.GearToggle,       "GEAR_TOGGLE"),
        (Event.FlapsIncr,        "FLAPS_INCR"),
        (Event.FlapsDecr,        "FLAPS_DECR"),
        (Event.FlapsSet,         "FLAPS_SET"),
        (Event.ParkingBrakes,    "PARKING_BRAKES"),             // [Q3] toggle-only
        (Event.LandingLightsOn,  "LANDING_LIGHTS_ON"),          // explicit, so a
        (Event.LandingLightsOff, "LANDING_LIGHTS_OFF"),         // dropped frame
        (Event.ApMaster,         "AP_MASTER"),                  // can't invert state
    };
}
