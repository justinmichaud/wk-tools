#!/usr/bin/env bash

set -euo pipefail

SRC="$HOME/WebKit"
MIRROR="${WK_VM_MIRROR:?WK_VM_MIRROR must name the guest mirror; this script is run by targets/vm.sh}"
WK_TOOLS_DIR="$HOME/wk-tools"

say() { printf '==> %s\n' "$*" >&2; }

# sed, not `| head -1`, which SIGPIPEs xcodebuild.
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

_store=$(python3 "$WK_TOOLS_DIR/lib/wkmac.py" physical-store 2>/dev/null)
if [ -n "$_store" ]; then
    # 0 = all available space; macOS may auto-expand and return error -69743 for it, so free space after is the verdict.
    _out=$(sudo diskutil apfs resizeContainer "$_store" 0 2>&1) || true
fi

_free=$(_free_gb)
if [ "${_free:-0}" -ge "$NEED_FREE_GB" ]; then
    say "disk: ${_free}G free on the data volume"
else
    echo "warning: only ${_free}G free on the data volume, need ~${NEED_FREE_GB}G." >&2
    echo "         The APFS container may not span the whole disk. resizeContainer said:" >&2
    printf '         %s\n' "${_out:-no physical store found}" >&2
    echo "         Grow it with:  WK_VM_DISK_GB=<bigger> wk vm base --refresh" >&2
fi

sudo pmset -a disablesleep 1 >/dev/null 2>&1 || true
sudo systemsetup -setcomputersleep Never >/dev/null 2>&1 || true

say "refreshing the WebKit mirror at $MIRROR (the first one clones all of WebKit)"
_refresh=$(bash -c '. "$1/lib/common.sh"; . "$1/lib/store.sh"; mirror_refresh_script "$2"' \
               _ "$WK_TOOLS_DIR" "$MIRROR") \
    || { echo "error: could not build the mirror refresh script" >&2; exit 1; }
sh -c "$_refresh" 2>&1 | sed 's/^/    /' >&2 \
    || { echo "error: the mirror at $MIRROR could not be made (above)" >&2; exit 1; }

git -C "$MIRROR" rev-parse --verify --quiet refs/heads/main >/dev/null || {
    echo "error: the mirror at $MIRROR has no origin/main, so there is nothing" >&2
    echo "       to check out. The fetch above says which upstream failed." >&2
    exit 1
}

if [ -d "$SRC/.git" ]; then
    say "WebKit checkout present, fast-forwarding it from the mirror"
    git -C "$SRC" fetch --quiet --no-tags --prune "$MIRROR" \
        '+refs/heads/main:refs/remotes/origin/main' || true
    git -C "$SRC" reset --hard --quiet refs/remotes/origin/main || true
else
    say "cloning WebKit from the mirror"
    git clone --quiet --shared --branch main "$MIRROR" "$SRC"
fi
_wiring=$(bash -c '. "$1/lib/common.sh"; . "$1/lib/store.sh"; wk_wiring_script "$2"' \
              _ "$WK_TOOLS_DIR" "$SRC" 2>/dev/null) \
    && sh -c "$_wiring" \
    && say "remotes: origin=WebKit/WebKit, forks added" \
    || say "WARNING: could not wire the checkout's remotes"

say "WebKit at $(git -C "$SRC" rev-parse --short HEAD)"

if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
    say "Claude CLI present"
else
    say "installing the Claude CLI"
    curl -fsSL https://claude.ai/install.sh | bash || \
        echo "warning: Claude CLI install failed; 'wk ai claude' will not work here" >&2
fi

if [ -d "$WK_TOOLS_DIR/claude" ]; then
    mkdir -p "$HOME/.claude"
    ln -sfn "$WK_TOOLS_DIR/claude/settings.json" "$HOME/.claude/settings.json"
    ln -sfn "$WK_TOOLS_DIR/claude/hooks"         "$HOME/.claude/hooks"
    ln -sfn "$WK_TOOLS_DIR/claude/CLAUDE.md"     "$HOME/.claude/CLAUDE.md"
    ln -sfn "$WK_TOOLS_DIR/claude/skills"        "$HOME/.claude/skills"
    say "Claude config linked from $WK_TOOLS_DIR/claude"
else
    echo "warning: $WK_TOOLS_DIR/claude missing; ~/.claude not configured" >&2
fi
git config --global --replace-all include.path "$WK_TOOLS_DIR/dotfiles/gitconfig"
say "git identity and settings included from $WK_TOOLS_DIR/dotfiles/gitconfig"

# The guest keeps the image's admin password: `sysadminctl -oldPassword`, the only form the account itself can run, exits 0 having changed nothing on macOS Tahoe 26.5.
WK_VM_USER="${WK_VM_USER:-admin}"
WK_VM_PASSWORD="${WK_VM_PASSWORD:-admin}"

WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"
WK_VM_DISPLAY_W="${WK_VM_DISPLAY%x*}"
WK_VM_DISPLAY_H="${WK_VM_DISPLAY#*x}"

WK_VM_PASSWORD="$WK_VM_PASSWORD" bash "$WK_TOOLS_DIR/vm/desktop.sh"

# pmset alone does not keep the display awake.
sudo -n tee /Library/LaunchDaemons/org.wk.nosleep.plist >/dev/null <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.wk.nosleep</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/caffeinate</string><string>-dimsu</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
PLIST
sudo -n launchctl bootout system/org.wk.nosleep 2>/dev/null || true
sudo -n launchctl bootstrap system /Library/LaunchDaemons/org.wk.nosleep.plist 2>/dev/null || true

# A login agent, because the CoreGraphics mode-picking call errors against a sleeping display. Do not raise the size: it is a floor on the tart window, and a large one makes the window unresizable.
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

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$HOME/Library/LaunchAgents/org.wk.display.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.wk.display</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/.local/bin/wk-set-display</string>
    <string>${WK_VM_DISPLAY_W:-1280}</string>
    <string>${WK_VM_DISPLAY_H:-800}</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST

bash "$WK_TOOLS_DIR/vm/shell-rc.sh" "$WK_TOOLS_DIR"

if [ -d "$SRC/Tools/Scripts" ]; then
    say "warming webkitpy's autoinstalled packages"
    ( cd "$SRC" && Tools/Scripts/run-webkit-tests --help >/dev/null 2>&1 ) || \
        echo "warning: could not warm webkitpy's autoinstall; workspaces will do it themselves" >&2
    if [ -d "$SRC/Tools/Scripts/libraries/autoinstalled" ]; then
        say "autoinstalled: $(du -sh "$SRC/Tools/Scripts/libraries/autoinstalled" | cut -f1)"
    fi
fi

say "base provisioning complete"
