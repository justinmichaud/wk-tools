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

# --- egress ---------------------------------------------------------------
# The guest's only route out is the proxy on the host: Softnet denies
# everything else. Nothing here resolves names itself -- the proxy does that,
# on the other side of the boundary -- so no resolver is needed or wanted.
#
# Anything that speaks HTTP honours these. What does NOT is ssh, so `git push`
# over ssh from inside a guest will not work; push over HTTPS, or do it from
# the host. The Linux workspaces solve this with container/proxy/ssh-proxy.py
# and the same trick would work here, but it is not wired up yet.
WK_PROXY="${WK_VM_PROXY_ADDR:-192.168.64.1}:${WK_VM_PROXY_PORT:-3128}"
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
