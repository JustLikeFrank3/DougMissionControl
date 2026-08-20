# llama-logon.ps1 - start the llama.cpp server at logon: the Windows mirror
# of the llama-server@<profile> systemd unit on the Linux boot, so :8081
# answers under either OS.
#
# Config is C:\ProgramData\dualboot\llama.env (KEY=VALUE, # comments), the
# Windows counterpart of /etc/llama-server/{common,<profile>}.env. Values are
# Windows-native: LLAMA_BIN and MODEL are absolute Windows paths, and any
# model path inside PROFILE_ARGS must be too - the Linux profile strings
# cannot be pasted across unchanged.
#
# Until LLAMA_BIN and MODEL point at files that exist, this exits silently.
# An unconfigured or half-installed llama must never hold up logon, and must
# never be the reason a boot looks broken.
#
# SKIP_ON_INTENTS (comma-separated, e.g. "sim,squadrons") plus PI_HOST make
# the start intent-aware: a boot triggered for a game keeps its VRAM and
# llama stays down until the deck's LOCAL MODEL control asks for it.
#
# Registered as the LlamaServer logon task by windows/setup.ps1.

$ErrorActionPreference = 'SilentlyContinue'

$envFile = Join-Path $env:ProgramData 'dualboot\llama.env'
if (-not (Test-Path $envFile)) { exit 0 }

$cfg = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
        $cfg[$Matches[1]] = $Matches[2].Trim('"')
    }
}

$bin   = $cfg['LLAMA_BIN']
$model = $cfg['MODEL']
if (-not $bin   -or -not (Test-Path $bin))   { exit 0 }
if (-not $model -or -not (Test-Path $model)) { exit 0 }

# The GPU belongs to whichever intent booted this machine. A boot asked for
# by a game trigger must not spend its VRAM on the model, so this reads the
# same /tmp/flightsim-intent the greeting reads, with the same freshness
# rule - the two scripts can never disagree about what kind of boot this is.
# Absent, stale, or unreadable intent is a plain boot, and plain boots get
# llama; that is also the case where the greeting launches nothing, so the
# model and a game never contend for the card. SKIP_ON_INTENTS or PI_HOST
# unset keeps the old always-start behaviour.
#
# The deck's LOCAL MODEL control overrides the intent: quitting a game and
# asking for llama back IS the override, and the boot agent marks it by
# touching llama-start.requested just before starting this task. Freshness
# instead of deletion because the agent runs as SYSTEM and this script as
# the logon user - consuming a SYSTEM-owned file would need ACL surgery,
# letting it age out needs nothing.
$flag = Join-Path $env:ProgramData 'dualboot\llama-start.requested'
$forced = (Test-Path $flag) -and
    (((Get-Date) - (Get-Item $flag).LastWriteTime).TotalSeconds -lt 120)
$skipIntents = @(($cfg['SKIP_ON_INTENTS'] -split ',') |
    ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $forced -and $skipIntents -and $cfg['PI_HOST']) {
    $intentRaw = & ssh -o BatchMode=yes -o ConnectTimeout=4 $cfg['PI_HOST'] `
        'cat /tmp/flightsim-intent 2>/dev/null'
    if ($intentRaw -match '^(\w+) (\d+)$') {
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$Matches[2]
        if ($age -lt 1800 -and $skipIntents -contains $Matches[1]) { exit 0 }
    }
}

# Not $host: that is a PowerShell automatic variable.
$listenAddr = if ($cfg['HOST'])      { $cfg['HOST'] }      else { '0.0.0.0' }
$port       = if ($cfg['PORT'])      { $cfg['PORT'] }      else { '8081' }
$modelAlias = if ($cfg['ALIAS'])     { $cfg['ALIAS'] }     else { 'qwen3.8-27b' }
$reasoning  = if ($cfg['REASONING']) { $cfg['REASONING'] } else { 'medium' }

# Already serving? Leave it alone. The model takes nearly all the VRAM, so a
# second copy could only fail to bind and churn - the same reason the Linux
# side allows just one llama-server instance at a time.
try {
    $probe = New-Object System.Net.Sockets.TcpClient
    if ($probe.ConnectAsync('127.0.0.1', [int]$port).Wait(1000)) {
        $probe.Close()
        exit 0
    }
    $probe.Close()
} catch { }

$argList = @(
    '-m', $model, '-ngl', '99',
    '--host', $listenAddr, '--port', $port,
    '--alias', $modelAlias,
    '--reasoning-effort', $reasoning,
    '--metrics'
)
# The systemd unit leaves $PROFILE_ARGS unquoted so systemd word-splits it;
# this is that split, done explicitly.
if ($cfg['PROFILE_ARGS']) {
    $argList += ($cfg['PROFILE_ARGS'] -split '\s+' | Where-Object { $_ })
}

Start-Process -FilePath $bin -ArgumentList $argList -WindowStyle Hidden
exit 0
