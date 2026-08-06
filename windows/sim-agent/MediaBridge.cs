// MediaBridge.cs — system-wide now-playing, via Windows' own media session
// manager (the API behind the volume-flyout media card). Whatever is actually
// producing audio — Spotify, a browser tab, VLC — reports here without any
// per-app integration.
//
// Same philosophy as the sim side: this publishes OBSERVED playback state.
// A transport command is transmitted to the session and acknowledged as sent;
// whether playback actually changed shows up in the next snapshot, never in
// the reply.
//
// Album art is deliberately NOT in the /media JSON. It can run tens of
// kilobytes, and the widget polls every couple of seconds — so the JSON
// carries art_id (a hash of track identity) and the art bytes live behind
// /media/art, fetched only when art_id changes.

using System.Runtime.InteropServices.WindowsRuntime;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Windows.Media.Control;

namespace FlightDeckSimAgent;

internal sealed class MediaBridge
{
    private readonly object _lock = new();
    private JsonObject _snapshot = new() { ["active"] = false };
    private byte[] _art = Array.Empty<byte>();
    private string _artId = "";
    private string _artFetchedFor = "";

    public JsonObject Snapshot()
    {
        lock (_lock) return (JsonObject)_snapshot.DeepClone();
    }

    public (byte[] Bytes, string Id) Art()
    {
        lock (_lock) return (_art, _artId);
    }

    /// <summary>Refresh the snapshot. Called from the HTTP path at most once
    /// per poll; failures degrade to "no session" rather than throwing.</summary>
    public async Task RefreshAsync()
    {
        try
        {
            var mgr = await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
            var session = mgr.GetCurrentSession();
            if (session is null) { Set(new JsonObject { ["active"] = false }, null, ""); return; }

            var info = session.GetPlaybackInfo();
            var media = await session.TryGetMediaPropertiesAsync();
            var timeline = session.GetTimelineProperties();

            var playing = info.PlaybackStatus ==
                GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing;
            var trackKey = $"{media.Title}{media.Artist}{media.AlbumTitle}";
            var artId = Convert.ToHexString(
                MD5.HashData(Encoding.UTF8.GetBytes(trackKey)))[..12].ToLowerInvariant();

            var snap = new JsonObject
            {
                ["active"] = true,
                ["playing"] = playing,
                ["title"] = media.Title ?? "",
                ["artist"] = media.Artist ?? "",
                ["album"] = media.AlbumTitle ?? "",
                // The AUMID names the app owning the session (e.g. Spotify.exe).
                ["app"] = session.SourceAppUserModelId ?? "",
                ["position_s"] = (int)timeline.Position.TotalSeconds,
                ["duration_s"] = (int)timeline.EndTime.TotalSeconds,
                ["art_id"] = artId,
                ["can"] = new JsonObject
                {
                    ["play_pause"] = info.Controls.IsPlayEnabled || info.Controls.IsPauseEnabled,
                    ["next"] = info.Controls.IsNextEnabled,
                    ["prev"] = info.Controls.IsPreviousEnabled,
                },
            };

            byte[]? art = null;
            if (artId != _artFetchedFor && media.Thumbnail is not null)
            {
                try
                {
                    using var stream = await media.Thumbnail.OpenReadAsync();
                    var buf = new byte[stream.Size];
                    await stream.ReadAsync(buf.AsBuffer(), (uint)stream.Size,
                        Windows.Storage.Streams.InputStreamOptions.None);
                    art = buf;
                }
                catch { art = Array.Empty<byte>(); }   // track without art is fine
            }

            Set(snap, art, artId);
        }
        catch
        {
            Set(new JsonObject { ["active"] = false }, null, "");
        }
    }

    /// <summary>Send one transport command. True means the session accepted the
    /// call — observed state still only changes via the next snapshot.</summary>
    public async Task<bool> CommandAsync(string action)
    {
        try
        {
            var mgr = await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
            var session = mgr.GetCurrentSession();
            if (session is null) return false;
            return action switch
            {
                "play_pause" => await session.TryTogglePlayPauseAsync(),
                "next" => await session.TrySkipNextAsync(),
                "prev" => await session.TrySkipPreviousAsync(),
                _ => false,
            };
        }
        catch { return false; }
    }

    private void Set(JsonObject snap, byte[]? art, string artId)
    {
        lock (_lock)
        {
            _snapshot = snap;
            if (art is not null) { _art = art; _artFetchedFor = artId; }
            _artId = artId;
            if (artId.Length == 0) { _art = Array.Empty<byte>(); _artFetchedFor = ""; }
        }
    }
}
