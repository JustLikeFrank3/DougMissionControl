// SimBridge.cs — the one place that talks SimConnect.
//
// Owns a dedicated pump thread. Every SimConnect call happens on that thread:
// the managed wrapper is not thread-safe, so commands arriving on HTTP threads
// are marshalled through a queue and drained here. That is also what lets
// POST /command answer truthfully — `accepted` is set by the thread that
// actually made the TransmitClientEvent call, not by the one that wanted to.
//
// Losing the sim is normal operation, not an error. The workstation reboots
// into Linux and this whole process disappears; while it is running, MSFS may
// simply not be up yet. Connect failures are therefore quiet and retried
// forever, with no alarm state and no backoff escalation.

using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using Microsoft.FlightSimulator.SimConnect;

namespace FlightDeckSimAgent;

internal sealed class SimBridge : IDisposable
{
    private const int ReconnectDelayMs = 3000;

    // Every 6th sim frame: ~5-10 Hz at 30-60 fps, comfortably above the 4 Hz
    // the brief asks for and cheap enough to leave running all flight.
    private const uint StateFrameInterval = 5;

    private readonly CancellationTokenSource _cts = new();
    private readonly EventWaitHandle _simEvent = new(false, EventResetMode.AutoReset);
    private readonly ConcurrentQueue<Command> _commands = new();
    private readonly Thread _pump;

    private SimConnect? _sim;
    private SimStateRaw _state;
    private SimCapsRaw _caps;
    private SimGpsRaw _gps;
    private bool _haveState;
    private bool _haveCaps;
    private string _aircraft = "";
    private string _simName = "";

    /// <summary>Raised on the pump thread each time a fresh state frame lands.</summary>
    public event Action<SimStateRaw, SimCapsRaw, SimGpsRaw, string>? StateReceived;

    /// <summary>A live SimConnect session — not merely "the process is running".</summary>
    public bool Connected { get; private set; }

    public string Aircraft => _aircraft;

    /// <summary>Whatever the sim called itself when it opened the session, so
    /// /health reports the simulator we actually reached rather than the one we
    /// assumed. Empty while offline.</summary>
    public string SimName => _simName;

    private sealed record Command(Event Id, uint Data0, uint Data1, TaskCompletionSource<bool> Result);

    public SimBridge()
    {
        _pump = new Thread(Pump) { IsBackground = true, Name = "simconnect-pump" };
    }

    public void Start() => _pump.Start();

    /// <summary>
    /// Queue a sim event for transmission. The returned task completes true once
    /// the pump thread has actually transmitted it, false if there is no session
    /// or the transmit threw. True means "sent", never "it worked".
    /// </summary>
    public Task<bool> TransmitAsync(Event id, uint data0 = 0, uint data1 = 0)
    {
        if (!Connected) return Task.FromResult(false);
        var tcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        _commands.Enqueue(new Command(id, data0, data1, tcs));
        _simEvent.Set();   // wake the pump so the command does not wait on a frame
        return tcs.Task;
    }

    /// <summary>Last observed state. False until the first frame has landed.</summary>
    public bool TryGetState(out SimStateRaw state, out SimCapsRaw caps)
    {
        state = _state;
        caps = _caps;
        return _haveState && _haveCaps;
    }

    private void Pump()
    {
        while (!_cts.IsCancellationRequested)
        {
            if (_sim is null && !TryOpen())
            {
                DrainCommands(connected: false);
                _cts.Token.WaitHandle.WaitOne(ReconnectDelayMs);
                continue;
            }

            try
            {
                // Wake on either a SimConnect message or a queued command. The
                // 200 ms ceiling keeps shutdown responsive when both are idle.
                if (WaitHandle.WaitAny(new[] { _simEvent, _cts.Token.WaitHandle }, 200) == 0)
                {
                    _sim!.ReceiveMessage();
                }
                DrainCommands(connected: true);
            }
            catch (COMException)
            {
                // The sim went away mid-session. Same path as never having found
                // it: tear down, report offline, keep retrying quietly.
                Close();
            }
        }

        Close();
    }

    private bool TryOpen()
    {
        try
        {
            var sim = new SimConnect("FlightDeckSimAgent", IntPtr.Zero, 0, _simEvent, 0);

            sim.OnRecvOpen += OnOpen;
            sim.OnRecvQuit += (_, _) => Close();
            sim.OnRecvException += OnException;
            sim.OnRecvSimobjectData += OnSimObjectData;

            foreach (var (name, unit) in SimVars.StateVars)
                sim.AddToDataDefinition(Definition.State, name, unit, SIMCONNECT_DATATYPE.FLOAT64, 0.0f, SimConnect.SIMCONNECT_UNUSED);
            sim.RegisterDataDefineStruct<SimStateRaw>(Definition.State);

            foreach (var (name, unit) in SimVars.CapsVars)
                sim.AddToDataDefinition(Definition.Caps, name, unit, SIMCONNECT_DATATYPE.FLOAT64, 0.0f, SimConnect.SIMCONNECT_UNUSED);
            sim.RegisterDataDefineStruct<SimCapsRaw>(Definition.Caps);

            sim.AddToDataDefinition(Definition.Title, SimVars.TitleVar, null, SIMCONNECT_DATATYPE.STRING256, 0.0f, SimConnect.SIMCONNECT_UNUSED);
            sim.RegisterDataDefineStruct<SimTitleRaw>(Definition.Title);

            foreach (var (name, unit) in SimVars.GpsVars)
                sim.AddToDataDefinition(Definition.Gps, name, unit, SIMCONNECT_DATATYPE.FLOAT64, 0.0f, SimConnect.SIMCONNECT_UNUSED);
            foreach (var name in SimVars.GpsStringVars)
                sim.AddToDataDefinition(Definition.Gps, name, null, SIMCONNECT_DATATYPE.STRING32, 0.0f, SimConnect.SIMCONNECT_UNUSED);
            sim.RegisterDataDefineStruct<SimGpsRaw>(Definition.Gps);

            foreach (var (id, name) in SimVars.ClientEvents)
            {
                sim.MapClientEventToSimEvent(id, name);
                sim.AddClientEventToNotificationGroup(Group.Main, id, false);
            }
            sim.SetNotificationGroupPriority(Group.Main, SimConnect.SIMCONNECT_GROUP_PRIORITY_HIGHEST);

            _sim = sim;
            return true;
        }
        catch (COMException)
        {
            // MSFS is not up. Expected, and not worth a log line every 3 s.
            return false;
        }
    }

    private void OnOpen(SimConnect sender, SIMCONNECT_RECV_OPEN data)
    {
        _simName = data.szApplicationName?.Trim() ?? "";
        Connected = true;

        sender.RequestDataOnSimObject(Request.State, Definition.State,
            SimConnect.SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_PERIOD.SIM_FRAME,
            SIMCONNECT_DATA_REQUEST_FLAG.DEFAULT, 0, StateFrameInterval, 0);

        // Capabilities and aircraft identity change only on aircraft load, so a
        // 1 Hz CHANGED subscription is plenty and costs nothing.
        sender.RequestDataOnSimObject(Request.Caps, Definition.Caps,
            SimConnect.SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_PERIOD.SECOND,
            SIMCONNECT_DATA_REQUEST_FLAG.CHANGED, 0, 0, 0);

        sender.RequestDataOnSimObject(Request.Title, Definition.Title,
            SimConnect.SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_PERIOD.SECOND,
            SIMCONNECT_DATA_REQUEST_FLAG.CHANGED, 0, 0, 0);

        // Waypoints sequence every few minutes at cruise; 1 Hz on change costs
        // nothing and keeps the panel's plan a beat behind the GPS at worst.
        sender.RequestDataOnSimObject(Request.Gps, Definition.Gps,
            SimConnect.SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_PERIOD.SECOND,
            SIMCONNECT_DATA_REQUEST_FLAG.CHANGED, 0, 0, 0);
    }

    private void OnException(SimConnect sender, SIMCONNECT_RECV_EXCEPTION data)
    {
        // Most useful during Step 0: an unrecognised SimVar name or unit shows up
        // here as NAME_UNRECOGNIZED rather than as a silently zeroed field.
        Console.Error.WriteLine($"simconnect exception {(SIMCONNECT_EXCEPTION)data.dwException} (send id {data.dwSendID}, index {data.dwIndex})");
    }

    private void OnSimObjectData(SimConnect sender, SIMCONNECT_RECV_SIMOBJECT_DATA data)
    {
        switch ((Request)data.dwRequestID)
        {
            case Request.State:
                _state = (SimStateRaw)data.dwData[0];
                _haveState = true;
                if (_haveCaps) StateReceived?.Invoke(_state, _caps, _gps, _aircraft);
                break;

            case Request.Caps:
                _caps = (SimCapsRaw)data.dwData[0];
                _haveCaps = true;
                break;

            case Request.Title:
                _aircraft = ((SimTitleRaw)data.dwData[0]).Title?.Trim() ?? "";
                break;

            case Request.Gps:
                _gps = (SimGpsRaw)data.dwData[0];
                break;
        }
    }

    private void DrainCommands(bool connected)
    {
        while (_commands.TryDequeue(out var cmd))
        {
            if (!connected || _sim is null)
            {
                cmd.Result.TrySetResult(false);
                continue;
            }

            try
            {
                // AP_ALT_VAR_SET_ENGLISH takes both an altitude and an altitude
                // slot. The single-argument API silently writes the default
                // slot, which is not necessarily the slot the aircraft tracks.
                if (cmd.Id == Event.ApAltVarSet)
                {
                    _sim.TransmitClientEvent_EX1(SimConnect.SIMCONNECT_OBJECT_ID_USER, cmd.Id,
                        Group.Main, SIMCONNECT_EVENT_FLAG.GROUPID_IS_PRIORITY,
                        cmd.Data0, cmd.Data1, 0, 0, 0);
                }
                else
                {
                    _sim.TransmitClientEvent(SimConnect.SIMCONNECT_OBJECT_ID_USER, cmd.Id, cmd.Data0,
                        Group.Main, SIMCONNECT_EVENT_FLAG.GROUPID_IS_PRIORITY);
                }
                cmd.Result.TrySetResult(true);
            }
            catch (COMException)
            {
                cmd.Result.TrySetResult(false);
                throw;   // let Pump tear the session down
            }
        }
    }

    private void Close()
    {
        Connected = false;
        _haveState = false;
        _haveCaps = false;
        _gps = default;
        _aircraft = "";
        _simName = "";
        try { _sim?.Dispose(); } catch (COMException) { }
        _sim = null;
        DrainCommands(connected: false);
    }

    public void Dispose()
    {
        _cts.Cancel();
        _simEvent.Set();
        if (_pump.IsAlive) _pump.Join(TimeSpan.FromSeconds(2));
        _cts.Dispose();
        _simEvent.Dispose();
    }
}
