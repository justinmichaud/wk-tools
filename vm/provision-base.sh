#!/usr/bin/env bash

set -euo pipefail

WK_TOOLS_DIR="$HOME/wk-tools"

say() { printf '==> %s\n' "$*" >&2; }

_xcode=$(xcodebuild -version 2>/dev/null | sed -n 1p) || true
[ -n "$_xcode" ] || {
    echo "error: no usable Xcode in this image" >&2
    exit 1
}
say "Xcode: $_xcode"
sudo xcodebuild -license accept >/dev/null 2>&1 || true
sudo xcodebuild -runFirstLaunch >/dev/null 2>&1 || true

NEED_FREE_GB=60

_free_gb() { df -g /System/Volumes/Data | awk 'NR==2 {print $4}'; }

_store=$(python3 "$WK_TOOLS_DIR/lib/wk/mac.py" physical-store 2>/dev/null)
if [ -n "$_store" ]; then
    _out=$(sudo diskutil apfs resizeContainer "$_store" 0 2>&1) || true
fi

_free=$(_free_gb)
if [ "${_free:-0}" -ge "$NEED_FREE_GB" ]; then
    say "disk: ${_free}G free on the data volume"
else
    echo "warning: only ${_free}G free on the data volume, need ~${NEED_FREE_GB}G." >&2
    echo "         The APFS container may not span the whole disk. resizeContainer said:" >&2
    printf '         %s\n' "${_out:-no physical store found}" >&2
    echo "         Grow it with:  WK_VM_DISK_GB=<bigger> wk sysimage build macos-guest-base --rebuild" >&2
fi

sudo pmset -a disablesleep 1 >/dev/null 2>&1 || true
sudo systemsetup -setcomputersleep Never >/dev/null 2>&1 || true

# The guest keeps the image's admin password: `sysadminctl -oldPassword`, the only form the account itself can run, exits 0 having changed nothing on macOS Tahoe 26.4.
WK_VM_USER="${WK_VM_USER:-admin}"
WK_VM_PASSWORD="${WK_VM_PASSWORD:-admin}"

{ ( . "$WK_TOOLS_DIR/bench/mac-quiet-desktop.sh"; wk_quiet_desktop_script )
  cat "$WK_TOOLS_DIR/bench/mac-pyobjc.sh" "$WK_TOOLS_DIR/vm/desktop.sh"
} | WK_VM_PASSWORD="$WK_VM_PASSWORD" bash -s

: "${WK_MOUNT_MIRROR:?}"
sudo -n mkdir -p "$(dirname "$WK_MOUNT_MIRROR")"
sudo -n install -m 0755 "$WK_TOOLS_DIR/vm/mount-mirror.sh" "$WK_MOUNT_MIRROR"

# Set by a login agent, since CoreGraphics' mode pick errors against a sleeping display. WK_VM_DISPLAY is a floor on the tart window: a large one makes it unresizable.
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/wk-set-display.m" <<'OBJC'
#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
int main(int c, char **v) { @autoreleasepool {
  if (c < 3) return 2;
  size_t want_w = (size_t)atol(v[1]), want_h = (size_t)atol(v[2]);
  CGDirectDisplayID d = CGMainDisplayID();
  CFArrayRef ms = CGDisplayCopyAllDisplayModes(d, NULL);
  long n = ms ? CFArrayGetCount(ms) : 0;
  for (long i = 0; i < n; i++) {
    CGDisplayModeRef m = (CGDisplayModeRef)CFArrayGetValueAtIndex(ms, i);
    if (CGDisplayModeGetWidth(m) != want_w || CGDisplayModeGetHeight(m) != want_h) continue;
    CGDisplayConfigRef cfg;
    if (CGBeginDisplayConfiguration(&cfg) != kCGErrorSuccess) return 1;
    CGConfigureDisplayWithDisplayMode(cfg, d, m, NULL);
    return CGCompleteDisplayConfiguration(cfg, kCGConfigurePermanently) == kCGErrorSuccess ? 0 : 1;
  }
  return 3;
} }
OBJC
clang -fobjc-arc -framework Foundation -framework CoreGraphics \
    "$HOME/.local/bin/wk-set-display.m" -o "$HOME/.local/bin/wk-set-display" 2>/dev/null ||     echo "warning: could not build wk-set-display; the guest will stay at its default resolution" >&2

PYTHONPATH="$WK_TOOLS_DIR/lib" python3 -m wk.daemons guest nosleep mirror display

say "base provisioning complete"
