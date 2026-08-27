# macOS: verify the host has what it needs. Install nothing.
#
# The host deliberately depends on four things, all of which install as signed
# packages or Apple tooling. Homebrew is not used: a brew tree tends to
# accumulate Qt/LLVM/ffmpeg/rust, and none of that should ever become
# load-bearing for a dev environment that is supposed to survive a fresh
# install. Anything the SDK needs (GNU getopt, bash 5, systemd tools) lives
# inside the podman VM, which is Linux and has them already.

_missing=0

check_tool() {
    local bin="$1" name="$2" where="$3"
    if have "$bin"; then
        unchanged "$name present ($(command -v "$bin"))"
    else
        warn "$name is missing -- install it from: $where"
        _missing=$((_missing + 1))
    fi
}

# zsh is the macOS system shell and is expected at /bin/zsh, but shell/bashrc
# execs it only if it finds one -- and it fails *silently* back to bash, so a
# missing or unexecutable zsh looks like "my prompt is different today" rather
# than like an error. On Ubuntu the same file is installed by host/linux/apt.txt.
check_tool zsh       "zsh"       "it ships with macOS at /bin/zsh -- if this is missing, something is very wrong"
check_tool podman    "podman"    "https://podman.io/docs/installation#macos (official .pkg, not brew)"
check_tool git       "git"       "xcode-select --install"
check_tool tailscale "Tailscale" "https://tailscale.com/download/macos"
# Optional: only `wk find` (the LAN sweep for a board off the tailnet) needs it,
# and it refuses by name without it. Noted, not counted missing.
have nmap && unchanged "nmap present ($(command -v nmap))" || log "nmap absent -- only 'wk find' needs it (nmap.org, the .dmg)"

if [ -d /Applications/Zed.app ]; then
    unchanged "Zed present"
else
    warn "Zed is missing -- install it from: https://zed.dev/download"
    _missing=$((_missing + 1))
fi

# Xcode proper is only needed for the macOS VM lane, so this is a note rather
# than a failure.
if xcode-select -p >/dev/null 2>&1; then
    unchanged "Xcode command line tools present"
else
    warn "Xcode command line tools missing -- run: xcode-select --install"
    _missing=$((_missing + 1))
fi

if [ "$_missing" -gt 0 ]; then
    die "$_missing required tool(s) missing; install them and re-run ./setup"
fi

# --- optional: macOS VMs for the Apple ports ---------------------------------
# Not in the required list, and deliberately not installed here. Two reasons:
# it is only needed if you build the Apple ports, and its licence is not OSI
# (FSL-1.1-ALv2), which is a decision for a person rather than a setup script.
#
# It also cannot simply be copied to a bare path: the binary needs the
# com.apple.security.virtualization entitlement, which lives in the signed .app
# bundle. Extracting the executable out of the bundle produces something that
# fails at runtime with an unhelpful error.
if have tart || [ -x "$HOME/.local/bin/tart" ]; then
    unchanged "tart present (macOS VM target available)"
elif [ -d "$HOME/.tart" ]; then
    warn "~/.tart exists but tart is not on PATH -- 'wk vm' will not work"
else
    debug "tart not installed; 'wk vm' unavailable (see README.md, Setup)"
fi

unset _missing
