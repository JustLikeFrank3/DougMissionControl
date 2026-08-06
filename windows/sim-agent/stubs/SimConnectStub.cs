// stubs/SimConnectStub.cs — COMPILE-TIME ONLY. Never shipped, never run.
//
// Declares just enough of the managed SimConnect surface for the rest of the
// agent to compile on a machine without the MSFS SDK installed, so the other
// ~800 lines get real compiler feedback instead of none.
//
// What this proves: the agent's own types, nullability and control flow.
// What it does NOT prove: that these signatures match the real
// Microsoft.FlightSimulator.SimConnect.dll. They are written from the SDK's
// documented surface and could be wrong. A build against the real reference is
// the only thing that settles that.
//
//   dotnet build -p:SimConnectStub=true    # this file, no SDK needed
//   dotnet build                           # the real reference, SDK required
//
// The #error below is the quarantine: this file is excluded from the compile
// unless SimConnectStub=true, and if that exclusion ever breaks the build fails
// loudly here rather than silently producing an agent that cannot reach a sim.

#if !SIMCONNECT_STUB
#error SimConnectStub.cs was compiled into a real build. It is a compile-only shim and can never talk to MSFS. Fix the Compile Remove condition in FlightDeckSimAgent.csproj.
#endif

using System.Runtime.InteropServices;

namespace Microsoft.FlightSimulator.SimConnect;

public enum SIMCONNECT_DATATYPE { INVALID, INT32, INT64, FLOAT32, FLOAT64, STRING8, STRING32, STRING64, STRING128, STRING256, STRING260, STRINGV }

public enum SIMCONNECT_PERIOD { NEVER, ONCE, VISUAL_FRAME, SIM_FRAME, SECOND }

[Flags]
public enum SIMCONNECT_DATA_REQUEST_FLAG { DEFAULT = 0, CHANGED = 1, TAGGED = 2 }

[Flags]
public enum SIMCONNECT_EVENT_FLAG { DEFAULT = 0, FAST_REPEAT_TIMER = 1, SLOW_REPEAT_TIMER = 2, GROUPID_IS_PRIORITY = 16 }

public enum SIMCONNECT_EXCEPTION
{
    NONE, ERROR, SIZE_MISMATCH, UNRECOGNIZED_ID, UNOPENED, VERSION_MISMATCH,
    TOO_MANY_GROUPS, NAME_UNRECOGNIZED, TOO_MANY_EVENT_NAMES, EVENT_ID_DUPLICATE,
    TOO_MANY_MAPS, TOO_MANY_OBJECTS, TOO_MANY_REQUESTS, DATA_ERROR, INVALID_ARRAY,
}

public class SIMCONNECT_RECV { public uint dwID; }

public class SIMCONNECT_RECV_OPEN : SIMCONNECT_RECV { public string szApplicationName = ""; }

public class SIMCONNECT_RECV_EXCEPTION : SIMCONNECT_RECV { public uint dwException; public uint dwSendID; public uint dwIndex; }

public class SIMCONNECT_RECV_SIMOBJECT_DATA : SIMCONNECT_RECV { public uint dwRequestID; public object[] dwData = Array.Empty<object>(); }

public class SimConnect : IDisposable
{
    public const uint SIMCONNECT_UNUSED = 0xFFFFFFFF;
    public const uint SIMCONNECT_OBJECT_ID_USER = 0;
    public const uint SIMCONNECT_GROUP_PRIORITY_HIGHEST = 1;

    public delegate void RecvOpenEventHandler(SimConnect sender, SIMCONNECT_RECV_OPEN data);
    public delegate void RecvQuitEventHandler(SimConnect sender, SIMCONNECT_RECV data);
    public delegate void RecvExceptionEventHandler(SimConnect sender, SIMCONNECT_RECV_EXCEPTION data);
    public delegate void RecvSimobjectDataEventHandler(SimConnect sender, SIMCONNECT_RECV_SIMOBJECT_DATA data);

    public event RecvOpenEventHandler? OnRecvOpen;
    public event RecvQuitEventHandler? OnRecvQuit;
    public event RecvExceptionEventHandler? OnRecvException;
    public event RecvSimobjectDataEventHandler? OnRecvSimobjectData;

    public SimConnect(string szName, IntPtr hWnd, uint UserEventWin32, WaitHandle? hEventHandle, uint ConfigIndex)
        => throw new COMException("SimConnect stub: no simulator, and never will be.");

    public void AddToDataDefinition(Enum DefineID, string DatumName, string? UnitsName, SIMCONNECT_DATATYPE DatumType, float fEpsilon, uint DatumID) { }
    public void RegisterDataDefineStruct<T>(Enum dwID) { }
    public void MapClientEventToSimEvent(Enum EventID, string EventName) { }
    public void AddClientEventToNotificationGroup(Enum GroupID, Enum EventID, bool bMaskable) { }
    public void SetNotificationGroupPriority(Enum GroupID, uint uPriority) { }
    public void RequestDataOnSimObject(Enum RequestID, Enum DefineID, uint ObjectID, SIMCONNECT_PERIOD Period, SIMCONNECT_DATA_REQUEST_FLAG Flags, uint origin, uint interval, uint limit) { }
    public void TransmitClientEvent(uint ObjectID, Enum EventID, uint dwData, Enum GroupID, SIMCONNECT_EVENT_FLAG Flags) { }
    public uint GetLastSentPacketID() => 0;
    public void ReceiveMessage() { }
    public void Dispose() { GC.SuppressFinalize(this); }

    // Silences "event is never used" on the handlers above; unreachable.
    private void NeverCalled()
    {
        OnRecvOpen?.Invoke(this, new SIMCONNECT_RECV_OPEN());
        OnRecvQuit?.Invoke(this, new SIMCONNECT_RECV());
        OnRecvException?.Invoke(this, new SIMCONNECT_RECV_EXCEPTION());
        OnRecvSimobjectData?.Invoke(this, new SIMCONNECT_RECV_SIMOBJECT_DATA());
    }
}
