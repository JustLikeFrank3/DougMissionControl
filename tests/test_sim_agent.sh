#!/usr/bin/env bash
# Builds the Windows sim agent against its SimConnect stub and exercises the
# two halves a compiler cannot check: which sim event a Flight Deck command
# resolves to, and the shape of the state the panel reads back.
#
# Runs on Linux. The agent targets net8.0-windows, which cross-compiles fine
# with EnableWindowsTargeting; the harness itself is plain net8.0 and reaches
# into the built assembly by reflection, so nothing here needs a Windows host
# or the MSFS SDK. It proves this project's own arithmetic only — it says
# nothing about whether MSFS honours the events, which only the aeroplane can.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
agent="$repo_dir/windows/sim-agent"
harness="$repo_dir/tests/sim-agent-check"

if ! command -v dotnet >/dev/null 2>&1; then
    echo "SKIP: sim-agent tests need the .NET SDK (not installed here)"
    exit 0
fi

export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1

# The stub build has its own output tree, so this cannot leave a stub exe
# sitting where a real one belongs.
dotnet build "$agent/FlightDeckSimAgent.csproj" \
    -p:SimConnectStub=true -p:EnableWindowsTargeting=true \
    -v quiet --nologo >/dev/null

dll="$(find "$agent/bin/stub" -name 'flightdeck-sim-agent.dll' -print -quit)"
if [ -z "$dll" ]; then
    echo "FAIL: sim-agent stub build produced no assembly"
    exit 1
fi

exec dotnet run --project "$harness/check.csproj" -v quiet -- "$dll"
