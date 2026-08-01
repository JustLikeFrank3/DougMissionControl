# boot-agent.ps1 — tiny LAN control endpoint on :9107 (Windows), the
# reverse leg of flightsim-boot.sh: lets the Pi reboot Windows so GRUB's
# saved default lands the workstation in Linux.
#
#   GET /status                  -> 200 "windows"
#   GET /reboot?token=<token>    -> 200 "rebooting", reboot in 5 s
#
# Token: first line of C:\ProgramData\jobcontext\boot-agent.token (created
# by windows-setup.ps1, mirrored into /etc/flightsim/boot.env on the Pi).
# Raw TcpListener for the same reason as gpu-exporter.ps1: no urlacl
# dance. Runs as SYSTEM at startup (FlightSimBootAgent task) so a reboot
# works even before anyone logs on.

$ErrorActionPreference = 'SilentlyContinue'
$token = (Get-Content 'C:\ProgramData\jobcontext\boot-agent.token' -TotalCount 1)
if ($token) { $token = $token.Trim() }

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, 9107)
$listener.Start()

while ($true) {
    $client = $listener.AcceptTcpClient()
    $reboot = $false
    try {
        $stream = $client.GetStream()
        $stream.ReadTimeout = 3000
        $reader = New-Object System.IO.StreamReader($stream)
        $request = $reader.ReadLine()
        while (($line = $reader.ReadLine()) -and $line -ne '') { }  # drain headers

        $status = '404 Not Found'; $body = 'not found'
        if ($request -match '^GET /status') {
            $status = '200 OK'; $body = 'windows'
        } elseif ($request -match '^GET /reboot\?token=([^ ]+)') {
            if ($token -and ($Matches[1] -ceq $token)) {
                $status = '200 OK'; $body = 'rebooting'; $reboot = $true
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
}
