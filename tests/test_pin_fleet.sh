#!/usr/bin/env bash
# pi/pin-fleet.sh against throwaway files and a stand-in `tailscale`: what it
# writes, that a second run and `off` leave the files as they should be, and
# that it changes nothing when it cannot do the whole job.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pin="$repo_dir/pi/pin-fleet.sh"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

fail() { echo "$1" >&2; exit 1; }

# A stand-in for Tailscale: it knows the machines listed in $tmpdir/tailnet.
mkdir "$tmpdir/bin"
cat > "$tmpdir/bin/tailscale" <<EOF
#!/usr/bin/env bash
[ "\$1" = ip ] && [ "\$2" = -4 ] || exit 2
a="\$(sed -n "s/^\$3 //p" "$tmpdir/tailnet")"
[ -n "\$a" ] || { echo "no such peer" >&2; exit 1; }
echo "\$a"
EOF
chmod +x "$tmpdir/bin/tailscale"
export PATH="$tmpdir/bin:$PATH"
printf 'douglas 100.64.0.11\ndougvoice 100.101.102.103\n' > "$tmpdir/tailnet"

export PIN_HOSTS="$tmpdir/hosts" PIN_TEMPLATE="$tmpdir/hosts.tmpl" PIN_CONF="$tmpdir/boot.env"
plain() { printf '127.0.1.1 node1 node1\n127.0.0.1 localhost\n\n::1 localhost ip6-localhost\n\n' > "$PIN_HOSTS"; }
: > "$PIN_CONF"

# --- a hosts file nobody rebuilds: the pins go in, and only there
plain
cp "$PIN_HOSTS" "$tmpdir/before"
out="$(bash "$pin")"
grep -qxF "100.64.0.11 douglas" "$PIN_HOSTS" || fail "douglas was not pinned: $(cat "$PIN_HOSTS")"
grep -qxF "100.101.102.103 dougvoice" "$PIN_HOSTS" || fail "dougvoice was not pinned"
[ "$(head -5 "$PIN_HOSTS")" = "$(head -5 "$tmpdir/before")" ] || fail "the lines that were there changed"
[ ! -e "$PIN_TEMPLATE" ] || fail "a template appeared from nowhere"
[[ "$out" == *"Nothing needs restarting"* ]] || fail "the first run did not say it pinned: $out"

# --- a second run changes nothing, and says so
cp "$PIN_HOSTS" "$tmpdir/pinned"
out="$(bash "$pin")"
cmp -s "$PIN_HOSTS" "$tmpdir/pinned" || fail "a second run changed the file"
[[ "$out" == *"Nothing was changed"* ]] || fail "a second run did not say it changed nothing: $out"

# --- a machine with a new address is pinned at the new one, once
printf 'douglas 100.99.0.5\ndougvoice 100.101.102.103\n' > "$tmpdir/tailnet"
bash "$pin" > /dev/null
grep -qxF "100.99.0.5 douglas" "$PIN_HOSTS" || fail "the new address was not pinned"
[ "$(grep -c ' douglas$' "$PIN_HOSTS")" = 1 ] || fail "douglas is pinned twice"
[ "$(grep -c '^# >>> flight deck' "$PIN_HOSTS")" = 1 ] || fail "there are two blocks"

# --- off gives back the file as it was, to the byte
bash "$pin" off > /dev/null
cmp -s "$PIN_HOSTS" "$tmpdir/before" || fail "off did not give the file back: $(diff "$tmpdir/before" "$PIN_HOSTS")"
out="$(bash "$pin" off)"
[[ "$out" == *"no pins"* ]] || fail "off with nothing pinned did not say so: $out"

# --- a machine Tailscale does not know: nothing is changed, not even for the other
printf 'douglas 100.64.0.11\n' > "$tmpdir/tailnet"
if bash "$pin" > /dev/null 2> "$tmpdir/err"; then fail "an unknown machine was let through"; fi
cmp -s "$PIN_HOSTS" "$tmpdir/before" || fail "a failed run changed the file"
grep -q "dougvoice" "$tmpdir/err" || fail "the failure did not name the machine: $(cat "$tmpdir/err")"

# --- an answer that is not a tailnet address is not pinned
printf 'douglas 100.64.0.11\ndougvoice 192.168.1.20\n' > "$tmpdir/tailnet"
if bash "$pin" > /dev/null 2>&1; then fail "a LAN address was pinned"; fi
printf 'douglas 100.64.0.11\ndougvoice 100.128.0.1\n' > "$tmpdir/tailnet"
if bash "$pin" > /dev/null 2>&1; then fail "an address outside the tailnet range was pinned"; fi
cmp -s "$PIN_HOSTS" "$tmpdir/before" || fail "a refused address changed the file"
printf 'douglas 100.64.0.11\ndougvoice 100.101.102.103\n' > "$tmpdir/tailnet"

# --- a hosts file cloud-init rebuilds at boot: the template gets the pins too
printf "# Your system has configured 'manage_etc_hosts' as True.\n127.0.1.1 node1 node1\n127.0.0.1 localhost\n" > "$PIN_HOSTS"
printf "## template:jinja\n# Your system has configured 'manage_etc_hosts' as True.\n127.0.1.1 {{fqdn}} {{hostname}}\n127.0.0.1 localhost\n" > "$PIN_TEMPLATE"
cp "$PIN_HOSTS" "$tmpdir/before"; cp "$PIN_TEMPLATE" "$tmpdir/tmpl-before"
out="$(bash "$pin")"
for f in "$PIN_HOSTS" "$PIN_TEMPLATE"; do
    grep -qxF "100.64.0.11 douglas" "$f" || fail "douglas is not pinned in $f"
    grep -qxF "100.101.102.103 dougvoice" "$f" || fail "dougvoice is not pinned in $f"
done
grep -qxF "127.0.1.1 {{fqdn}} {{hostname}}" "$PIN_TEMPLATE" || fail "the template's own lines changed"
# Nothing in the block that a template engine would take for its own.
if sed -n '/^# >>> flight deck/,/^# <<< flight deck/p' "$PIN_TEMPLATE" | grep -q '[{}%$]'; then
    fail "the block holds a character a template would act on"
fi
[[ "$out" == *"cloud-init"* ]] || fail "it did not say the template was written: $out"
bash "$pin" off > /dev/null
cmp -s "$PIN_HOSTS" "$tmpdir/before" || fail "off did not give the managed hosts file back"
cmp -s "$PIN_TEMPLATE" "$tmpdir/tmpl-before" || fail "off did not give the template back"

# --- a template that is not in use is left alone
plain
bash "$pin" > /dev/null
cmp -s "$PIN_TEMPLATE" "$tmpdir/tmpl-before" || fail "a template nobody uses was written to"
bash "$pin" off > /dev/null
rm -f "$PIN_TEMPLATE"

# --- the names are the deck's own: boot.env says what they are
printf 'DOUGLAS_HOST="brain"\nVOICE_HOST=100.64.0.9\n' > "$PIN_CONF"
printf 'brain 100.64.0.77\n' > "$tmpdir/tailnet"
plain
out="$(bash "$pin")"
grep -qxF "100.64.0.77 brain" "$PIN_HOSTS" || fail "the name from boot.env was not pinned"
if grep -q "100.64.0.9" "$PIN_HOSTS"; then fail "a host given as an address was pinned"; fi
[[ "$out" == *"VOICE_HOST is not a short name"* ]] || fail "it did not say why VOICE_HOST was left out: $out"
: > "$PIN_CONF"
printf 'douglas 100.64.0.11\ndougvoice 100.101.102.103\n' > "$tmpdir/tailnet"

# --- a file with no newline at its end: the block still starts on its own line
printf '127.0.0.1 localhost' > "$PIN_HOSTS"
bash "$pin" > /dev/null
[ "$(sed -n 1p "$PIN_HOSTS")" = "127.0.0.1 localhost" ] || fail "the block ran into the last line"
grep -qxF "100.64.0.11 douglas" "$PIN_HOSTS" || fail "nothing was pinned in a file with no final newline"

# --- half a block is somebody's accident: refused, and left as it is
plain
echo "# >>> flight deck: fleet machines by their Tailscale addresses (pi/pin-fleet.sh)" >> "$PIN_HOSTS"
cp "$PIN_HOSTS" "$tmpdir/half"
if bash "$pin" > /dev/null 2>&1; then fail "half a block was built on"; fi
if bash "$pin" off > /dev/null 2>&1; then fail "half a block was removed as if it were whole"; fi
cmp -s "$PIN_HOSTS" "$tmpdir/half" || fail "a refused run changed the file"

# --- nothing is left lying beside the files
[ -z "$(find "$tmpdir" -maxdepth 1 -name 'hosts.??????' -o -maxdepth 1 -name 'hosts.tmpl.??????')" ] \
    || fail "a temporary file was left behind: $(ls "$tmpdir")"

# --- a file it may not write is said to need sudo
plain; chmod a-w "$PIN_HOSTS"
if [ -w "$PIN_HOSTS" ]; then
    : # running as root: every file is writable, and there is nothing to refuse
elif bash "$pin" > /dev/null 2> "$tmpdir/err"; then
    fail "a read-only hosts file was accepted"
else
    grep -q "sudo" "$tmpdir/err" || fail "the refusal did not mention sudo: $(cat "$tmpdir/err")"
fi

echo "pin-fleet tests passed"
