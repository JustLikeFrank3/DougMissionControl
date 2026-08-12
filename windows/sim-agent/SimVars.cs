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
//   [Q3] RESOLVED — PARKING_BRAKE_SET does honour a 1/0 parameter. Confirmed by
//        experiment (--probe-events) on an A320neo: SET 1 drove BRAKE PARKING
//        POSITION to 1, SET 0 back to 0. Parking brake now uses the explicit
//        path rather than a toggle with a differ-guard.
//        The first attempt ran against a Bell 407 and appeared to show the
//        event dead — but the PARKING_BRAKES control could not move that brake
//        either, because a 407 has none. Hence the control in --probe-events.
//   [Q4] RESOLVED as unanswerable. There is no autothrottle CAPABILITY variable.
//        AUTOTHROTTLE ACTIVE, AUTOPILOT THROTTLE ARM and AUTOPILOT MANAGED
//        THROTTLE ACTIVE are all recognised by MSFS 2024 but all report present
//        engagement, not fitment, so each reads false on an aircraft that has an
//        autothrottle and is not using it. capabilities.autothrottle is
//        published as null — unknown — rather than a fabricated false.

using System.Runtime.InteropServices;

namespace FlightDeckSimAgent;

internal enum Definition { State = 0, Caps = 1, Title = 2, Gps = 3 }

internal enum FacilityDefinition { Airport = 100 }

internal enum Request { State = 0, Caps = 1, Title = 2, Gps = 3, FlightPlan = 4, Facility = 5 }

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
    ParkingBrakeSet,
    LandingLightsOn,
    LandingLightsOff,
    ApMaster,
    HeadingBugSet,
    ApAltVarSet,
    ApVsVarSet,
    ApSpdVarSet,
    ApHdgHoldOn,
    ApHdgHoldOff,
    ApAltHoldToggle,
    ApVsHoldToggle,
    ApFlcToggle,
    Com1StbySet,
    Com1Swap,
    Com2StbySet,
    Com2Swap,
    Nav1StbySet,
    Nav1Swap,
    Nav2StbySet,
    Nav2Swap,
    XpdrSet,
    XpdrIdent,
    BaroSet,
    HeadingSlotSet,
    ApMachVarSet,
    ApMachRefOn,
    ApMachRefOff,
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
    public double PlaneLatDeg;
    public double PlaneLonDeg;
    public double GroundSpeedKt;
    public double GpsTrueTrackDeg;
    public double EngRpm1;
    public double EngRpm2;
    public double VerticalSpeedFpm;
    public double FuelTotalGal;
    public double PlanePitchDeg;
    public double PlaneBankDeg;
    public double ApHdgDeg;
    public double ApAltFt;
    public double ApVsFpm;
    public double ApSpdKt;
    public double OnGround;
    public double Com1ActMHz;
    public double Com1SbyMHz;
    public double Com2ActMHz;
    public double Com2SbyMHz;
    public double Nav1ActMHz;
    public double Nav1SbyMHz;
    public double Nav2ActMHz;
    public double Nav2SbyMHz;
    public double XpdrCodeBcd;
    public double XpdrState;
    public double BaroInHg;
    // Throttle lever position per engine, 0-100. Singles report 0 on :2, the
    // same convention GENERAL ENG RPM uses, so the panel can tell a twin from
    // a single without being told which airframe it is looking at.
    public double Throttle1Pct;
    public double Throttle2Pct;
    public double Throttle3Pct;
    public double Throttle4Pct;
    // How many the airframe HAS. A shut-down engine reads 0 throttle, which is
    // indistinguishable from an engine that is not there — so the count is
    // asked for rather than inferred, and a feathered engine on a quad still
    // shows its lever at zero instead of vanishing.
    public double EngineCount;
    // Which autopilot MODES are engaged. A bug is only a target: the aircraft
    // does not chase it until the mode that reads it is on, which is why
    // setting AP SPD to 165 with no mode engaged leaves the throttle exactly
    // where it was. The panel needs these to say so rather than let a bug sit
    // there looking authoritative.
    public double ApHdgLock;
    public double ApAltLock;
    public double ApVsHold;
    public double ApFlcActive;
    public double ApIasHold;
    // MSFS can keep several altitude references.  The AP follows this slot,
    // so AP_ALT_VAR_SET_ENGLISH must receive it as its second parameter.
    public double ApAltitudeSlotIndex;
    // Built-in ATC's next assigned controller frequency. Zero when no handoff
    // is pending; published only after Normalize validates the COM band.
    public double AtcFutureAgentMHz;
    // Generic aural warnings exposed by SimConnect. These are observed warning
    // states, not limits inferred from a particular aircraft's handbook.
    public double OverspeedWarning;
    public double StallWarning;
    public double EngOnFire1;
    public double EngOnFire2;
    public double EngOnFire3;
    public double EngOnFire4;
    public double GearWarning;
    public double GearDamageBySpeed;
    public double GearSpeedExceeded;
    // Observed height above terrain for departure-phase detection. Keep this
    // at the tail: StateVars and SimStateRaw are positionally paired.
    public double PlaneAltitudeAboveGroundFt;
    // Quads. RPM:1/:2 predate these; :3/:4 read 0 on twins and singles.
    public double EngRpm3;
    public double EngRpm4;
    // Mach for the speed schedules — above the crossover a KIAS target decays.
    public double AirspeedMach;
    // Local magnetic variation (east positive), so a true bearing computed
    // from lat/lon can be shown against the magnetic compass rose.
    public double MagVarDeg;
    // The MCP speed changeover: which reference the window is flying, and
    // the mach bug itself — a separate bug from the knots one in the sim.
    public double ApMachVar;
    public double ApSpeedIsMach;
    // Mach hold is a third engaged flag — past the changeover the sim raises
    // this instead of AIRSPEED HOLD, and the panel read the mode as off.
    public double ApMachHold;
    // Boeing autothrottle. A 747 in SPD holds the bug with thrust and raises
    // NONE of the pitch-mode flags above — these are what say it is flying.
    // Probed 2026-08-11 on the Asobo 747-8i: its A/T raises NEITHER of these
    // either — it is model-side code invisible to SimConnect. They stay for
    // airframes that do report, and the panel words that honestly.
    public double ApThrottleArm;
    public double AutothrottleActive;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct SimCapsRaw
{
    public double IsGearRetractable;
    public double FlapsAvailable;
    public double FlapsNumHandlePositions;
    public double SpoilerAvailable;
    public double AutopilotAvailable;
    public double Com2Available;
    public double Nav1Available;
    public double Nav2Available;
    public double XpdrAvailable;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct SimTitleRaw
{
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
    public string Title;
}

// The GPS's view of the active flight plan. SimConnect exposes only the leg
// being flown — prev and next — plus index/count; the full route is not
// readable as SimVars. The panel accumulates legs as they sequence.
[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct SimGpsRaw
{
    public double FlightPlanActive;
    public double WpCount;
    public double WpIndex;
    public double NextLatDeg;
    public double NextLonDeg;
    public double PrevLatDeg;
    public double PrevLonDeg;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
    public string NextId;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
    public string PrevId;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct AirportFacilityRaw
{
    public double Latitude;
    public double Longitude;
    public double Altitude;
    public int RunwayCount;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct RunwayFacilityRaw
{
    public double Latitude;
    public double Longitude;
    public double Altitude;
    public float Heading;
    public float Length;
    public int PrimaryNumber;
    public int PrimaryDesignator;
    public int SecondaryNumber;
    public int SecondaryDesignator;
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
        // NAV surface. Appended at the END: SimConnect fills SimStateRaw
        // positionally, so insertion anywhere else shifts every later field.
        ("PLANE LATITUDE",                 "Degrees"),           // radians natively
        ("PLANE LONGITUDE",                "Degrees"),           // radians natively
        ("GROUND VELOCITY",                "Knots"),
        ("GPS GROUND TRUE TRACK",          "Degrees"),           // radians natively
        // SIM readouts, second batch. Appended at the END, same rule as above.
        // Not yet Step-0 verified against SimvarWatcher — flag here if wrong.
        ("GENERAL ENG RPM:1",              "Rpm"),
        ("GENERAL ENG RPM:2",              "Rpm"),               // 0 on singles
        ("VERTICAL SPEED",                 "feet/minute"),
        ("FUEL TOTAL QUANTITY",            "Gallons"),
        ("PLANE PITCH DEGREES",            "Degrees"),           // radians natively
        ("PLANE BANK DEGREES",             "Degrees"),           // radians natively
        // AP bug positions — commanded state, observed back from the sim.
        ("AUTOPILOT HEADING LOCK DIR",     "Degrees"),
        ("AUTOPILOT ALTITUDE LOCK VAR",    "Feet"),
        ("AUTOPILOT VERTICAL HOLD VAR",    "Feet/minute"),
        ("AUTOPILOT AIRSPEED HOLD VAR",    "Knots"),
        // Mission phase + comms drawer. BCO16 arrives as a double holding the
        // BCD value — decoded at the boundary in Normalize, never in the UI.
        ("SIM ON GROUND",                  "Bool"),
        ("COM ACTIVE FREQUENCY:1",         "MHz"),
        ("COM STANDBY FREQUENCY:1",        "MHz"),
        ("COM ACTIVE FREQUENCY:2",         "MHz"),
        ("COM STANDBY FREQUENCY:2",        "MHz"),
        ("NAV ACTIVE FREQUENCY:1",         "MHz"),
        ("NAV STANDBY FREQUENCY:1",        "MHz"),
        ("NAV ACTIVE FREQUENCY:2",         "MHz"),
        ("NAV STANDBY FREQUENCY:2",        "MHz"),
        ("TRANSPONDER CODE:1",             "BCO16"),
        ("TRANSPONDER STATE:1",            "Enum"),
        ("KOHLSMAN SETTING HG",            "inHg"),
        // Throttle levers. Appended at the END of THIS list, which is the one
        // paired with SimStateRaw — the pairing is positional, so a row added
        // to the wrong list lands in the wrong struct entirely.
        ("GENERAL ENG THROTTLE LEVER POSITION:1", "Percent"),
        ("GENERAL ENG THROTTLE LEVER POSITION:2", "Percent"),
        ("GENERAL ENG THROTTLE LEVER POSITION:3", "Percent"),
        ("GENERAL ENG THROTTLE LEVER POSITION:4", "Percent"),   // SimConnect caps at 4
        ("NUMBER OF ENGINES",                     "Number"),
        // AP mode flags, paired with the tail of SimStateRaw. Same END rule.
        ("AUTOPILOT HEADING LOCK",        "Bool"),
        ("AUTOPILOT ALTITUDE LOCK",       "Bool"),
        ("AUTOPILOT VERTICAL HOLD",       "Bool"),
        ("AUTOPILOT FLIGHT LEVEL CHANGE", "Bool"),
        ("AUTOPILOT AIRSPEED HOLD",       "Bool"),
        // Appended only: StateVars and SimStateRaw are positional.
        ("AUTOPILOT ALTITUDE SLOT INDEX",  "Number"),
        ("ATC FUTURE AGENT FREQUENCY",     "MHz"),
        ("OVERSPEED WARNING",              "Bool"),
        ("STALL WARNING",                  "Bool"),
        ("ENG ON FIRE:1",                  "Bool"),
        ("ENG ON FIRE:2",                  "Bool"),
        ("ENG ON FIRE:3",                  "Bool"),
        ("ENG ON FIRE:4",                  "Bool"),
        ("GEAR WARNING:1",                 "Enum"),
        ("GEAR DAMAGE BY SPEED",           "Bool"),
        ("GEAR SPEED EXCEEDED",            "Bool"),
        ("PLANE ALT ABOVE GROUND",          "Feet"),
        // Appended only — same positional rule as everything above.
        ("GENERAL ENG RPM:3",              "Rpm"),
        ("GENERAL ENG RPM:4",              "Rpm"),
        ("AIRSPEED MACH",                  "Mach"),
        ("MAGVAR",                         "Degrees"),
        ("AUTOPILOT MACH HOLD VAR",        "Number"),
        ("AUTOPILOT MANAGED SPEED IN MACH", "Bool"),
        ("AUTOPILOT MACH HOLD",            "Bool"),
        ("AUTOPILOT THROTTLE ARM",         "Bool"),
        // NOT "AUTOPILOT MANAGED SPEED" — probed NAME_UNRECOGNIZED in MSFS 2024.
        ("AUTOTHROTTLE ACTIVE",            "Bool"),
    };

    public static readonly (string Name, string Unit)[] CapsVars =
    {
        ("IS GEAR RETRACTABLE",        "Bool"),
        ("FLAPS AVAILABLE",            "Bool"),
        ("FLAPS NUM HANDLE POSITIONS", "Number"),
        ("SPOILER AVAILABLE",          "Bool"),
        ("AUTOPILOT AVAILABLE",        "Bool"),
        ("COM AVAILABLE:2",            "Bool"),
        ("NAV AVAILABLE:1",            "Bool"),
        ("NAV AVAILABLE:2",            "Bool"),
        ("TRANSPONDER AVAILABLE:1",    "Bool"),
    };

    public const string TitleVar = "TITLE";

    // Doubles first, strings after — same order as SimGpsRaw's fields, and the
    // strings are registered separately because their datum type differs.
    public static readonly (string Name, string Unit)[] GpsVars =
    {
        ("GPS IS ACTIVE FLIGHT PLAN",   "Bool"),
        ("GPS FLIGHT PLAN WP COUNT",    "Number"),
        ("GPS FLIGHT PLAN WP INDEX",    "Number"),
        ("GPS WP NEXT LAT",             "Degrees"),           // radians natively
        ("GPS WP NEXT LON",             "Degrees"),           // radians natively
        ("GPS WP PREV LAT",             "Degrees"),           // radians natively
        ("GPS WP PREV LON",             "Degrees"),           // radians natively
    };

    public static readonly string[] GpsStringVars =
    {
        "GPS WP NEXT ID",
        "GPS WP PREV ID",
    };

    public static readonly (Event Id, string Name)[] ClientEvents =
    {
        (Event.GearUp,           "GEAR_UP"),
        (Event.GearDown,         "GEAR_DOWN"),
        (Event.GearToggle,       "GEAR_TOGGLE"),
        (Event.FlapsIncr,        "FLAPS_INCR"),
        (Event.FlapsDecr,        "FLAPS_DECR"),
        (Event.FlapsSet,         "FLAPS_SET"),
        (Event.ParkingBrakes,    "PARKING_BRAKES"),             // toggle, still mapped
        (Event.ParkingBrakeSet,  "PARKING_BRAKE_SET"),          // [Q3] takes 1/0
        (Event.LandingLightsOn,  "LANDING_LIGHTS_ON"),          // explicit, so a
        (Event.LandingLightsOff, "LANDING_LIGHTS_OFF"),         // dropped frame
        (Event.ApMaster,         "AP_MASTER"),                  // can't invert state
        (Event.HeadingBugSet,    "HEADING_BUG_SET"),            // takes degrees
        (Event.ApAltVarSet,      "AP_ALT_VAR_SET_ENGLISH"),     // takes feet
        (Event.ApVsVarSet,       "AP_VS_VAR_SET_ENGLISH"),      // signed fpm
        (Event.ApSpdVarSet,      "AP_SPD_VAR_SET"),             // takes knots
        // Mode engagement. These are toggles because they are the events the
        // SDK cockpit templates send and the Working Title CJ4 responds to.
        // Resolve guards them against observed state, preserving explicit
        // on/off behavior at the Flight Deck boundary.
        (Event.ApHdgHoldOn,      "AP_HDG_HOLD_ON"),
        (Event.ApHdgHoldOff,     "AP_HDG_HOLD_OFF"),
        // Boeings park the heading reference in a slot the way the altitude
        // events do — HDG SEL only reads the bug once the slot points at it.
        (Event.HeadingSlotSet,   "HEADING_SLOT_INDEX_SET"),
        (Event.ApAltHoldToggle,  "AP_ALT_HOLD"),
        (Event.ApVsHoldToggle,   "AP_PANEL_VS_HOLD"),
        // Speed is flown by FLC on everything modern. On an airframe with no
        // autothrottle that means PITCH, not throttle: the aeroplane trades
        // altitude for the speed you asked for.
        (Event.ApFlcToggle,      "FLIGHT_LEVEL_CHANGE"),
        // The MCP speed changeover and the mach bug it reveals.
        (Event.ApMachVarSet,     "AP_MACH_VAR_SET"),
        (Event.ApMachRefOn,      "AP_MANAGED_SPEED_IN_MACH_ON"),
        (Event.ApMachRefOff,     "AP_MANAGED_SPEED_IN_MACH_OFF"),
        // Radios take Hz — the _HZ family avoids the BCD encoding entirely.
        (Event.Com1StbySet,      "COM_STBY_RADIO_SET_HZ"),
        (Event.Com1Swap,         "COM_STBY_RADIO_SWAP"),
        (Event.Com2StbySet,      "COM2_STBY_RADIO_SET_HZ"),
        (Event.Com2Swap,         "COM2_RADIO_SWAP"),
        (Event.Nav1StbySet,      "NAV1_STBY_SET_HZ"),
        (Event.Nav1Swap,         "NAV1_RADIO_SWAP"),
        (Event.Nav2StbySet,      "NAV2_STBY_SET_HZ"),
        (Event.Nav2Swap,         "NAV2_RADIO_SWAP"),
        (Event.XpdrSet,          "XPNDR_SET"),                  // BCD16 code
        (Event.XpdrIdent,        "XPNDR_IDENT_ON"),
        (Event.BaroSet,          "KOHLSMAN_SET"),               // millibars * 16
    };
}
