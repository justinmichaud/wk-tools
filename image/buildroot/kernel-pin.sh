#!/usr/bin/env bash
# The modules are decompressed and depmod re-run over the result: these images run BusyBox modprobe against modules.dep, and a .ko.xz path there leaves every module unloadable.

set -euo pipefail

deb="${1:-}"; release="${2:-}"; out="${3:-}"
[ -n "$deb" ] && [ -n "$release" ] && [ -n "$out" ] \
    || { echo "usage: kernel-pin.sh <kernel .deb> <release> <output dir>" >&2; exit 2; }
[ -f "$deb" ] || { echo "kernel-pin: no such package: $deb" >&2; exit 1; }

for t in dpkg-deb xz depmod tar; do
    command -v "$t" >/dev/null \
        || { echo "kernel-pin: $t is required to prepare a pinned kernel and is not installed" >&2; exit 1; }
done

mkdir -p "$out"
tarball="$out/wk-kernel-$release.tar"

stamp="$tarball.from"
want="$(sha256sum "$deb" | cut -d' ' -f1)"
if [ -f "$tarball" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$want" ]; then
    printf '%s' "$tarball"
    exit 0
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
dpkg-deb -x "$deb" "$work/x"

k="$work/x/boot/vmlinuz-$release"
d="$work/x/usr/lib/linux-image-$release"
[ -f "$k" ] || { echo "kernel-pin: $deb carries no boot/vmlinuz-$release" >&2; exit 1; }
[ -d "$d" ] || { echo "kernel-pin: $deb carries no device trees for $release" >&2; exit 1; }
[ -d "$work/x/lib/modules/$release" ] \
    || { echo "kernel-pin: $deb carries no modules for $release" >&2; exit 1; }

# The firmware jumps to whatever this is, and a mismatch is a board that hangs with no console.
magic=$(od -An -tx4 -j36 -N4 "$k" | tr -d ' \n')
[ "$magic" = "016f2818" ] \
    || { echo "kernel-pin: $k is not a 32-bit ARM zImage (magic $magic)" >&2; exit 1; }

tree="$work/tree"
mkdir -p "$tree/boot" "$tree/lib/modules" "$tree/dtb"
cp "$k" "$tree/boot/zImage"
cp -a "$work/x/lib/modules/$release" "$tree/lib/modules/"
find "$tree/lib/modules/$release" -name '*.ko.xz' -exec xz -d {} +
depmod -b "$tree" "$release"
grep -q '\.ko\.xz' "$tree/lib/modules/$release/modules.dep" \
    && { echo "kernel-pin: modules.dep still names .xz modules after depmod" >&2; exit 1; }
cp "$d"/*.dtb "$tree/dtb/"
[ -d "$d/overlays" ] && cp -a "$d/overlays" "$tree/dtb/overlays"

tar -C "$tree" -cf "$tarball.new" .
mv -f "$tarball.new" "$tarball"
printf '%s' "$want" > "$stamp"
printf '%s' "$tarball"
