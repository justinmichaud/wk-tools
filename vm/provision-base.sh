#!/usr/bin/env bash
#
# Runs once, over ssh from targets/vm.sh, inside the golden base VM; every
# workspace inherits the result for free via `tart clone` (APFS copy-on-write).
#
# bash 3.2: the macOS system bash, and there is no other one here.

set -euo pipefail

MARKER="$HOME/.wk-provisioned"
SRC="$HOME/WebKit"
# The tree targets/vm.sh rsyncs in (t_tools).
WK_TOOLS_DIR="$HOME/wk-tools"

say() { printf '==> %s\n' "$*" >&2; }

# --- Xcode -------------------------------------------------------------------
# Xcode without an accepted licence or first-launch components fails the
# build far downstream with an unrelated error. sed (not `| head -1`) avoids
# SIGPIPEing xcodebuild, which fails the pipeline even on success.
_xcode=$(xcodebuild -version 2>/dev/null | sed -n 1p) || true
[ -n "$_xcode" ] || {
    echo "error: no usable Xcode in this image" >&2
    exit 1
}
say "Xcode: $_xcode"
sudo xcodebuild -license accept >/dev/null 2>&1 || true
sudo xcodebuild -runFirstLaunch >/dev/null 2>&1 || true

# --- claim the whole disk ------------------------------------------------------
# Growing the virtual disk doesn't grow the guest's APFS container; without a
# resize the build dies with "No space left" while the host sees free space.
# 60G is the floor: a Release build needs tens of GB on top of the ~19G checkout.
NEED_FREE_GB=60

_free_gb() { df -g /System/Volumes/Data | awk 'NR==2 {print $4}'; }

_store=$(python3 "$WK_TOOLS_DIR/lib/wkmac.py" physical-store 2>/dev/null)
if [ -n "$_store" ]; then
    # 0 = all available space. macOS may auto-expand already, returning error
    # -69743 as success in disguise -- judged by free space after, not exit status.
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

# --- keep the guest awake ----------------------------------------------------
# A sleeping headless VM looks like a stalled build to the watchdog, which
# would kill it as one; there's no display or battery here to save power for.
sudo pmset -a disablesleep 1 >/dev/null 2>&1 || true
sudo systemsetup -setcomputersleep Never >/dev/null 2>&1 || true

# --- the checkout ------------------------------------------------------------
# Single branch (~920 total in WebKit; the rest cost tens of GB nobody checks
# out) -- a workspace that needs one fetches it on demand.
if [ -d "$SRC/.git" ]; then
    say "WebKit checkout present, fetching"
    git -C "$SRC" fetch --quiet origin main || true
    git -C "$SRC" reset --hard --quiet origin/main || true
elif [ -d /tmp/wk-seed.git ]; then
    # Local clone from the host's rsync'd bare seed hardlinks and finishes in
    # seconds; origin is then re-pointed at GitHub so the result matches a fresh clone.
    say "cloning WebKit from the host-provided seed"
    git clone --quiet --branch main /tmp/wk-seed.git "$SRC"
    git -C "$SRC" remote set-url origin https://github.com/WebKit/WebKit.git
    git -C "$SRC" fetch --quiet origin main || true
    git -C "$SRC" reset --hard --quiet origin/main || true
    rm -rf /tmp/wk-seed.git
else
    say "cloning WebKit (main only) -- this is the slow step, and it happens once"
    git clone --quiet --single-branch --branch main \
        https://github.com/WebKit/WebKit.git "$SRC"
fi
# origin is WebKit/WebKit; forks are wired the same way every checkout gets,
# from wk_wiring_script (lib/store.sh). Run in a subshell because those files
# define their own log()/warn(); must not clash with this script's.
#
# A guest has no deploy key, so a push from here fails at the door rather than
# silently succeeding -- see `wk push`.
_wiring=$(bash -c '. "$1/lib/common.sh"; . "$1/lib/store.sh"; wk_wiring_script "$2"' \
              _ "$WK_TOOLS_DIR" "$SRC" 2>/dev/null) \
    && sh -c "$_wiring" \
    && say "remotes: origin=WebKit/WebKit, forks added" \
    || say "WARNING: could not wire the checkout's remotes"

say "WebKit at $(git -C "$SRC" rev-parse --short HEAD)"

# --- Claude Code -------------------------------------------------------------
# Its own installer, to ~/.local/share/claude/versions/: a binary copied
# elsewhere can't self-update.
#
# Credentials live in the login Keychain on Darwin, not
# ~/.claude/.credentials.json, so the Linux workspaces' shared-secrets volume
# has nothing to share here -- log in once per VM before shutdown and the
# Keychain survives cloning.
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
    say "Claude CLI present"
else
    say "installing the Claude CLI"
    curl -fsSL https://claude.ai/install.sh | bash || \
        echo "warning: Claude CLI install failed; 'wk ai claude' will not work here" >&2
fi

# Same Claude config entries container/firstrun.sh links in a container;
# missing them means a skip-permissions agent with no CLAUDE.md, settings or skills.
#
# Skills are a read-only symlink here, not a mutable volume: `wk build`
# re-rsyncs with --delete, so an in-guest skill edit should fail to write
# rather than silently vanish on the next sync.
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

# --- egress ---------------------------------------------------------------
# The guest's only route out is the host proxy; Softnet denies everything else.
# ssh doesn't honour http_proxy, so `git push` over ssh from inside a guest
# fails -- push over HTTPS or from the host instead.
#
# No default address: Tart's stock gateway (192.168.64.1) is wrong for guests
# on WK_VM_SUBNET (192.168.2.x), and a wrong guess fails silently, indistinguishable
# from Softnet denying traffic -- so an unset WK_VM_PROXY_ADDR is a hard stop.
if [ -z "${WK_VM_PROXY_ADDR:-}" ]; then
    echo "provision-base: WK_VM_PROXY_ADDR was not passed in; refusing to guess" >&2
    echo "  (the guest would silently get an unreachable proxy and no egress)" >&2
    exit 1
fi
WK_PROXY="${WK_VM_PROXY_ADDR}:${WK_VM_PROXY_PORT:-3128}"
for _f in "$HOME/.zprofile" "$HOME/.bash_profile"; do
    if ! grep -q 'wk-tools: egress' "$_f" 2>/dev/null; then
        cat >> "$_f" <<EOF
# wk-tools: egress goes through the proxy on the host; Softnet denies the rest.
export http_proxy=http://$WK_PROXY
export https_proxy=http://$WK_PROXY
export HTTP_PROXY=http://$WK_PROXY
export HTTPS_PROXY=http://$WK_PROXY
export no_proxy=localhost,127.0.0.1,::1
export NO_PROXY=localhost,127.0.0.1,::1
EOF
    fi
done

# The account's password. Two facts: what the pulled image ships with (Cirrus
# Labs: admin/admin) and what this guest is meant to have. Both arrive from
# targets/vm.sh, which is where they are declared and explained; the defaults
# here only cover running this script by hand.
WK_VM_USER="${WK_VM_USER:-admin}"
WK_VM_IMAGE_PASSWORD="${WK_VM_IMAGE_PASSWORD:-admin}"
WK_VM_PASSWORD="${WK_VM_PASSWORD:-1}"

# Changed once, here, so everything after this -- the screen-lock call below,
# an Xcode prompt at the window, `wk vm enter`'s note -- means one password.
# Skipped when it already is that: sysadminctl needs the *current* one, so a
# re-provision must not offer the image's.
if [ "$WK_VM_PASSWORD" != "$WK_VM_IMAGE_PASSWORD" ]; then
    if dscl . -authonly "$WK_VM_USER" "$WK_VM_PASSWORD" >/dev/null 2>&1; then
        say "password already set"
    elif sudo -n sysadminctl -resetPasswordFor "$WK_VM_USER" \
             -newPassword "$WK_VM_PASSWORD" -adminUser "$WK_VM_USER" \
             -adminPassword "$WK_VM_IMAGE_PASSWORD" >/dev/null 2>&1; then
        say "password set for $WK_VM_USER"
    else
        echo "warning: could not change $WK_VM_USER's password; it is still the image's" >&2
        WK_VM_PASSWORD="$WK_VM_IMAGE_PASSWORD"
    fi
fi

# WK_VM_DISPLAY ("1920x1080") is passed in so the guest and `tart set --display` cannot disagree.
WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"
WK_VM_DISPLAY_W="${WK_VM_DISPLAY%x*}"
WK_VM_DISPLAY_H="${WK_VM_DISPLAY#*x}"

# --- the screen ------------------------------------------------------------
# A macOS guest is the only workspace kind with a real GPU and a window meant
# to be looked at, so it must come up on a usable desktop, not a password
# prompt and not behind a modal panel.
#
# vm/desktop.sh, which t_start runs again on every guest boot -- not a copy of
# it here. The base cannot settle this on its own: `defaults -currentHost`
# writes per hardware UUID and `tart clone` gives the clone a new one, so the
# screen-saver setting the base holds does not apply to anything cloned from it
# (measured with `wk vm check`). One file, two callers.
WK_VM_PASSWORD="$WK_VM_PASSWORD" bash "$WK_TOOLS_DIR/vm/desktop.sh"

# pmset alone doesn't keep the display awake; hold a power assertion for the guest's life.
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

# Set once at login to the VM's configured size so it doesn't come up on a
# stale saved mode; --display-refit then tracks the window on resize. Do NOT
# raise this "big" -- it's a floor on the tart window (WK_VM_DISPLAY in
# targets/vm.sh), and a large floor makes the window unresizable.
#
# `tart set --display WxH` sets display capability, not the picked mode, and
# the CoreGraphics call to pick it errors against a sleeping display -- hence
# a login agent, not a one-shot during provisioning.
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

# --- the shell ---------------------------------------------------------------
# The same rc every other machine in the fleet reads (shell/bashrc), wired in by
# vm/shell-rc.sh -- which targets/vm.sh also runs on every start, so a guest
# cloned before this existed converges too. One script, so the base and the
# clones cannot end up with different shells.
bash "$WK_TOOLS_DIR/vm/shell-rc.sh" "$WK_TOOLS_DIR"

# --- webkitpy's autoinstalled packages ----------------------------------------
# run-webkit-tests downloads webkitpy's PyPI dependencies (11 MB, measured)
# into Tools/Scripts/libraries/autoinstalled on first use; warming it here puts
# it in the golden image, free for every workspace via the APFS clone.
#
# Proxy vars are explicitly cleared: unlike a workspace, the base boots without
# Softnet, on the open 192.168.64.x vmnet -- the Softnet-gateway proxy block
# written above is for clones and points nowhere reachable from inside the base.
# Best-effort: a base that skips this just pays the download later, per workspace.
if [ -d "$SRC/Tools/Scripts" ]; then
    say "warming webkitpy's autoinstalled packages"
    ( cd "$SRC" && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        Tools/Scripts/run-webkit-tests --help >/dev/null 2>&1 ) || \
        echo "warning: could not warm webkitpy's autoinstall; workspaces will do it themselves" >&2
    if [ -d "$SRC/Tools/Scripts/libraries/autoinstalled" ]; then
        say "autoinstalled: $(du -sh "$SRC/Tools/Scripts/libraries/autoinstalled" | cut -f1)"
    fi
fi

# --- what is deliberately absent ---------------------------------------------
# ccache: no Homebrew or signed installer here, and Xcode only uses it via
# WK_USE_CCACHE=YES finding one on PATH. A mac build is always a real build.

date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER"
say "base provisioning complete"
