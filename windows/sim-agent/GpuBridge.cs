// GpuBridge.cs - GPU telemetry this project owns.
//
// The panel's DECK gauges used to come only from a Prometheus exporter that
// belongs to a different project (jobContext's gpu-exporter.ps1 on :9106).
// That exporter has now died three times, and each time it took the gauges
// with it while the machine underneath was perfectly healthy - a panel
// reporting "no data" about a GPU that is sitting there at 47 degrees.
//
// This agent is already running whenever Windows is, already token-guarded,
// and already on the box where the GPU is. nvidia-smi ships with the driver
// and jarvis-greeting.ps1 has been shelling out to it for the greeting since
// the beginning. So the numbers were always available; nothing was asking.
//
// This is NOT a metrics stack. There is no retention, no scrape history and
// no second Prometheus - just the current reading, served in exposition format
// because deck-api already knows how to parse that, which makes the whole
// change on the Pi side a URL preference rather than a new parser.
//
// Sampled on a timer rather than per request: nvidia-smi takes 50-300ms to
// answer and /metrics must never be the slow endpoint that makes a caller
// think the agent has hung.

using System.Diagnostics;
using System.Globalization;

namespace FlightDeckSimAgent;

internal sealed class GpuBridge : IDisposable
{
    // Slower than the panel's poll on purpose. GPU temperature does not move
    // meaningfully inside two seconds, and every sample is a process spawn.
    private const int SampleMs = 2000;

    private readonly CancellationTokenSource _cts = new();
    private readonly Thread _pump;
    private readonly object _lock = new();

    private Sample? _last;
    private string? _why;          // why there is no reading, for the panel

    private sealed record Sample(double TempC, double UtilPct, double MemUsedMb,
                                 double MemTotalMb, string Name);

    public GpuBridge()
    {
        _pump = new Thread(Pump) { IsBackground = true, Name = "gpu-sample" };
    }

    public void Start() => _pump.Start();

    private void Pump()
    {
        while (!_cts.IsCancellationRequested)
        {
            var (sample, why) = Read();
            lock (_lock) { _last = sample; _why = why; }
            _cts.Token.WaitHandle.WaitOne(SampleMs);
        }
    }

    /// <summary>
    /// One nvidia-smi query. Returns null with a reason rather than throwing:
    /// a box with no NVIDIA card is a normal deployment, not a fault, and the
    /// panel renders a missing reading as a gap either way.
    /// </summary>
    private static (Sample?, string?) Read()
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "nvidia-smi",
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            psi.ArgumentList.Add("--query-gpu=temperature.gpu,utilization.gpu,"
                               + "memory.used,memory.total,name");
            psi.ArgumentList.Add("--format=csv,noheader,nounits");

            using var p = Process.Start(psi);
            if (p is null) return (null, "nvidia-smi did not start");
            var text = p.StandardOutput.ReadToEnd();
            // Bounded: a hung nvidia-smi must not park this thread forever, and
            // it must not be left running either.
            if (!p.WaitForExit(4000))
            {
                try { p.Kill(entireProcessTree: true); } catch { /* already gone */ }
                return (null, "nvidia-smi did not answer within 4s");
            }
            if (p.ExitCode != 0) return (null, $"nvidia-smi exited {p.ExitCode}");

            // First GPU only. This desk has one, and a panel with one set of
            // gauges cannot honestly show two.
            var line = text.Split('\n').FirstOrDefault(l => l.Trim().Length > 0);
            if (line is null) return (null, "nvidia-smi returned nothing");

            var f = line.Split(',');
            if (f.Length < 5) return (null, "nvidia-smi returned an unexpected shape");

            double N(int i) => double.TryParse(f[i].Trim(), NumberStyles.Float,
                CultureInfo.InvariantCulture, out var v) ? v : double.NaN;

            var s = new Sample(N(0), N(1), N(2), N(3), f[4].Trim());
            // "[N/A]" is what nvidia-smi prints for a field a card will not
            // report, and it parses to NaN. A NaN in exposition format is
            // worse than an absent metric, so refuse the whole sample only if
            // the two the panel actually draws are missing.
            if (double.IsNaN(s.TempC) && double.IsNaN(s.UtilPct))
                return (null, "nvidia-smi reported no temperature or utilisation");
            return (s, null);
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return (null, "nvidia-smi is not installed");
        }
        catch (Exception e)
        {
            return (null, $"nvidia-smi failed: {e.GetType().Name}");
        }
    }

    /// <summary>
    /// The latest reading in Prometheus exposition format, or empty when there
    /// is nothing to report.
    ///
    /// Exposition format rather than JSON purely so deck-api needs no new
    /// parser: it already scrapes and canonicalises this shape, and these names
    /// are chosen to match the patterns it looks for.
    /// </summary>
    public string Exposition()
    {
        Sample? s;
        string? why;
        lock (_lock) { s = _last; why = _why; }

        if (s is null)
        {
            // A comment, not a metric. A scraper reads this as "no samples",
            // which is the truth, while a human reading the endpoint by hand
            // gets told why.
            return $"# no GPU reading: {why ?? "not sampled yet"}\n";
        }

        var sb = new System.Text.StringBuilder();
        void Metric(string name, string help, string type, double value)
        {
            if (double.IsNaN(value)) return;   // absent beats a NaN sample
            sb.Append("# HELP ").Append(name).Append(' ').Append(help).Append('\n');
            sb.Append("# TYPE ").Append(name).Append(' ').Append(type).Append('\n');
            sb.Append(name).Append(' ')
              .Append(value.ToString("0.###", CultureInfo.InvariantCulture)).Append('\n');
        }

        Metric("gpu_temperature_celsius", "GPU core temperature.", "gauge", s.TempC);
        Metric("gpu_utilization_percent", "GPU busy percentage.", "gauge", s.UtilPct);
        Metric("gpu_memory_used_bytes", "VRAM in use.", "gauge", s.MemUsedMb * 1024 * 1024);
        Metric("gpu_memory_total_bytes", "VRAM fitted.", "gauge", s.MemTotalMb * 1024 * 1024);
        return sb.ToString();
    }

    /// <summary>The card's name, or empty. For /health, so the panel can say
    /// whose numbers these are.</summary>
    public string Name()
    {
        lock (_lock) return _last?.Name ?? "";
    }

    public void Dispose()
    {
        _cts.Cancel();
        _pump.Join(TimeSpan.FromSeconds(2));
        _cts.Dispose();
    }
}
