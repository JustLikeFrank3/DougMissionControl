// HttpApi.cs — the LAN surface, on :9109.
//
// Raw TcpListener and a hand-rolled HTTP/1.1 response, exactly as
// windows/boot-agent.ps1 does it and for the same reason: HttpListener needs a
// urlacl reservation for any non-localhost prefix, a hand-rolled response needs
// nothing. It also makes SSE trivial — write the headers, then keep writing
// `data: {...}` frames down the same socket forever.
//
//   GET  /                       -> 200 "sim-agent"   (unauthenticated liveness,
//   GET  /status                    mirrors boot-agent.ps1's / and /status so
//                                   deck-api can probe without holding a token)
//   GET  /health?token=<token>   -> agent + session identity
//   GET  /state?token=<token>    -> one normalized snapshot
//   GET  /events?token=<token>   -> SSE stream of the /state shape
//   POST /command?token=<token>  -> transmit one control command
//
// Token: first line of C:\ProgramData\dualboot\sim-agent.token, compared in
// constant time, case-sensitively. Also accepted as an X-Auth-Token header,
// which is the better habit; the query form exists to match the boot agent.
// Wrong token is 403. Non-RFC1918 source is 403 before the token is even read.

using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;

namespace FlightDeckSimAgent;

internal sealed class HttpApi : IDisposable
{
    private readonly TcpListener _listener;
    private readonly byte[] _token;
    private readonly CancellationTokenSource _cts = new();

    // DropOldest: a slow or wedged panel must never stall the sim loop, and a
    // stale state frame is worthless anyway — the next one is 200 ms behind it.
    private readonly System.Collections.Concurrent.ConcurrentDictionary<Guid, Channel<string>> _subscribers = new();

    public Func<JsonObject> Health { get; init; } = () => new JsonObject();
    public Func<JsonObject?> State { get; init; } = () => null;
    public Func<JsonObject, Task<JsonObject>> Command { get; init; } = _ => Task.FromResult(new JsonObject());
    public Func<Task<JsonObject>> Media { get; init; } = () => Task.FromResult(new JsonObject());
    public Func<(byte[] Bytes, string Id)> MediaArt { get; init; } = () => (Array.Empty<byte>(), "");
    public Func<string, Task<JsonObject>> MediaCommand { get; init; } = _ => Task.FromResult(new JsonObject());
    public Func<JsonObject> Apps { get; init; } = () => new JsonObject();
    public Func<JsonObject> AppsLaunch { get; init; } = () => new JsonObject();
    public Func<string, JsonObject> AppsMacro { get; init; } = _ => new JsonObject();

    public HttpApi(int port, string token)
    {
        _listener = new TcpListener(IPAddress.Any, port);
        _token = Encoding.UTF8.GetBytes(token);
    }

    public void Start()
    {
        _listener.Start();
        _ = AcceptLoopAsync();
    }

    /// <summary>Push one state snapshot to every open SSE stream.</summary>
    public void Broadcast(JsonObject state)
    {
        if (_subscribers.IsEmpty) return;
        var payload = state.ToJsonString();
        foreach (var sub in _subscribers.Values) sub.Writer.TryWrite(payload);
    }

    private async Task AcceptLoopAsync()
    {
        while (!_cts.IsCancellationRequested)
        {
            TcpClient client;
            try { client = await _listener.AcceptTcpClientAsync(_cts.Token); }
            catch (OperationCanceledException) { break; }
            catch (SocketException) { continue; }

            _ = HandleAsync(client);
        }
    }

    private async Task HandleAsync(TcpClient client)
    {
        using (client)
        {
            try
            {
                client.NoDelay = true;
                var remote = (client.Client.RemoteEndPoint as IPEndPoint)?.Address;
                var stream = client.GetStream();

                var req = await ReadRequestAsync(stream, _cts.Token);
                if (req is null) return;

                if (!IsLan(remote))
                {
                    await WriteAsync(stream, "403 Forbidden", "text/plain", "forbidden\n");
                    return;
                }

                await RouteAsync(stream, req);
            }
            catch (IOException) { }          // client hung up
            catch (OperationCanceledException) { }
            catch (SocketException) { }
        }
    }

    private async Task RouteAsync(NetworkStream stream, HttpRequest req)
    {
        // Liveness first: no token, both verbs of the path, plain text. deck-api
        // uses this to tell "agent process up" from "sim session live", which
        // /health answers.
        if (req.Method == "GET" && (req.Path == "/" || req.Path == "/status"))
        {
            await WriteAsync(stream, "200 OK", "text/plain", "sim-agent\n");
            return;
        }

        if (!Authorized(req))
        {
            await WriteAsync(stream, "403 Forbidden", "text/plain", "forbidden\n");
            return;
        }

        switch (req.Method, req.Path)
        {
            case ("GET", "/health"):
                await WriteJsonAsync(stream, "200 OK", Health());
                return;

            case ("GET", "/state"):
                var state = State();
                if (state is null)
                {
                    await WriteJsonAsync(stream, "503 Service Unavailable",
                        new JsonObject { ["error"] = "sim not connected" });
                    return;
                }
                await WriteJsonAsync(stream, "200 OK", state);
                return;

            case ("GET", "/events"):
                await StreamEventsAsync(stream);
                return;

            // ── media: system-wide now-playing, art behind its own path ──
            case ("GET", "/media"):
                await WriteJsonAsync(stream, "200 OK", await Media());
                return;

            case ("GET", "/media/art"):
            {
                var (bytes, id) = MediaArt();
                if (bytes.Length == 0)
                {
                    await WriteAsync(stream, "404 Not Found", "text/plain", "no art\n");
                    return;
                }
                var head = $"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nX-Art-Id: {id}\r\n"
                         + $"Content-Length: {bytes.Length}\r\nConnection: close\r\n\r\n";
                await stream.WriteAsync(Encoding.ASCII.GetBytes(head), _cts.Token);
                await stream.WriteAsync(bytes, _cts.Token);
                await stream.FlushAsync(_cts.Token);
                return;
            }

            case ("POST", "/media/command"):
            {
                var mb = ParseBody(req);
                if (mb is null) { await BadBody(stream); return; }
                await WriteJsonAsync(stream, "200 OK",
                    await MediaCommand(mb["action"]?.ToString() ?? ""));
                return;
            }

            // ── apps: squadrons session + open-loop macros ──
            case ("GET", "/apps"):
                await WriteJsonAsync(stream, "200 OK", Apps());
                return;

            case ("POST", "/apps/squadrons/launch"):
                await WriteJsonAsync(stream, "200 OK", AppsLaunch());
                return;

            case ("POST", "/apps/squadrons/macro"):
            {
                var ab = ParseBody(req);
                if (ab is null) { await BadBody(stream); return; }
                await WriteJsonAsync(stream, "200 OK",
                    AppsMacro(ab["id"]?.ToString() ?? ""));
                return;
            }

            case ("POST", "/command"):
                var body = ParseBody(req);
                if (body is null) { await BadBody(stream); return; }
                await WriteJsonAsync(stream, "200 OK", await Command(body));
                return;

            default:
                await WriteAsync(stream, "404 Not Found", "text/plain", "not found\n");
                return;
        }
    }

    private async Task StreamEventsAsync(NetworkStream stream)
    {
        var id = Guid.NewGuid();
        var channel = Channel.CreateBounded<string>(new BoundedChannelOptions(8)
        {
            FullMode = BoundedChannelFullMode.DropOldest,
            SingleReader = true,
        });
        _subscribers[id] = channel;

        try
        {
            var head = "HTTP/1.1 200 OK\r\n"
                     + "Content-Type: text/event-stream\r\n"
                     + "Cache-Control: no-cache\r\n"
                     + "Connection: keep-alive\r\n\r\n";
            await stream.WriteAsync(Encoding.ASCII.GetBytes(head), _cts.Token);
            await stream.FlushAsync(_cts.Token);

            // Open with the current snapshot so a panel that connects mid-flight
            // renders observed state immediately instead of waiting for a change.
            var now = State();
            if (now is not null) channel.Writer.TryWrite(now.ToJsonString());

            await foreach (var payload in channel.Reader.ReadAllAsync(_cts.Token))
            {
                var frame = Encoding.UTF8.GetBytes($"data: {payload}\n\n");
                await stream.WriteAsync(frame, _cts.Token);
                await stream.FlushAsync(_cts.Token);
            }
        }
        catch (IOException) { }              // panel closed the tab, or rebooted
        catch (OperationCanceledException) { }
        finally
        {
            _subscribers.TryRemove(id, out _);
        }
    }

    private bool Authorized(HttpRequest req)
    {
        if (_token.Length == 0) return false;
        var supplied = req.Headers.TryGetValue("x-auth-token", out var h) && h.Length > 0
            ? h
            : req.Query.TryGetValue("token", out var q) ? q : null;
        if (supplied is null) return false;

        var bytes = Encoding.UTF8.GetBytes(supplied);
        return bytes.Length == _token.Length && CryptographicOperations.FixedTimeEquals(bytes, _token);
    }

    private static bool IsLan(IPAddress? addr)
    {
        if (addr is null) return false;
        if (IPAddress.IsLoopback(addr)) return true;
        if (addr.IsIPv4MappedToIPv6) addr = addr.MapToIPv4();
        if (addr.AddressFamily != AddressFamily.InterNetwork) return false;

        var b = addr.GetAddressBytes();
        return b[0] == 10
            || (b[0] == 172 && b[1] >= 16 && b[1] <= 31)
            || (b[0] == 192 && b[1] == 168);
    }

    private sealed record HttpRequest(
        string Method,
        string Path,
        Dictionary<string, string> Query,
        Dictionary<string, string> Headers,
        string Body);

    private static async Task<HttpRequest?> ReadRequestAsync(NetworkStream stream, CancellationToken ct)
    {
        var buffer = new MemoryStream();
        var chunk = new byte[2048];
        int headerEnd = -1;

        while (headerEnd < 0)
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(5));
            int read = await stream.ReadAsync(chunk, timeout.Token);
            if (read == 0) return null;
            buffer.Write(chunk, 0, read);
            headerEnd = IndexOfHeaderEnd(buffer.GetBuffer(), (int)buffer.Length);
            if (buffer.Length > 64 * 1024) return null;
        }

        var raw = buffer.GetBuffer();
        var headerText = Encoding.UTF8.GetString(raw, 0, headerEnd);
        var lines = headerText.Split("\r\n");
        var parts = lines[0].Split(' ');
        if (parts.Length < 2) return null;

        var method = parts[0].ToUpperInvariant();
        var target = parts[1];
        var query = new Dictionary<string, string>(StringComparer.Ordinal);
        var path = target;
        var qIdx = target.IndexOf('?');
        if (qIdx >= 0)
        {
            path = target[..qIdx];
            foreach (var pair in target[(qIdx + 1)..].Split('&', StringSplitOptions.RemoveEmptyEntries))
            {
                var eq = pair.IndexOf('=');
                if (eq < 0) query[Uri.UnescapeDataString(pair)] = "";
                else query[Uri.UnescapeDataString(pair[..eq])] = Uri.UnescapeDataString(pair[(eq + 1)..]);
            }
        }

        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in lines.Skip(1))
        {
            var colon = line.IndexOf(':');
            if (colon > 0) headers[line[..colon].Trim()] = line[(colon + 1)..].Trim();
        }

        var body = "";
        if (headers.TryGetValue("Content-Length", out var lenText) && int.TryParse(lenText, out var len) && len > 0)
        {
            if (len > 64 * 1024) return null;
            var bodyStart = headerEnd + 4;
            var have = (int)buffer.Length - bodyStart;
            var bodyBytes = new byte[len];
            Array.Copy(raw, bodyStart, bodyBytes, 0, Math.Min(have, len));

            while (have < len)
            {
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
                timeout.CancelAfter(TimeSpan.FromSeconds(5));
                int read = await stream.ReadAsync(bodyBytes.AsMemory(have, len - have), timeout.Token);
                if (read == 0) break;
                have += read;
            }
            body = Encoding.UTF8.GetString(bodyBytes);
        }

        return new HttpRequest(method, path, query, headers, body);
    }

    private static int IndexOfHeaderEnd(byte[] buf, int length)
    {
        for (int i = 0; i + 3 < length; i++)
            if (buf[i] == '\r' && buf[i + 1] == '\n' && buf[i + 2] == '\r' && buf[i + 3] == '\n')
                return i;
        return -1;
    }

    private static JsonObject? ParseBody(HttpRequest req)
    {
        try { return JsonNode.Parse(req.Body) as JsonObject; }
        catch (JsonException) { return null; }
    }

    private static Task BadBody(NetworkStream stream) =>
        WriteJsonAsync(stream, "400 Bad Request",
            new JsonObject { ["accepted"] = false, ["reason"] = "malformed body" });

    private static Task WriteJsonAsync(NetworkStream stream, string status, JsonObject body) =>
        WriteAsync(stream, status, "application/json", body.ToJsonString());

    private static async Task WriteAsync(NetworkStream stream, string status, string contentType, string body)
    {
        var bytes = Encoding.UTF8.GetBytes(body);
        var head = $"HTTP/1.1 {status}\r\nContent-Type: {contentType}\r\nContent-Length: {bytes.Length}\r\nConnection: close\r\n\r\n";
        await stream.WriteAsync(Encoding.ASCII.GetBytes(head));
        await stream.WriteAsync(bytes);
        await stream.FlushAsync();
    }

    public void Dispose()
    {
        _cts.Cancel();
        _listener.Stop();
        foreach (var sub in _subscribers.Values) sub.Writer.TryComplete();
        _cts.Dispose();
    }
}
