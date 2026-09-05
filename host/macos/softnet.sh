# vmnet needs root, so softnet is installed SUID root.
WK_SOFTNET_VERSION="${WK_SOFTNET_VERSION:-0.23.0}"
WK_SOFTNET_BIN="${WK_SOFTNET_BIN:-/usr/local/bin/softnet}"

if ! have tart && [ ! -x "$HOME/.local/bin/tart" ]; then
    debug "tart not installed; skipping softnet (see README.md, Setup)"
    return 0 2>/dev/null || true
fi

# `softnet --version` prints "softnet 0.23.0-e5fd48c": version plus commit.
_installed_ver=""
[ -x "$WK_SOFTNET_BIN" ] && \
    _installed_ver=$("$WK_SOFTNET_BIN" --version 2>/dev/null | awk '{print $NF}' | cut -d- -f1)

if [ "$_installed_ver" = "$WK_SOFTNET_VERSION" ] && [ -u "$WK_SOFTNET_BIN" ]; then
    unchanged "softnet $WK_SOFTNET_VERSION (SUID)"
else
    _tmp=$(mktemp -d)
    info "downloading softnet $WK_SOFTNET_VERSION"
    _base="https://github.com/cirruslabs/softnet/releases/download/$WK_SOFTNET_VERSION"

    if curl -fsSL -o "$_tmp/softnet.tar.gz" "$_base/softnet.tar.gz" &&
       curl -fsSL -o "$_tmp/sums.txt"      "$_base/softnet_${WK_SOFTNET_VERSION}_checksums.txt"; then

        _want=$(awk '/softnet.tar.gz$/ {print $1}' "$_tmp/sums.txt")
        _got=$(shasum -a 256 "$_tmp/softnet.tar.gz" | awk '{print $1}')

        if [ -n "$_want" ] && [ "$_want" = "$_got" ]; then
            tar -xzf "$_tmp/softnet.tar.gz" -C "$_tmp"
            if [ -f "$_tmp/softnet" ]; then
                if sudo install -o root -g wheel -m 4755 "$_tmp/softnet" "$WK_SOFTNET_BIN"; then
                    changed "installed softnet $WK_SOFTNET_VERSION at $WK_SOFTNET_BIN (SUID root)"
                else
                    warn "could not install softnet -- macOS guests will have unfiltered egress.
    This stage needs a terminal for sudo:  ./setup --stage softnet"
                fi
            else
                warn "softnet archive did not contain the expected binary"
            fi
        else
            warn "softnet checksum mismatch -- refusing to install
    expected $_want
    got      $_got"
        fi
    else
        warn "could not download softnet; macOS guests will have unfiltered egress"
    fi
    rm -rf "$_tmp"
fi

unset _installed_ver _tmp _base _want _got
