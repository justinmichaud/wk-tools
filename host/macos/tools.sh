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

check_tool podman    "podman"    "https://podman.io/docs/installation#macos (official .pkg, not brew)"
check_tool git       "git"       "xcode-select --install"
check_tool tailscale "Tailscale" "https://tailscale.com/download/macos"

if [ -d /Applications/Zed.app ]; then
    unchanged "Zed present"
else
    warn "Zed is missing -- install it from: https://zed.dev/download"
    _missing=$((_missing + 1))
fi

# Xcode proper is only needed for the Phase 4 macOS VM work, so this is a note
# rather than a failure.
if xcode-select -p >/dev/null 2>&1; then
    unchanged "Xcode command line tools present"
else
    warn "Xcode command line tools missing -- run: xcode-select --install"
    _missing=$((_missing + 1))
fi

if [ "$_missing" -gt 0 ]; then
    die "$_missing required tool(s) missing; install them and re-run ./setup"
fi

unset _missing
