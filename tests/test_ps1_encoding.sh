#!/usr/bin/env bash
# Every .ps1 in this repo must be pure ASCII.
#
# Windows PowerShell 5.1 reads a .ps1 with no byte-order mark as ANSI, not
# UTF-8. A UTF-8 em-dash is E2 80 94, and in code page 1252 that last byte is
# U+201D, a smart closing quote -- which PowerShell accepts as a string
# delimiter. So an em-dash inside a double-quoted string silently ENDS the
# string mid-sentence and everything after it becomes loose tokens:
#
#   throw "no display matches '$Match' - run set-primary-display.ps1 -List"
#             becomes
#   Unexpected token 'run' in expression or statement.
#
# The error points at a line that is perfectly correct, names a token the
# author never wrote, and cascades into a dozen more "missing closing brace"
# errors further down the file. It cost an evening.
#
# A BOM would also fix it, but BOMs get stripped by editors, lost by
# copy-paste, and are invisible in review. Staying ASCII cannot be undone by
# accident, and this test is what makes that stick. The scheduled tasks run
# powershell.exe (5.1), so PowerShell 7's UTF-8 default does not save us.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$repo_dir" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
failures = []

for path in sorted(root.rglob("*.ps1")):
    raw = path.read_bytes()
    rel = path.relative_to(root)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        failures.append(f"{rel}: not valid UTF-8 either, so this needs a human")
        continue
    # Report the CHARACTER and its line, not each byte of it: an em-dash is
    # three bytes and naming all three helps nobody find it.
    offenders = sorted({(n, ch)
                        for n, line in enumerate(text.splitlines(), 1)
                        for ch in line if ord(ch) > 127})
    if offenders:
        where = ", ".join(f"line {n} {ch!r}" for n, ch in offenders)
        failures.append(f"{rel}: {where}")

if failures:
    print("FAIL: non-ASCII in PowerShell files", file=sys.stderr)
    for f in failures:
        print("  - " + f, file=sys.stderr)
    print("", file=sys.stderr)
    print("  Windows PowerShell 5.1 reads these as code page 1252. An em-dash's",
          file=sys.stderr)
    print("  last byte becomes a smart quote and terminates the enclosing string.",
          file=sys.stderr)
    print("  Use plain ASCII: '-' for a dash, '\"' for a quote.", file=sys.stderr)
    raise SystemExit(1)

print("PowerShell files are ASCII-clean")
PY
