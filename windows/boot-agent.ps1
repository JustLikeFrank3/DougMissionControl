# boot-agent.ps1 — tiny LAN control endpoint on :9107 (Windows), the
# reverse leg of flightsim-boot.sh: lets the Pi reboot Windows so GRUB's
# saved default lands the workstation in Linux.
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

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, 9107)
$listener.Start()

while ($true) {
    $client = $listener.AcceptTcpClient()
    $reboot = $false
    $launch = $false
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
        } elseif ($request -match '^GET /(reboot|launch)\?token=([^ ]+)') {
            if ($token -and ($Matches[2] -ceq $token)) {
                $status = '200 OK'
                if ($Matches[1] -eq 'reboot') { $body = 'rebooting'; $reboot = $true }
                else { $body = 'launching'; $launch = $true }
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
    if ($launch) { Start-ScheduledTask -TaskName 'JarvisGreeting' }
}
