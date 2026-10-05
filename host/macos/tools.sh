
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
have nmap && unchanged "nmap present ($(command -v nmap))" || log "nmap absent -- only 'wk machine probe' needs it (nmap.org, the .dmg)"

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

WK_GIT_LFS_VERSION=3.6.1
if have git-lfs || [ -x "$HOME/.local/bin/git-lfs" ]; then
    unchanged "git-lfs present"
else
    _tmp=$(mktemp -d)
    case "$(uname -m)" in x86_64) _arch=amd64 ;; *) _arch=arm64 ;; esac
    _zip="git-lfs-darwin-$_arch-v$WK_GIT_LFS_VERSION.zip"
    _base="https://github.com/git-lfs/git-lfs/releases/download/v$WK_GIT_LFS_VERSION"
    if curl -fsSL -o "$_tmp/$_zip" "$_base/$_zip" && curl -fsSL -o "$_tmp/sums" "$_base/sha256sums.asc" &&
       [ "$(awk -v z="$_zip" '$2 == z {print $1}' "$_tmp/sums")" = "$(shasum -a 256 "$_tmp/$_zip" | awk '{print $1}')" ] &&
       unzip -q "$_tmp/$_zip" -d "$_tmp" &&
       install -d "$HOME/.local/bin" && install -m 755 "$_tmp/git-lfs-$WK_GIT_LFS_VERSION/git-lfs" "$HOME/.local/bin/git-lfs"; then
        changed "git-lfs $WK_GIT_LFS_VERSION installed at ~/.local/bin/git-lfs"
    else
        warn "git-lfs $WK_GIT_LFS_VERSION did not download, verify or install -- install it from: https://git-lfs.com"
        _missing=$((_missing + 1))
    fi
    rm -rf "$_tmp"
    unset _tmp _arch _zip _base
fi

if [ "$_missing" -gt 0 ]; then
    die "$_missing required tool(s) missing; install them and re-run ./setup"
fi

# tart is not installed here: non-OSI licence (FSL-1.1-ALv2), and the binary
# needs com.apple.security.virtualization from its signed .app bundle.
if wk_py wk.places tart >/dev/null; then
    unchanged "tart present (the vm place available)"
elif [ -d "$HOME/.tart" ]; then
    warn "~/.tart exists but tart is not on PATH -- no macOS guest will work"
else
    debug "tart not installed; no macOS guests (see README.md, Setup)"
fi

unset _missing
