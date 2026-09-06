
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

check_tool zsh       "zsh"       "it ships with macOS at /bin/zsh -- if this is missing, something is very wrong"
check_tool podman    "podman"    "https://podman.io/docs/installation#macos (official .pkg, not brew)"
check_tool git       "git"       "xcode-select --install"
check_tool tailscale "Tailscale" "https://tailscale.com/download/macos"
have nmap && unchanged "nmap present ($(command -v nmap))" || log "nmap absent -- only 'wk find' needs it (nmap.org, the .dmg)"

if [ -d /Applications/Zed.app ]; then
    unchanged "Zed present"
else
    warn "Zed is missing -- install it from: https://zed.dev/download"
    _missing=$((_missing + 1))
fi

if xcode-select -p >/dev/null 2>&1; then
    unchanged "Xcode command line tools present"
else
    warn "Xcode command line tools missing -- run: xcode-select --install"
    _missing=$((_missing + 1))
fi

. "$WK_ROOT/bench/mac-pyobjc.sh"
if wk_pyobjc_have; then
    unchanged "pyobjc $WK_PYOBJC_VERSION present (run-benchmark and the raiser)"
elif wk_pyobjc_install; then
    changed "pyobjc $WK_PYOBJC_VERSION installed"
else
    warn "pyobjc $WK_PYOBJC_VERSION did not install -- run-benchmark cannot drive a browser here"
    _missing=$((_missing + 1))
fi

if [ "$_missing" -gt 0 ]; then
    die "$_missing required tool(s) missing; install them and re-run ./setup"
fi

# tart is not installed here: non-OSI licence (FSL-1.1-ALv2), and the binary
# needs com.apple.security.virtualization from its signed .app bundle.
if tart_bin >/dev/null; then
    unchanged "tart present (macOS VM target available)"
elif [ -d "$HOME/.tart" ]; then
    warn "~/.tart exists but tart is not on PATH -- 'wk vm' will not work"
else
    debug "tart not installed; 'wk vm' unavailable (see README.md, Setup)"
fi

unset _missing
