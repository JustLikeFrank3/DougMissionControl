// AudioBridge.cs - what the speakers are actually doing, as a spectrum.
//
// The panel is on the Pi and the music is on the workstation, so the Edge has
// no signal of its own to visualise. Web Audio on the Pi would have nothing to
// listen to. The honest way to draw a spectrum on that panel is to measure it
// where the sound is and send the bands over the link that already exists.
//
// The alternative - bars driven by track position, or a canned pattern that
// merely looks busy - is exactly the lie the rest of this panel is built to
// avoid. SIM never draws a commanded gear position and SCREENS never draws a
// commanded input; a visualiser that is not listening to the music would be
// the same class of untruth with better graphics.
//
// WASAPI loopback: open the default RENDER endpoint for capture and the OS
// hands back what is being played. No virtual cable, no driver, no effect on
// what comes out of the Scarlett. When nothing is playing the endpoint returns
// silence, which reads as a flat spectrum - correctly, because that is what is
// coming out of the speakers.
//
// Raw COM rather than NAudio: this project has no NuGet dependencies and one
// audio capture path is not worth becoming the reason it acquires its first.

using System.Runtime.InteropServices;

namespace FlightDeckSimAgent;

internal sealed class AudioBridge : IDisposable
{
    /// <summary>Bands the panel draws. Enough to look like a spectrum, few
    /// enough that the JSON stays trivial at 20 Hz.</summary>
    public const int BandCount = 48;

    private const int FftSize = 1024;          // power of two, ~21ms at 48kHz
    private const int PublishMs = 50;          // 20 Hz
    private const int ReconnectMs = 3000;

    private readonly CancellationTokenSource _cts = new();
    private readonly Thread _pump;
    private readonly object _lock = new();

    // The rolling window the FFT reads, mono-mixed.
    private readonly float[] _window = new float[FftSize];
    private int _windowPos;

    private readonly float[] _bands = new float[BandCount];
    private float _peak;
    private bool _active;
    private string _why = "not started";

    public AudioBridge()
    {
        _pump = new Thread(Pump) { IsBackground = true, Name = "audio-loopback" };
    }

    public void Start() => _pump.Start();

    /// <summary>
    /// The current spectrum: BandCount magnitudes in 0..1, low frequency
    /// first, plus the overall peak. `active` is false when there is no
    /// capture session at all - which is different from a session that is
    /// capturing silence, and the panel says so differently.
    /// </summary>
    public (bool Active, float[] Bands, float Peak, string Why) Snapshot()
    {
        lock (_lock) return (_active, (float[])_bands.Clone(), _peak, _why);
    }

    private void Pump()
    {
        while (!_cts.IsCancellationRequested)
        {
            try
            {
                Capture();
            }
            catch (COMException e)
            {
                // The message now names the call that failed, because Ok()
                // built it that way. "GetMixFormat failed (0x88890004)" is a
                // sentence someone can act on; "NullReferenceException" is not.
                Fail($"{e.Message} (0x{e.HResult:X8})");
            }
            catch (Exception e)
            {
                Fail($"audio capture failed: {e.GetType().Name}");
            }
            // Losing the endpoint is ordinary: the default device changes when
            // a monitor with speakers is plugged in, which is exactly what
            // happens on this desk. Reopen quietly, forever.
            if (!_cts.IsCancellationRequested)
                _cts.Token.WaitHandle.WaitOne(ReconnectMs);
        }
    }

    private void Fail(string why)
    {
        lock (_lock)
        {
            _active = false;
            _why = why;
            Array.Clear(_bands);
            _peak = 0;
        }
    }

    /// <summary>
    /// Every COM call here returns an HRESULT that used to be dropped on the
    /// floor. A failed call then left a null behind and the first NULL
    /// DEREFERENCE several frames later became the error message - which is how
    /// a mis-declared vtable slot reported itself as "NullReferenceException"
    /// and named nothing that would help. Check each one where it happens.
    /// </summary>
    private static void Ok(int hr, string what)
    {
        if (hr != 0) throw new COMException($"{what} failed", hr);
    }

    private void Capture()
    {
        var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        Ok(enumerator.GetDefaultAudioEndpoint(EDataFlow.Render, ERole.Console, out var device),
           "GetDefaultAudioEndpoint");
        try
        {
            var iid = typeof(IAudioClient).GUID;
            Ok(device.Activate(ref iid, CLSCTX_ALL, IntPtr.Zero, out var clientObj),
               "IMMDevice.Activate");
            var client = (IAudioClient)clientObj;

            Ok(client.GetMixFormat(out var pFormat), "GetMixFormat");
            try
            {
                var fmt = Marshal.PtrToStructure<WAVEFORMATEX>(pFormat);
                // The mix format is whatever the endpoint runs at, and in
                // shared mode it cannot be argued with - so read it rather
                // than assume 48kHz stereo float, which is usually but not
                // always what it is.
                var channels = fmt.nChannels;
                var rate = fmt.nSamplesPerSec;
                var bits = fmt.wBitsPerSample;
                var isFloat = IsFloat(pFormat, fmt);

                // 200ms buffer, loopback, event-free (we poll on our own timer,
                // which is simpler than an event handle and plenty at 20Hz).
                Ok(client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                    AUDCLNT_STREAMFLAGS_LOOPBACK, 2_000_000, 0, pFormat, IntPtr.Zero),
                   "IAudioClient.Initialize");

                var captureIid = typeof(IAudioCaptureClient).GUID;
                Ok(client.GetService(ref captureIid, out var captureObj), "GetService");
                var capture = (IAudioCaptureClient)captureObj;

                Ok(client.Start(), "IAudioClient.Start");
                lock (_lock) { _active = true; _why = ""; }

                var lastPublish = Environment.TickCount64;
                try
                {
                    while (!_cts.IsCancellationRequested)
                    {
                        Drain(capture, channels, bits, isFloat);

                        var now = Environment.TickCount64;
                        if (now - lastPublish >= PublishMs)
                        {
                            lastPublish = now;
                            Publish(rate);
                        }
                        // Well under the buffer, so nothing is ever dropped.
                        _cts.Token.WaitHandle.WaitOne(10);
                    }
                }
                finally
                {
                    client.Stop();
                    Marshal.ReleaseComObject(capture);
                }
            }
            finally
            {
                Marshal.FreeCoTaskMem(pFormat);
                Marshal.ReleaseComObject(client);
            }
        }
        finally
        {
            Marshal.ReleaseComObject(device);
            Marshal.ReleaseComObject(enumerator);
        }
    }

    /// <summary>
    /// WAVEFORMATEXTENSIBLE hides the real sample type behind a subformat GUID,
    /// and the mix format is almost always extensible. Reading wFormatTag alone
    /// would call 32-bit float "unknown" and produce a spectrum of noise.
    /// </summary>
    private static bool IsFloat(IntPtr pFormat, WAVEFORMATEX fmt)
    {
        const int WAVE_FORMAT_IEEE_FLOAT = 0x0003;
        const int WAVE_FORMAT_EXTENSIBLE = unchecked((short)0xFFFE);
        if (fmt.wFormatTag == WAVE_FORMAT_IEEE_FLOAT) return true;
        if (fmt.wFormatTag != WAVE_FORMAT_EXTENSIBLE) return false;
        // SubFormat sits at the end of WAVEFORMATEXTENSIBLE: 18 bytes of
        // WAVEFORMATEX, then 2 of samples union and 4 of channel mask.
        var sub = Marshal.PtrToStructure<Guid>(pFormat + 18 + 2 + 4);
        return sub == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
    }

    /// <summary>Pull everything waiting and fold it into the rolling window.</summary>
    private void Drain(IAudioCaptureClient capture, int channels, int bits, bool isFloat)
    {
        while (true)
        {
            Ok(capture.GetNextPacketSize(out var frames), "GetNextPacketSize");
            if (frames == 0) return;

            // S_FALSE (1) means the buffer is empty, which is not an error.
            var hr = capture.GetBuffer(out var pData, out var got, out var flags, out _, out _);
            if (hr == 1) return;
            Ok(hr, "GetBuffer");
            try
            {
                if (got == 0) continue;
                // AUDCLNT_BUFFERFLAGS_SILENT means the buffer contents are
                // undefined and must be treated as zero - reading it as audio
                // draws a spectrum out of uninitialised memory.
                if ((flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0)
                {
                    for (var i = 0; i < got; i++) Push(0f);
                    continue;
                }
                for (var frame = 0; frame < got; frame++)
                {
                    // Mono-mix: a spectrum is about content, not stereo image,
                    // and one set of bars cannot honestly show two channels.
                    double sum = 0;
                    for (var ch = 0; ch < channels; ch++)
                    {
                        var offset = (frame * channels + ch) * (bits / 8);
                        sum += ReadSample(pData + offset, bits, isFloat);
                    }
                    Push((float)(sum / Math.Max(channels, 1)));
                }
            }
            finally
            {
                capture.ReleaseBuffer(got);
            }
        }
    }

    private static double ReadSample(IntPtr p, int bits, bool isFloat)
    {
        if (isFloat) return bits == 64 ? Marshal.PtrToStructure<double>(p)
                                       : Marshal.PtrToStructure<float>(p);
        return bits switch
        {
            16 => Marshal.ReadInt16(p) / 32768.0,
            32 => Marshal.ReadInt32(p) / 2147483648.0,
            // 24-bit packed, little-endian: sign-extend the top byte.
            24 => ((Marshal.ReadByte(p) | (Marshal.ReadByte(p, 1) << 8)
                    | (sbyte)Marshal.ReadByte(p, 2) << 16)) / 8388608.0,
            8 => (Marshal.ReadByte(p) - 128) / 128.0,
            _ => 0,
        };
    }

    private void Push(float sample)
    {
        _window[_windowPos] = sample;
        _windowPos = (_windowPos + 1) % FftSize;
    }

    private void Publish(int sampleRate)
    {
        var re = new double[FftSize];
        var im = new double[FftSize];
        // Unwrap the ring so the newest sample lands last, and apply a Hann
        // window - without it every band leaks into its neighbours and the
        // spectrum looks like a smear rather than notes.
        for (var i = 0; i < FftSize; i++)
        {
            var s = _window[(_windowPos + i) % FftSize];
            re[i] = s * (0.5 - 0.5 * Math.Cos(2 * Math.PI * i / (FftSize - 1)));
        }

        Fft(re, im);

        // Log-spaced bands from 30Hz to 16kHz. Linear bins would put four
        // fifths of the bars above 5kHz, where music has almost nothing, and
        // the bass - the part you can see moving - would be one bar wide.
        var bins = FftSize / 2;
        var hzPerBin = sampleRate / (double)FftSize;
        var lo = 30.0;
        var hi = Math.Min(16000.0, sampleRate / 2.0 - hzPerBin);
        var bands = new float[BandCount];
        float peak = 0;

        for (var b = 0; b < BandCount; b++)
        {
            var f0 = lo * Math.Pow(hi / lo, b / (double)BandCount);
            var f1 = lo * Math.Pow(hi / lo, (b + 1) / (double)BandCount);
            var i0 = Math.Clamp((int)(f0 / hzPerBin), 1, bins - 1);
            var i1 = Math.Clamp((int)(f1 / hzPerBin), i0 + 1, bins);

            double best = 0;
            for (var i = i0; i < i1; i++)
            {
                var mag = Math.Sqrt(re[i] * re[i] + im[i] * im[i]) / (FftSize / 2.0);
                if (mag > best) best = mag;
            }
            // dB, then mapped onto 0..1 across a 60dB floor. Linear magnitude
            // looks dead: music spends most of its time in the bottom few
            // percent of it, and the bars would barely leave the floor.
            var db = 20 * Math.Log10(best + 1e-9);
            var v = (float)Math.Clamp((db + 60) / 60.0, 0, 1);
            bands[b] = v;
            if (v > peak) peak = v;
        }

        lock (_lock)
        {
            // Attack fast, release slow. A spectrum that falls as fast as it
            // rises reads as flicker; this is the same asymmetry every real
            // meter uses.
            for (var b = 0; b < BandCount; b++)
                _bands[b] = bands[b] > _bands[b] ? bands[b]
                          : _bands[b] * 0.75f + bands[b] * 0.25f;
            _peak = peak;
        }
    }

    /// <summary>In-place iterative radix-2 FFT. Small, allocation-free per
    /// stage, and entirely adequate for 1024 points at 20Hz.</summary>
    private static void Fft(double[] re, double[] im)
    {
        var n = re.Length;
        for (int i = 1, j = 0; i < n; i++)
        {
            var bit = n >> 1;
            for (; (j & bit) != 0; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j)
            {
                (re[i], re[j]) = (re[j], re[i]);
                (im[i], im[j]) = (im[j], im[i]);
            }
        }
        for (var len = 2; len <= n; len <<= 1)
        {
            var ang = -2 * Math.PI / len;
            var wRe = Math.Cos(ang);
            var wIm = Math.Sin(ang);
            for (var i = 0; i < n; i += len)
            {
                double curRe = 1, curIm = 0;
                for (var k = 0; k < len / 2; k++)
                {
                    var uRe = re[i + k];
                    var uIm = im[i + k];
                    var vRe = re[i + k + len / 2] * curRe - im[i + k + len / 2] * curIm;
                    var vIm = re[i + k + len / 2] * curIm + im[i + k + len / 2] * curRe;
                    re[i + k] = uRe + vRe;
                    im[i + k] = uIm + vIm;
                    re[i + k + len / 2] = uRe - vRe;
                    im[i + k + len / 2] = uIm - vIm;
                    var nextRe = curRe * wRe - curIm * wIm;
                    curIm = curRe * wIm + curIm * wRe;
                    curRe = nextRe;
                }
            }
        }
    }

    public void Dispose()
    {
        _cts.Cancel();
        _pump.Join(TimeSpan.FromSeconds(2));
        _cts.Dispose();
    }

    // ── COM ────────────────────────────────────────────────────────────────

    private const int CLSCTX_ALL = 23;
    private const int AUDCLNT_SHAREMODE_SHARED = 0;
    private const int AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000;
    private const int AUDCLNT_BUFFERFLAGS_SILENT = 0x2;

    private static readonly Guid KSDATAFORMAT_SUBTYPE_IEEE_FLOAT =
        new("00000003-0000-0010-8000-00aa00389b71");

    private enum EDataFlow { Render = 0, Capture = 1, All = 2 }
    private enum ERole { Console = 0, Multimedia = 1, Communications = 2 }

    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    private class MMDeviceEnumerator { }

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator
    {
        // EXACTLY ONE placeholder, and the count is the whole interface.
        //
        // A ComImport interface is a vtable by position: every method declared
        // before the one you want is a slot, whether it is named or not. The
        // real order is EnumAudioEndpoints, GetDefaultAudioEndpoint, GetDevice,
        // then the two notification-callback methods. Declaring two
        // placeholders put GetDefaultAudioEndpoint on GetDevice's slot, so the
        // call went to a method whose first argument is a device-id STRING,
        // handed it an enum, and came back with a null device - surfacing five
        // frames later as a NullReferenceException that named nothing useful.
        [PreserveSig] int EnumAudioEndpoints_NotUsed(int dataFlow, int stateMask,
            out IntPtr collection);
        [PreserveSig] int GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role,
            out IMMDevice device);
    }

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice
    {
        [PreserveSig] int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
            [MarshalAs(UnmanagedType.IUnknown)] out object instance);
    }

    [ComImport, Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioClient
    {
        [PreserveSig] int Initialize(int shareMode, int streamFlags, long bufferDuration,
            long periodicity, IntPtr format, IntPtr audioSessionGuid);
        [PreserveSig] int GetBufferSize(out uint frames);
        [PreserveSig] int GetStreamLatency(out long latency);
        [PreserveSig] int GetCurrentPadding(out uint padding);
        [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, IntPtr closest);
        [PreserveSig] int GetMixFormat(out IntPtr format);
        [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
        [PreserveSig] int Start();
        [PreserveSig] int Stop();
        [PreserveSig] int Reset();
        [PreserveSig] int SetEventHandle(IntPtr handle);
        [PreserveSig] int GetService(ref Guid iid,
            [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }

    [ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioCaptureClient
    {
        [PreserveSig] int GetBuffer(out IntPtr data, out int frames, out int flags,
            out long devicePosition, out long qpcPosition);
        [PreserveSig] int ReleaseBuffer(int frames);
        [PreserveSig] int GetNextPacketSize(out int frames);
    }

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    private struct WAVEFORMATEX
    {
        public short wFormatTag;
        public short nChannels;
        public int nSamplesPerSec;
        public int nAvgBytesPerSec;
        public short nBlockAlign;
        public short wBitsPerSample;
        public short cbSize;
    }
}
