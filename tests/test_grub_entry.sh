#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_dir/linux/grub_utils.sh"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

cat > "$tmpdir/grub.cfg" <<'EOF'
menuentry "Ubuntu" --class gnu-linux --class gnu --class os {
    linux /boot/vmlinuz
}
menuentry "Windows Boot Manager (UEFI)" {
    chainloader /EFI/Microsoft/Boot/bootmgfw.efi
}
EOF

entry="$(extract_windows_grub_entry "$tmpdir/grub.cfg")"
if [[ "$entry" != "Windows Boot Manager (UEFI)" ]]; then
    echo "expected double-quoted Windows entry, got: $entry" >&2
    exit 1
fi

cat > "$tmpdir/grub-single.cfg" <<'EOF'
menuentry 'Ubuntu' --class gnu-linux {
    linux /boot/vmlinuz
}
menuentry 'Windows 11' {
    chainloader /EFI/Microsoft/Boot/bootmgfw.efi
}
EOF

entry="$(extract_windows_grub_entry "$tmpdir/grub-single.cfg")"
if [[ "$entry" != "Windows 11" ]]; then
    echo "expected single-quoted Windows entry, got: $entry" >&2
    exit 1
fi

echo "grub entry tests passed"
