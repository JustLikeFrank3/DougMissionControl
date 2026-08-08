/* Pins the sim agent's command resolution and its published state shape.

   Neither half can be checked by compiling. Resolve() turns Flight Deck
   vocabulary into a sim event and a DWORD, and every bug this file exists to
   catch has been a wrong number in a right-looking call: FLAPS_SET handed a
   detent index where the SDK wants a 0-16383 handle position, an AP mode
   transmitted when the aircraft was already in it, a heading that failed to
   wrap. All of those compile perfectly and do nothing in the simulator.

   Reflection rather than a project reference, because the agent targets
   net8.0-windows and this must run on the Linux boot too. Build the agent's
   stub first — tests/test_sim_agent.sh does that, and skips cleanly where
   there is no dotnet. */

using System;
using System.Linq;
using System.Reflection;
using System.Text.Json.Nodes;

var dll = args.Length > 0 ? args[0] : throw new ArgumentException("pass the agent dll");
var asm = Assembly.LoadFrom(dll);
var prog = asm.GetType("FlightDeckSimAgent.Program");
var norm = asm.GetType("FlightDeckSimAgent.Normalize");
var resolve = prog.GetMethod("Resolve", BindingFlags.NonPublic | BindingFlags.Static);
var controls = norm.GetMethod("Controls", BindingFlags.Public | BindingFlags.Static);
var stateT = asm.GetType("FlightDeckSimAgent.SimStateRaw");
var capsT = asm.GetType("FlightDeckSimAgent.SimCapsRaw");

object MakeState(params (string, double)[] set)
{
    var s = Activator.CreateInstance(stateT);
    foreach (var (f, v) in set) stateT.GetField(f).SetValue(s, v);
    return s;
}

object MakeCaps(params (string, double)[] set)
{
    var c = Activator.CreateInstance(capsT);
    foreach (var (f, v) in set) capsT.GetField(f).SetValue(c, v);
    return c;
}

/// <summary>The event and parameter a command resolves to, as a flat string.</summary>
string Call(string control, string action, string value, object s, object c)
{
    var r = resolve.Invoke(null, new object[] { control, action, value, s, c });
    var t = r.GetType();
    if (t.GetProperty("Reason").GetValue(r) is object reason) return $"REJECT({reason})";
    var ev = t.GetProperty("Event").GetValue(r);
    return ev is null ? "NOOP" : $"{ev} data={t.GetProperty("Data").GetValue(r)}";
}

int fails = 0;
void Expect(string label, string got, string want)
{
    var ok = got == want;
    if (!ok) fails++;
    Console.WriteLine($"  {(ok ? "ok  " : "FAIL")} {label,-44} {got}{(ok ? "" : "   want: " + want)}");
}

/* ── flaps: a detent index in, a handle POSITION out ────────────────────────
   The raw SimVar reports the highest index and not the count, so Detents()
   adds one back and the top of the travel is detents-1. Getting this wrong
   sends every flap command to a fraction of a percent of travel, where the
   handle rounds back to UP and the flaps appear not to work at all. */

object Caps(double rawHandlePositions) => MakeCaps(
    ("FlapsAvailable", 1), ("FlapsNumHandlePositions", rawHandlePositions),
    ("AutopilotAvailable", 1));
var baron = Caps(2);      // 3 positions: UP / APPROACH / DOWN
var cub = Caps(1);        // 2 positions: UP / DOWN
var airliner = Caps(4);   // 5 positions, as observed on an A320neo
var st = MakeState();

Expect("flaps 0 of 3 (Baron, clean)", Call("flaps", "set", "0", st, baron), "FlapsSet data=0");
Expect("flaps 1 of 3 (Baron, mid)", Call("flaps", "set", "1", st, baron), "FlapsSet data=8192");
Expect("flaps 2 of 3 (Baron, full)", Call("flaps", "set", "2", st, baron), "FlapsSet data=16383");
Expect("flaps 1 of 2 (Cub, full)", Call("flaps", "set", "1", st, cub), "FlapsSet data=16383");
Expect("flaps 1 of 5 (airliner)", Call("flaps", "set", "1", st, airliner), "FlapsSet data=4096");
Expect("flaps 4 of 5 (airliner, full)", Call("flaps", "set", "4", st, airliner), "FlapsSet data=16383");
Expect("flaps past the top detent", Call("flaps", "set", "3", st, baron), "REJECT(invalid value)");
Expect("flaps negative", Call("flaps", "set", "-1", st, baron), "REJECT(invalid value)");
Expect("flaps set with no number", Call("flaps", "set", "two", st, baron), "REJECT(invalid value)");
// The relative path stays resolvable for anything that still speaks it.
Expect("flaps incr still resolves", Call("flaps", "incr", null, st, baron), "FlapsIncr data=0");

/* ── AP modes ───────────────────────────────────────────────────────────────
   Explicit on/off, guarded against the state already being right: a redundant
   tap must transmit NOTHING, because these are the events that turn a bug from
   a number on a panel into an aeroplane that follows it, and a toggle that got
   through twice would leave the mode inverted. */

var apOff = MakeState();
var apOn = MakeState(("ApHdgLock", 1), ("ApAltLock", 1), ("ApVsHold", 1), ("ApFlcActive", 1));
var iasOnly = MakeState(("ApIasHold", 1));

Expect("ap_hdg mode on while off", Call("ap_hdg", "mode", "on", apOff, baron), "ApHdgHoldOn data=0");
Expect("ap_hdg mode on while on", Call("ap_hdg", "mode", "on", apOn, baron), "NOOP");
Expect("ap_hdg mode off while on", Call("ap_hdg", "mode", "off", apOn, baron), "ApHdgHoldOff data=0");
Expect("ap_hdg mode off while off", Call("ap_hdg", "mode", "off", apOff, baron), "NOOP");
Expect("ap_alt mode on", Call("ap_alt", "mode", "on", apOff, baron), "ApAltHoldOn data=0");
Expect("ap_vs mode on", Call("ap_vs", "mode", "on", apOff, baron), "ApVsHoldOn data=0");
Expect("ap_spd mode on (FLC)", Call("ap_spd", "mode", "on", apOff, baron), "ApFlcOn data=0");
// IAS hold and FLC fly the same bug, so an aircraft already in IAS hold is
// already flying it — engaging FLC on top would be a second mode, not a fix.
Expect("ap_spd mode on under IAS hold", Call("ap_spd", "mode", "on", iasOnly, baron), "NOOP");
Expect("ap_spd mode off under IAS hold", Call("ap_spd", "mode", "off", iasOnly, baron), "ApFlcOff data=0");
Expect("ap mode with a bad value", Call("ap_spd", "mode", "maybe", apOff, baron), "REJECT(invalid value)");

/* ── the bugs themselves ──────────────────────────────────────────────────── */

Expect("ap_spd set 165", Call("ap_spd", "set", "165", apOff, baron), "ApSpdVarSet data=165");
Expect("ap_hdg set wraps past 360", Call("ap_hdg", "set", "370", apOff, baron), "HeadingBugSet data=10");
Expect("ap_hdg set wraps negative", Call("ap_hdg", "set", "-10", apOff, baron), "HeadingBugSet data=350");
Expect("ap_alt above the ceiling", Call("ap_alt", "set", "70000", apOff, baron), "REJECT(invalid value)");
// A descent rides as two's complement — the sim reads the DWORD back signed.
Expect("ap_vs set descending", Call("ap_vs", "set", "-500", apOff, baron), $"ApVsVarSet data={unchecked((uint)-500)}");
Expect("ap_vs beyond the limit", Call("ap_vs", "set", "-9000", apOff, baron), "REJECT(invalid value)");
Expect("a control with no such action", Call("ap_spd", "toggle", null, apOff, baron), "REJECT(unsupported action)");

/* ── the published shape ────────────────────────────────────────────────────
   The panel keys off these names. A bug that arrives without its mode renders
   as a number nothing is chasing, which is the whole failure this pairing
   exists to prevent. */

string Mode(object s, string key)
{
    var c = (JsonObject)controls.Invoke(null, new[] { s, baron });
    return c[key]?["mode"]?.ToString() ?? "(absent)";
}

Expect("ap_hdg publishes its mode, off", Mode(apOff, "ap_hdg"), "off");
Expect("ap_hdg publishes its mode, on", Mode(apOn, "ap_hdg"), "on");
Expect("ap_alt publishes its mode", Mode(apOn, "ap_alt"), "on");
Expect("ap_vs publishes its mode", Mode(apOn, "ap_vs"), "on");
Expect("ap_spd reads FLC", Mode(apOn, "ap_spd"), "on");
Expect("ap_spd also reads IAS hold", Mode(iasOnly, "ap_spd"), "on");
Expect("ap_spd off when neither is set", Mode(apOff, "ap_spd"), "off");

// No autopilot, no bugs at all — the panel greys the tiles by their absence.
var noAp = (JsonObject)controls.Invoke(null, new[] { apOn, MakeCaps() });
Expect("no autopilot, no ap_spd key", noAp.ContainsKey("ap_spd") ? "present" : "absent", "absent");

/* ── the FFT behind the visualiser ──────────────────────────────────────────
   A wrong butterfly or a bad bit-reversal still produces a plausible-looking
   spectrum: bars that move with the music and mean nothing. The only way to
   catch that is to feed it a signal whose answer is known and check where the
   energy lands. */

var audioT = asm.GetType("FlightDeckSimAgent.AudioBridge");
var fft = audioT.GetMethod("Fft", BindingFlags.NonPublic | BindingFlags.Static);

int PeakBin(double freqHz, int rate = 48000, int n = 1024)
{
    var re = new double[n];
    var im = new double[n];
    for (var i = 0; i < n; i++) re[i] = Math.Sin(2 * Math.PI * freqHz * i / rate);
    fft.Invoke(null, new object[] { re, im });
    int best = 0;
    double bestMag = -1;
    for (var i = 1; i < n / 2; i++)   // skip DC
    {
        var mag = Math.Sqrt(re[i] * re[i] + im[i] * im[i]);
        if (mag > bestMag) { bestMag = mag; best = i; }
    }
    return best;
}

// bin = freq * N / rate. At 48kHz over 1024 points each bin is 46.875 Hz.
Expect("FFT puts 1 kHz in its bin", PeakBin(1000).ToString(), "21");
Expect("FFT puts 3 kHz in its bin", PeakBin(3000).ToString(), "64");
Expect("FFT puts 120 Hz in its bin", PeakBin(120).ToString(), "3");

// DC must land in bin 0 and nowhere else - a bit-reversal that is subtly wrong
// smears a constant across the spectrum, which on screen reads as "every bar
// half lit" and looks like a working visualiser.
{
    var n = 1024;
    var re = new double[n];
    var im = new double[n];
    for (var i = 0; i < n; i++) re[i] = 1.0;
    fft.Invoke(null, new object[] { re, im });
    var dc = Math.Sqrt(re[0] * re[0] + im[0] * im[0]);
    double spill = 0;
    for (var i = 1; i < n / 2; i++)
        spill = Math.Max(spill, Math.Sqrt(re[i] * re[i] + im[i] * im[i]));
    Expect("DC lands entirely in bin 0", $"{dc > 1000} {spill < 1e-6}", "True True");
}

// Silence in, silence out. A visualiser that draws bars from an empty buffer
// is the exact failure this whole capture path exists to avoid.
{
    var re = new double[1024];
    var im = new double[1024];
    fft.Invoke(null, new object[] { re, im });
    var any = re.Concat(im).Any(v => Math.Abs(v) > 1e-12);
    Expect("silence produces no spectrum", any ? "moved" : "flat", "flat");
}

/* ── band mapping ───────────────────────────────────────────────────────────
   The FFT was right and the display was still wrong: log-spaced bands narrower
   than one FFT bin clamped to that bin, so the first nine bands carried the
   SAME NUMBER and the bottom of the spectrum moved as one block. It looked
   like a working visualiser with a heavy bass response. */

var mapBands = audioT.GetMethod("MapBands", BindingFlags.NonPublic | BindingFlags.Static);

float[] Bands(double[] mag, int rate = 48000, int fftSize = 4096, int count = 64)
    => (float[])mapBands.Invoke(null, new object[] { mag, rate, fftSize, count });

// A spectrum that rises smoothly with frequency. Every band must read a
// DIFFERENT value: adjacent bands landing on the same number is precisely the
// bug, and on a ramp there is no legitimate reason for a tie.
{
    var mag = new double[2048];
    for (var i = 0; i < mag.Length; i++) mag[i] = 0.001 + i * 0.0004;
    var got = Bands(mag);
    var ties = 0;
    for (var b = 1; b < got.Length; b++) if (got[b] == got[b - 1]) ties++;
    Expect("no two bands read the same value on a ramp", ties.ToString(), "0");
    // And it must still be monotonic - interpolation that wanders is not a fix.
    var slips = 0;
    for (var b = 1; b < got.Length; b++) if (got[b] < got[b - 1]) slips++;
    Expect("a rising spectrum rises across the bars", slips.ToString(), "0");
}

// The bottom of the range is where this failed, so check it specifically: the
// first twelve bands cover 30 Hz upward and must all differ.
{
    var mag = new double[2048];
    for (var i = 0; i < mag.Length; i++) mag[i] = 0.001 + i * 0.0004;
    var got = Bands(mag);
    var distinct = new System.Collections.Generic.HashSet<float>();
    for (var b = 0; b < 12; b++) distinct.Add(got[b]);
    Expect("the lowest twelve bands are twelve values", distinct.Count.ToString(), "12");
}

// Silence is a floor, not noise, and a peak in one bin must not light the
// whole bottom end.
{
    var quiet = new double[2048];
    Expect("silence maps to zero everywhere",
        Bands(quiet).Any(v => v > 0) ? "moved" : "flat", "flat");

    var spike = new double[2048];
    spike[400] = 1.0;                     // 400 * 11.72 Hz = ~4.7 kHz
    var got = Bands(spike);
    var lit = got.Count(v => v > 0.5);
    Expect("a single tone lights a few bars, not the spectrum",
        lit <= 4 ? "focused" : $"{lit} bars", "focused");
}

Console.WriteLine(fails == 0
    ? "sim-agent resolve and state-shape tests passed"
    : $"FAIL: sim-agent ({fails} failed)");
return fails == 0 ? 0 : 1;
