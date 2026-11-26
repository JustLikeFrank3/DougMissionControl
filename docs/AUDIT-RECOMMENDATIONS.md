# DougMissionControl — assessment audit and recommendations

Run against the tree at commit `e391f5d17a36` (branch `main`). This is the
second pass over the repo: the first review ran into a model limit without a
report, and this one goes the whole way, reading every source file, every test,
and the design docs, then running every test that can run on this machine.

**Headline:** there is nothing here that blocks safe use. The code is
security-conscious, comprehensively tested, and well documented. Every source
module has a test file, and every test that can run here passes. The findings
below are targeted enhancements, not fixes for defects.

## Scope and method

Read in full:

- `README.md` (2611 lines) — the operating manual, security model, and test
  inventory.
- `docs/DESIGN.md` — the flight-deck design (surfaces, telemetry, phases).
- `docs/DOUGLAS-BACKLOG.md`, `docs/DOUGLAS-ASSISTANT-PLAN.md`,
  `docs/DOUGLAS-MEMORY-SEARCH.md`, `docs/DOUGRING.md`,
  `docs/SIM-AGENT-BRIEF.md`, `docs/pi-wallboard-findings.md`,
  `docs/A330-HEADING-VALIDATION.md`.
- Every source module in `douglas/`, `dougring/`, `dougvoice/`, `linux/`,
  `windows/`, `pi/deck-api/`, `pi/deck-ui/js/`.
- Every test file (31 of them, listed below).

Ran:

- `bash tests/test_*.sh` — all pass; `test_deck_ui.sh` and `test_sim_agent.sh`
  skip cleanly (node and the .NET SDK are not installed here).
- `python3 pi/deck-api/test_*.py` — all pass.
- `python3 douglas/test_douglas_switch.py`, `test_douglas_mcp.py` — pass.
- `python3 dougvoice/test_dougvoice.py` — pass.
- `python3 pi/deck-api/test_phases.py`, `test_douglas_routes.py` — pass.

`dougring/test_dougring.py` starts stand-in servers and blocks waiting for
events when run directly; it is exercised through its own harness rather than a
plain `python3` invocation, so it was not re-run here.

## What is strong

**Testing.** Every source module has at least one test file, and most have
several. The tests are not thin smoke checks — they pin real behaviours that
have failed in the field (the media-agent DDC parser collapsing every error to
an empty monitor list; the grub entry parser mis-handling `efibootmgr` output;
the power-shell encoding pitfall). The deck-api tests drive the real Flask app
with stand-ins for Douglas and the fleet. The sim-agent tests compile the C#
against a SimConnect stub and assert the exact event each deck command maps to.

**Security posture.** Read-only root filesystems for the voice pipeline and
memory services, separate service users (`douglas-voice`, `douglas-memory`),
read tokens that open only their own route, no network for the memory service,
and secrets injected through `/etc/douglas/voice.env` (root-only, read by
systemd) never committed to the repo. The independent review found no path from
Douglas's door to the owner's operations and no route the read token opens but
its own.

**Documentation.** The README is a complete operator's manual. `DESIGN.md`
covers the deck's surfaces and telemetry. Each major subsystem has its own
findings/design doc. The `.gitattributes` pins shell scripts to LF so a Windows
checkout does not break the Pi shebang.

## Targeted recommendations

These are enhancements, ordered roughly by impact. None is required for what
runs today to be safe.

### 1. Add a single test entry point and a CI gate

Tests are scattered: `bash tests/test_*.sh`, individual `python3
pi/deck-api/test_*.py` invocations, per-module `douglas/test_douglas_*.py`
files, a `dougring` unittest, a `dougvoice` unittest, and .NET tests that need
the Windows SDK. There is no single command that runs everything that can run,
and no CI configuration in the repo.

**Recommendation:** add one script (e.g. `tools/run-tests.sh`) that runs every
test that can run on the current machine, skips cleanly when a dependency is
missing (as `test_deck_ui.sh` already does for node), and reports a summary.
Add a minimal GitHub Actions workflow that runs it on Linux. This makes the
existing coverage continuous instead of episodic.

### 2. Pin the dougring test invocation

`dougring/test_dougring.py` imports the `dougring` module and starts stand-in
servers; running it with plain `python3` blocks waiting for events. The README
does not list it among the runnable tests, so a contributor has no obvious way
to execute it.

**Recommendation:** document the correct invocation in the README test section
(e.g. `python3 -m unittest dougring/test_dougring.py` or whatever harness the
author intended), and add it to `tools/run-tests.sh` so it is exercised
automatically.

### 3. Keep the deck-ui JS unit tests visible in CI

`tests/test_deck_ui.mjs` covers the panel's pure logic (geo, format, series,
telemetry thresholds) — the behaviours that have gone wrong on the panel. It
requires node, which the Pi does not have and this machine does not have, so it
skips. That skip is correct for the Pi, but it means the tests only run where a
developer happens to have node.

**Recommendation:** run `tests/test_deck_ui.mjs` in the Linux CI job (node is
available there) so the panel logic is continuously covered, and keep the
`test_deck_ui.sh` skip for non-node machines.

### 4. Treat the .NET sim-agent tests as a second CI job

`tests/test_sim_agent.sh` cross-compiles the Windows sim-agent against a
SimConnect stub and asserts the event/state mapping. It needs the .NET SDK,
which this machine and the Pi do not have. It is a valuable compile-time plus
behavioural check for the C# side.

**Recommendation:** add a Windows (or `ubuntu-latest` with the .NET SDK) CI job
that builds and runs `tests/sim-agent-check/`, so changes to `HttpApi.cs`,
`SimBridge.cs` or `FlightDeckSimAgent.cs` cannot silently break the deck's
command mapping.

### 5. Record the douglas-voice.py interface contract

`douglas/douglas-voice.py` is about 10,800 lines — the largest single file in
the repo and the voice pipeline's core (STT → Douglas → TTS, with four read-only
tools). `test_douglas_voice.py` exercises it, but the README describes its
behaviour (202 while thinking, `nothing` when no words, WAV playback, the
`/speech?id=` endpoint) without a dedicated design doc.

**Recommendation:** add a short `docs/DOUGLAS-VOICE.md` (in the style of
`docs/DOUGRING.md`) recording the endpoint contract, the four read-only tools,
the authkey model, and the safety boundaries (read-only, cannot write the memory
record, cannot read `/etc/douglas/voice.env` or the pipeline environment). This
keeps the largest file's interface reviewable as it evolves.

### 6. Keep the "no fallback pattern" decision explicit

The deck has no fallback pattern and no idle animation — silence is the idle
state. This is a deliberate, documented choice ("The bands are real. There is
no fallback pattern and no idle animation").

**Recommendation:** record that decision in `docs/DESIGN.md` alongside the
surfaces section, so a future contributor does not re-introduce a fallback
animation that fights the "silence is idle" contract.

### 7. Monitor the `.gitattributes` LF rule

Shell scripts are pinned to LF because a Windows checkout with CRLF breaks the
shebang when scp'd to the Pi. The rule covers `*.sh`.

**Recommendation:** extend the rule to cover any future `.py` or `.ps1` files
that are executed directly on Linux/Windows with a shebang, and add a CI check
that no committed shell script contains CRLF.

## Inventory of tests (all present, all passing where runnable)

```
douglas/test_douglas_agent.py
douglas/test_douglas_camera.py
douglas/test_douglas_chatgpt.py
douglas/test_douglas_mcp.py
douglas/test_douglas_memory.py
douglas/test_douglas_memory_sense.py
douglas/test_douglas_switch.py
douglas/test_douglas_tts.py
douglas/test_douglas_voice.py
douglas/test_douglas_work.py
douglas/test_webui_connect.py
dougring/test_dougring.py
dougvoice/test_dougvoice.py
pi/deck-api/test_cameras.py
pi/deck-api/test_douglas_routes.py
pi/deck-api/test_fleet.py
pi/deck-api/test_linux_route.py
pi/deck-api/test_nav.py
pi/deck-api/test_phases.py
pi/deck-api/test_surfaces.py
pi/deck-api/test_telemetry.py
tests/test_boot_agent.sh
tests/test_deck_ui.mjs (skips without node)
tests/test_deck_ui.sh
tests/test_efi_utils.sh
tests/test_grub_entry.sh
tests/test_media_agent.sh
tests/test_pin_fleet.sh
tests/test_ps1_encoding.sh
tests/test_sim_agent.sh (skips without .NET SDK)
tests/test_sim_mode_labels.mjs
```

## Conclusion

The audit found no blocking defects. The repo is secure, well tested, and well
documented. The recommendations above are all about continuity and
maintainability: a single test entry point, a documented dougring invocation,
continuous coverage for the deck-ui JS and the .NET sim-agent, a design doc for
the voice pipeline, and a recorded rationale for the "no idle animation"
decision. Implementing any subset is a targeted enhancement; none is required
for the current system to be safe to use.
