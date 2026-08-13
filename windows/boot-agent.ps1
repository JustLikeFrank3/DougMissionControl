# boot-agent.ps1 - tiny LAN control endpoint on :9107 (Windows), the
# reverse leg of flightsim-boot.sh: lets the Pi send the workstation back
# to Linux.
#
#   GET /status  (and GET /)     -> 200 "windows"
#   GET /reboot?token=<token>    -> 200 "rebooting", reboot in 5 s
#   GET /launch?token=<token>    -> 200 "launching", runs the
#                                   JarvisGreeting task in the logged-on
#                                   user's session (greeting + sim when
#                                   the recorded intent is "sim")
#
# Token: first line of C:\ProgramData\dualboot\boot-agent.token
# (created by windows/setup.ps1, mirrored into /etc/flightsim/boot.env on
# the Pi). Raw TcpListener rather than HttpListener: the latter needs a
# urlacl reservation for non-localhost prefixes, a hand-rolled HTTP/1.1
# response needs nothing. Runs as SYSTEM at startup (FlightSimBootAgent
# task) so a reboot works even before anyone logs on.

$ErrorActionPreference = 'SilentlyContinue'
$token = (Get-Content 'C:\ProgramData\dualboot\boot-agent.token' -TotalCount 1)
if ($token) { $token = $token.Trim() }

# Arming the firmware for one boot of GRUB is how /reboot reaches Linux now
# that a cold power-on is pointed at Windows. Copied beside this script by
# setup.ps1; if it is missing, /reboot falls back to a plain reboot, which
# is what this agent always used to do.
. "$PSScriptRoot\efi-entry.ps1"

$logFile = "$PSScriptRoot\boot-agent.log"
function Write-AgentLog($msg) {
    try { "$(Get-Date -Format s) $msg" | Add-Content $logFile } catch { }
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, 9107)
$listener.Start()

while ($true) {
    $client = $listener.AcceptTcpClient()
    $reboot = $false
    $launch = $false
    $off = $false
    try {
        $stream = $client.GetStream()
        $stream.ReadTimeout = 3000
        $reader = New-Object System.IO.StreamReader($stream)
        $request = $reader.ReadLine()
        while (($line = $reader.ReadLine()) -and $line -ne '') { }  # drain headers

        $status = '404 Not Found'; $body = 'not found'
        # "/" answers as well as "/status": the Pi's win_up() probe hits the
        # root path, so WIN_PORT can point here instead of at the exporter.
        # Mirrors linux/boot-agent.py. Both are reads, so both stay open.
        if ($request -match '^GET /(status)?(\s|\?)') {
            $status = '200 OK'; $body = 'windows'
        } elseif ($request -match '^GET /(reboot|launch|shutdown)\?token=([^ ]+)') {
            if ($token -and ($Matches[2] -ceq $token)) {
                $status = '200 OK'
                # /shutdown, not just /reboot. The panel could start this
                # machine and send it to the other OS but never stop it, so the
                # desk had a touchscreen that could only ever turn things ON.
                switch ($Matches[1]) {
                    'reboot'   {
                        # Point the firmware at GRUB for exactly one boot.
                        # Reboot regardless of the outcome: where the
                        # firmware order was never changed, GRUB's saved
                        # default is still Linux and a plain reboot lands
                        # there - the behaviour this agent shipped with.
                        $armed = $false
                        if (Get-Command Set-NextBootLinux -ErrorAction SilentlyContinue) {
                            $armed = Set-NextBootLinux
                        }
                        if (-not $armed) {
                            Write-AgentLog 'reboot: no Linux firmware entry - relying on the GRUB saved default'
                        }
                        $body = 'rebooting'; $reboot = $true
                    }
                    'launch'   { $body = 'launching';    $launch = $true }
                    'shutdown' { $body = 'powering off'; $off    = $true }
                }
            } else {
                $status = '403 Forbidden'; $body = 'forbidden'
            }
        }

        $bytes = [System.Text.Encoding]::UTF8.GetBytes($body + "`n")
        $head = "HTTP/1.1 $status`r`nContent-Type: text/plain`r`nContent-Length: $($bytes.Length)`r`nConnection: close`r`n`r`n"
        $headBytes = [System.Text.Encoding]::ASCII.GetBytes($head)
        $stream.Write($headBytes, 0, $headBytes.Length)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
    } catch { } finally { $client.Close() }

    if ($reboot) { & shutdown /r /t 5 /f }
    # /t 5 like the reboot: long enough for the reply to leave the wire, short
    # enough that nobody wonders whether the tap registered.
    if ($off) { & shutdown /s /t 5 /f }
    if ($launch) { Start-ScheduledTask -TaskName 'JarvisGreeting' }
}
