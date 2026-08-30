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
        echo "warning: Claude CLI install failed; 'wk claude' will not work here" >&2
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

# Cirrus Labs images ship admin/admin; sysadminctl needs it to change the lock
# setting below. Not a secret worth protecting: the guest holds no credentials,
# is Softnet-filtered, and is destroyed with `wk rm`.
WK_VM_PASSWORD="${WK_VM_PASSWORD:-admin}"

# WK_VM_DISPLAY ("1920x1080") is passed in so the guest and `tart set --display` cannot disagree.
WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"
WK_VM_DISPLAY_W="${WK_VM_DISPLAY%x*}"
WK_VM_DISPLAY_H="${WK_VM_DISPLAY#*x}"

# --- the screen ------------------------------------------------------------
# A macOS guest is the only workspace kind with a real GPU and a window meant
# to be looked at, so it must come up on a usable desktop, not a password prompt.
#
# Three separate mechanisms hide the desktop; disabling any two is not enough:
#
#   1. the screen saver   -- idleTime
#   2. display sleep      -- pmset (not sufficient alone)
#   3. the screen *lock*  -- sysadminctl, independent of the other two, and the
#      one that actually blocks login
sudo -n sysadminctl -screenLock off -password "$WK_VM_PASSWORD" 2>/dev/null ||     echo "warning: could not turn off the screen lock; the guest may come up locked" >&2

# 4. Setup Assistant's post-login panes (Siri, appearance, analytics) run on
#    every clone -- each clone is a new install by macOS's own reckoning, even
#    though auto-login and .AppleSetupDone are already satisfied.
#
#    Modal in front of the desktop: occludes any drawing window, which throttles
#    its timers -- a benchmark measuring the wrong thing rather than failing.
#    Each `DidSee*` key below is what the assistant sets when clicked through.
for _k in DidSeeCloudSetup DidSeeSiriSetup DidSeeAppearanceSetup           DidSeePrivacy DidSeeTrueTone DidSeeAccessibility DidSeeSyncSetup; do
    defaults write com.apple.SetupAssistant "$_k" -bool true
done
defaults write com.apple.SetupAssistant LastSeenCloudProductVersion "$(sw_vers -productVersion)"
defaults write com.apple.SetupAssistant LastSeenBuddyBuildVersion "$(sw_vers -buildVersion)"
# And the one already on screen, if this is a re-provision.
pkill -f 'Setup Assistant' 2>/dev/null || true

defaults -currentHost write com.apple.screensaver idleTime -int 0
defaults write com.apple.screensaver askForPassword -int 0
defaults write com.apple.screensaver askForPasswordDelay -int 0
sudo -n pmset -a displaysleep 0 sleep 0 disablesleep 1 2>/dev/null || true

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

# ~/.local/bin isn't on default macOS PATH; `wk build` uses a login shell so this file is read.
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
