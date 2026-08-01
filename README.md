# Flight sim bootup — "Alexa, flight sim bootup"

Voice-triggered boot of the dual-boot workstation into Windows, from any
state, with a Jarvis-style welcome. Rides the same plumbing the Pi
wallboard already uses to tell which OS is booted.

```
"Alexa, flight sim bootup"
        │
        ├─ Alexa Speaks (routine): "Right away, sir. Bringing the flight deck online."
        └─ turns on virtual device "flight sim"
                │  (fauxmo Wemo emulator on the Pi, 192.168.1.51)
                ▼
        flightsim-boot.sh on the Pi probes the workstation (192.168.1.50):
                │
                ├─ :9106 answers (gpu-exporter)    → Windows already up. Done.
                ├─ :9105 answers (ollama-exporter) → ssh forced-command key →
                │                                    grub-reboot "Windows…" + reboot
                └─ no ping                         → WOL magic packet to
                                                     AA-BB-CC-DD-EE-FF; if GRUB
                                                     default boots Linux first,
                                                     chains through the ssh path
                ▼
        Windows logs on → JarvisGreeting task speaks the welcome
        (+ optionally launches MSFS) and gpu-exporter comes up on :9106,
        which also flips the wallboard to the gaming playlist.
```

## Plain Windows boot: "Alexa, boot into Windows"

A third device, **pc** (port 49917), boots Windows with the greeting but
WITHOUT launching the sim. How: each Windows trigger records its intent
on the Pi (`/tmp/flightsim-intent`: `sim`/`plain` + timestamp);
jarvis-greeting.ps1 reads it back over ssh at logon and launches MSFS
only for a fresh `sim` intent. Manual power-button boots follow
`$SimOnManualBoot` (default off). Saying "flight sim bootup" while
Windows is already up now fires the greeting + sim remotely via the
boot-agent's `/launch` endpoint instead of no-opping.

## The reverse direction: "Alexa, workstation bootup" → Linux

A second fauxmo device, **workstation** (port 49916), boots the machine
into Linux the same way: Linux up → no-op; powered off → WOL (GRUB's
saved default already boots Linux); Windows up → the Pi calls the
token-guarded **boot-agent** (boot-agent.ps1, :9107, SYSTEM startup task)
which plain-reboots Windows into the GRUB default. At Linux logon an
autostart entry speaks a boot confirmation (same neural voice) and opens
VS Code. Alexa routine: phrase "workstation bootup" → Alexa Speaks → turn
on **workstation**. Caveat: if you ever flip the GRUB saved default to
Windows for faster cold sim starts, the Windows→Linux leg stops working
(a plain reboot would land back in Windows).

## One-time setup (in this order)

1. **Windows boot** (elevated PowerShell):
   `powershell -ExecutionPolicy Bypass -File scripts\flightsim\windows-setup.ps1`
   — arms NIC WOL, keeps Fast Startup off, installs the JarvisGreeting
   logon task. Then do the printed BIOS + auto-logon checklist.
2. **Pi** (from either boot; `PI_HOST=user@192.168.1.51` from Windows):
   `./scripts/flightsim/pi-setup.sh`
   — installs fauxmo + the boot orchestrator, prints the Pi's public key.
3. **Linux boot** (sudo, with the key from step 2):
   `sudo ./scripts/flightsim/linux-setup.sh 'ssh-ed25519 AAAA… flightsim-boot@pi'`
   — GRUB_DEFAULT=saved, `boot-to-windows` helper, forced-command
   authorized_keys entry, persistent `ethtool wol g`.
4. **Alexa app**:
   - "Alexa, discover devices" → finds **flight sim**.
   - Routines → **+** → When: *Voice* → "flight sim bootup".
   - Action 1: *Alexa Speaks* → e.g. "Right away, sir. Spinning up the
     flight deck. Systems will be online shortly."
   - Action 2: *Smart Home* → **flight sim** → On.

## Test matrix

| Starting state | Expect |
|---|---|
| Windows up | Alexa ack only; Pi logs "already up" (`journalctl -t flightsim-boot`) |
| Linux up | reboots into Windows in ~30 s, greeting on logon |
| Powered off | WOL powers on; direct to Windows if it's the GRUB default, else one chained Linux→Windows reboot (~2 min) |

Cold-boot speed note: WOL cannot pick a GRUB menu entry. If you want
off→Windows without the chained double boot, make Windows the saved
default on the Linux boot (`sudo grub-set-default 'Windows Boot Manager…'`
— exact title in `boot-to-windows`); Linux then becomes the
pick-at-the-menu OS. Everything works either way, Windows-default is just
faster.

## Troubleshooting

- **Echo won't discover "flight sim"**: fauxmo must answer on the Echo's
  subnet — `systemctl status fauxmo` on the Pi; re-discover. Some newer
  Echo firmwares are picky about Wemo emulation; retry discovery from the
  Alexa app (*Add device → Other → Wemo*).
- **WOL does nothing from full off**: BIOS "Resume by PCI-E/PME" off, or
  ErP deep-off enabled (kills NIC standby power), or the last shutdown was
  from Linux without `flightsim-wol.service` armed. Link LED on the NIC
  while off = standby power present.
- **Machine wakes but greeting/exporter never appear**: nobody logged on —
  both are logon tasks; enable auto sign-in (checklist in
  windows-setup.ps1).
- **Pi can't ssh the Linux boot**: only valid while Linux is up (the
  direct link 192.168.100.1 is Linux-only config). Test:
  `ssh -i ~/.ssh/flightsim_ed25519 user@192.168.100.1 boot` — that reboots
  the workstation into Windows on the spot.
- Orchestrator log: `journalctl -t flightsim-boot` on the Pi (also
  `~/flightsim-boot.log` for the detached run's stdout).

## Security notes

- The Pi holds no workstation credentials: its key is bound in
  `authorized_keys` to `command="sudo /usr/local/bin/boot-to-windows"`
  with `restrict`, and the sudoers rule covers exactly that script — the
  key can reboot the box into Windows and nothing else.
- fauxmo is LAN-only (UPnP discovery + one TCP port on 192.168.1.51);
  "off" is a no-op, `state_cmd` just probes :9106.
- Secrets: none. WOL packets and exporter probes are unauthenticated by
  nature and stay on the LAN.
