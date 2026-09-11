# iniBuilds A330 heading integration — 2026-09-11

Verified in the loaded MSFS 2024 `A330-200 (RR)`:

- Generic `AUTOPILOT HEADING LOCK` stayed 0 while the aircraft's `L:INI_ROLL_MODE` reported 2 (selected heading).
- `L:INI_FD1_ON` initially read 0 while the generic flight-director flag read 1. Setting the aircraft flight director on made the roll mode 2 and vertical mode 14. The integration does not automatically change the flight director.
- Writing `L:INI_HEADING_DIAL` to 282 made both the aircraft dial and generic heading bug read 282. Repeated input-event detents overshot, so this profile now uses the absolute dial write.
- `L:INI_FCU_SELECTED_HEADING_BUTTON` is a momentary command consumed by the aircraft. With the flight directors off, it captured current heading; selected targets and mode activation must be confirmed separately.
- `L:INI_AP1_BUTTON` was consumed, but `L:INI_ap1_on` and `L:INI_ap2_on` remained 0. **AP engagement is not verified.** Do not claim the aircraft is steering merely because heading mode is selected.
- Aircraft vertical mode 14 corresponds to V/S; the stock variables simultaneously reported altitude hold and V/S. The aircraft-specific readback removes that contradictory altitude-hold indication.

Bindings were cross-checked against the [Circuit Avionics A330 profile](https://circuitxl.co.uk/Profiles-Autopilot/MSFS-Microsoft---iniBuilds-A330-BelugaXL) and its downloadable configuration. Direct L-variable access is provided by [SimConnect data definitions](https://docs.flightsimulator.com/msfs2024/retail/programming-apis/simconnect/api-reference/events-and-data/simconnect_addtodatadefinition/).

Scope: exact stock A330-200/300 title family only; not Headwind or other third-party A330s. Other autopilot functions still need aircraft-specific validation. The remaining AP1 refusal needs cockpit annunciations and a comparison with a manual AP1 press, not more blind event retries.
