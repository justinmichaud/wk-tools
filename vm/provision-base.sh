#!/usr/bin/env bash
#
# Runs once, inside the golden base VM, over ssh from targets/vm.sh.
#
# Everything expensive about a macOS build environment is paid here exactly
# once -- Xcode's first-launch components, the WebKit clone, the Claude CLI --
# and every workspace inherits the result through `tart clone`, which is an
# APFS copy-on-write operation. That is the entire reason the base exists:
# without it, each workspace would re-clone a multi-gigabyte repository.
#
# Constraints:
#
#   bash 3.2. This is the macOS system bash and there is no other one here.
#   Idempotent. It is re-run by `wk vm base --rebuild` and after any change to
#   this file, so every step checks before acting.

set -euo pipefail

MARKER="$HOME/.wk-provisioned"
SRC="$HOME/WebKit"

say() { printf '==> %s\n' "$*" >&2; }

# --- Xcode -------------------------------------------------------------------
# The prebuilt image ships Xcode, which is the whole reason to start from it:
# a from-scratch install is multi-hour and needs an Apple ID. What it does not
# always ship is an accepted licence or the first-launch components, and both
# fail the build far downstream with an error that does not mention either.
# `| head -1` would SIGPIPE xcodebuild and make the pipeline fail even on
# success, so the fallback fires alongside the real answer. sed reads its input
# to the end and does not.
_xcode=$(xcodebuild -version 2>/dev/null | sed -n 1p) || true
[ -n "$_xcode" ] || {
    echo "error: no usable Xcode in this image" >&2
    exit 1
}
say "Xcode: $_xcode"
sudo xcodebuild -license accept >/dev/null 2>&1 || true
sudo xcodebuild -runFirstLaunch >/dev/null 2>&1 || true

# --- claim the whole disk ------------------------------------------------------
# Growing the virtual disk is only half of it: the guest's APFS container still
# spans the old size, so the extra space is invisible until the container is
# resized to fill its physical store. Without this the build dies with
# "No space left on device" while the host thinks the guest has 100 GB spare.
#
# The physical store is read from diskutil rather than assumed to be disk0s2 --
# it usually is, but hardcoding a device node to then resize it is not a good
# trade for one line of parsing.
# A Release build tree runs to tens of gigabytes on top of a ~19 GB checkout,
# so this is the floor below which a build is not worth starting.
NEED_FREE_GB=60

_free_gb() { df -g /System/Volumes/Data | awk 'NR==2 {print $4}'; }

_store=$(diskutil apfs list | sed -n 's/.*Physical Store \(disk[0-9]*s[0-9]*\).*/\1/p' | head -1)
if [ -n "$_store" ]; then
    # `0` means "use all available space". Recent macOS expands the container
    # by itself on first boot after the disk grows, in which case this returns
    # error -69743 ("new size must be different") -- which is success wearing
    # an error's clothes. So the outcome is judged on free space afterwards,
    # not on the exit status.
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
# A headless VM that goes to sleep mid-link looks exactly like a stalled build,
# and the watchdog would eventually kill it as one. There is no display and no
# battery here, so there is nothing to save.
sudo pmset -a disablesleep 1 >/dev/null 2>&1 || true
sudo systemsetup -setcomputersleep Never >/dev/null 2>&1 || true

# --- the checkout ------------------------------------------------------------
# Single branch, matching what the Linux mirror carries. WebKit has ~920
# branches and the rest cost tens of gigabytes for histories nobody checks out;
# a workspace that needs one can fetch it on demand.
if [ -d "$SRC/.git" ]; then
    say "WebKit checkout present, fetching"
    git -C "$SRC" fetch --quiet origin main || true
    git -C "$SRC" reset --hard --quiet origin/main || true
elif [ -d /tmp/wk-seed.git ]; then
    # The host had a checkout and rsync'd a bare copy of main in. Cloning from
    # it is a local clone inside the guest, so it hardlinks instead of copying
    # and finishes in seconds. origin is then re-pointed at GitHub and fetched,
    # so the result is indistinguishable from a fresh network clone -- the seed
    # only saves the download, it never decides what the checkout contains.
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
say "WebKit at $(git -C "$SRC" rev-parse --short HEAD)"

# --- Claude Code -------------------------------------------------------------
# Its own installer, to its own path. It self-updates into
# ~/.local/share/claude/versions/, so a binary copied anywhere else can never
# update itself -- a mistake already made once in this project.
#
# Credentials do NOT come across. On Darwin the CLI keeps them in the login
# Keychain, not in ~/.claude/.credentials.json, so the shared-secrets volume
# that works for the Linux workspaces has nothing to share here. Expect one
# `claude login` per VM; it survives cloning if you log in before the base is
# shut down, because the Keychain is part of the disk image.
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
    say "Claude CLI present"
else
    say "installing the Claude CLI"
    curl -fsSL https://claude.ai/install.sh | bash || \
        echo "warning: Claude CLI install failed; 'wk claude' will not work here" >&2
fi

# The workspace-side Claude config, same entries container/firstrun.sh links in
# a container. Without this the guest runs a skip-permissions agent with no
# CLAUDE.md, no settings and no skills. The caller rsyncs the whole wk-tools
# tree in before running this script.
#
# Skills are a read-only symlink into the synced tree here, unlike a
# container's shared mutable /skills volume: `wk build` re-rsyncs the tree with
# --delete on every run, so in-guest skill edits would be silently clobbered --
# better they fail to write than quietly vanish.
WK_TOOLS_DIR="$HOME/wk-tools"
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
# The guest's only route out is the proxy on the host: Softnet denies
# everything else. Nothing here resolves names itself -- the proxy does that,
# on the other side of the boundary -- so no resolver is needed or wanted.
#
# Anything that speaks HTTP honours these. What does NOT is ssh, so `git push`
# over ssh from inside a guest will not work; push over HTTPS, or do it from
# the host. The Linux workspaces solve this with container/proxy/ssh-proxy.py
# and the same trick would work here, but it is not wired up yet.
# No default for the address. 192.168.64.1 used to stand here -- Tart's stock
# vmnet gateway -- but this repo puts guests on WK_VM_SUBNET (192.168.2.x), so
# the fallback was an address nothing listens on. Guessing wrong here is silent
# and expensive: the guest gets a proxy it cannot reach, every fetch times out,
# and the failure is indistinguishable from Softnet correctly denying traffic.
# The caller knows the real address; if it did not pass one, say so and stop.
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

# The image's own account password. The Cirrus Labs images ship admin/admin and
# sysadminctl needs it to change the lock setting. This is not a secret and is
# not protecting anything: the guest holds no credentials, its egress is
# filtered by Softnet outside the guest, and it is destroyed with `wk rm`. It is
# recorded here because a guest whose password nobody knows is a guest you can
# be locked out of -- which is exactly what happened before the lock was turned
# off.
WK_VM_PASSWORD="${WK_VM_PASSWORD:-admin}"

# Split WK_VM_DISPLAY ("1920x1080") for the login agent below. Passed in by the
# caller so the guest and `tart set --display` cannot disagree.
WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"
WK_VM_DISPLAY_W="${WK_VM_DISPLAY%x*}"
WK_VM_DISPLAY_H="${WK_VM_DISPLAY#*x}"

# --- the screen ------------------------------------------------------------
# A macOS guest exists to be looked at: it is the only workspace kind with a
# real GPU, and MiniBrowser is meant to be interacted with. All of the below is
# about making sure the window actually shows a usable desktop rather than a
# password prompt nobody knows the answer to.
#
# Three separate mechanisms conspire to hide the desktop, and disabling any two
# of them is not enough:
#
#   1. the screen saver          -- idleTime
#   2. display sleep             -- pmset, and it is not sufficient on its own
#   3. the screen *lock*         -- a separate setting from either of the above
#
# The lock is the one that actually bit: with the screen saver off and
# askForPassword already 0, the guest still came up behind "Enter Password"
# after every boot, because the modern lock is controlled by sysadminctl and
# nothing else touches it.
sudo -n sysadminctl -screenLock off -password "$WK_VM_PASSWORD" 2>/dev/null ||     echo "warning: could not turn off the screen lock; the guest may come up locked" >&2

defaults -currentHost write com.apple.screensaver idleTime -int 0
defaults write com.apple.screensaver askForPassword -int 0
defaults write com.apple.screensaver askForPasswordDelay -int 0
sudo -n pmset -a displaysleep 0 sleep 0 disablesleep 1 2>/dev/null || true

# pmset alone did NOT keep the display awake -- measured: displaysleep 0,
# sleep 0 and SleepDisabled 1 were all already set and the display still slept,
# which reads as a black screen and then a lock. A held power assertion does
# work, so hold one for the life of the guest.
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

# Resolution. Set once at login to whatever the VM was configured with, so the
# guest does not come up on a stale saved mode -- this guest sat at 1024x768
# through several boots because that was the mode it had last been left in.
# After this, --display-refit takes over and the guest tracks the window as it
# is resized or full-screened, so do NOT raise this to "something big": the
# configured size is a *floor* on the tart window (see WK_VM_DISPLAY in
# targets/vm.sh), and a large one is what makes the window unresizable.
#
# `tart set --display WxH` sets what the virtual display is
# *capable* of, not the mode the guest picks: the guest was offered everything
# from 1024x576 to 3840x2160 and still sat at 1024x768, and --display-refit did
# not change that even with tart-guest-agent running. So ask CoreGraphics
# directly, at every login, once the display is awake -- the same call returns
# an error if it runs against a sleeping display, which is why this is a login
# agent and not a one-shot during provisioning.
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

# ~/.local/bin is not on the default macOS PATH, and `wk build` runs commands
# through a login shell specifically so this file is read.
if ! grep -q 'wk-tools: PATH' "$HOME/.zprofile" 2>/dev/null; then
    cat >> "$HOME/.zprofile" <<'EOF'
# wk-tools: PATH
export PATH="$HOME/.local/bin:$PATH"
EOF
fi
if ! grep -q 'wk-tools: PATH' "$HOME/.bash_profile" 2>/dev/null; then
    cat >> "$HOME/.bash_profile" <<'EOF'
# wk-tools: PATH
export PATH="$HOME/.local/bin:$PATH"
EOF
fi

# --- what is deliberately absent ---------------------------------------------
# ccache. There is no Homebrew here and no signed installer for it, and the
# Xcode build does not use it unless WK_USE_CCACHE=YES finds one on PATH. A mac
# build is therefore always a real build; budget accordingly, and prefer
# incremental builds in a long-lived workspace over recreating one.

date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER"
say "base provisioning complete"
