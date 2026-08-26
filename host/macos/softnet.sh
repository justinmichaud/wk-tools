# Softnet: the egress boundary for macOS guest VMs.
#
# The container workspaces reach the outside through a proxy because they have
# no network interface at all. A macOS guest cannot be built that way -- it is
# a whole operating system and needs an interface -- so the boundary is put in
# front of it instead:
#
#   Softnet     a userspace packet filter that Tart runs as a subprocess on the
#               HOST, outside the guest. Default-deny, with one address allowed:
#               the host itself.
#   wk-proxy    the same policy engine the containers use, listening on TCP on
#               the host, reached over that one allowed address.
#
# The guest therefore gets the same hostname allowlist as a container, enforced
# somewhere it cannot reach. This is what `pf` inside the guest could never be:
# pf is modifiable by anything running as root in the guest, which includes
# whatever is being sandboxed.
#
# --- about the privilege ------------------------------------------------------
# Softnet needs root, because vmnet does. It is installed SUID root here, once,
# at setup time -- the same trade the quiesce helper makes: a privilege granted
# deliberately to one audited path during setup, never one taken in the daily
# path. `wk` itself still never calls sudo.

WK_SOFTNET_VERSION="${WK_SOFTNET_VERSION:-0.23.0}"
WK_SOFTNET_BIN="${WK_SOFTNET_BIN:-/usr/local/bin/softnet}"

# Only meaningful if the macOS VM target is in use at all.
if ! have tart && [ ! -x "$HOME/.local/bin/tart" ]; then
    debug "tart not installed; skipping softnet (see README.md, Setup)"
    return 0 2>/dev/null || true
fi

# `softnet --version` prints "softnet 0.23.0-e5fd48c" -- the release version
# with the build's commit appended. Comparing the whole string against the
# release number never matches, so setup re-downloaded ~14 MB on every run
# while correctly reporting "no changes".
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

        # Verified before anything is installed, and certainly before anything
        # is made SUID root. An unverified download that then gets the setuid
        # bit is a local root exploit with extra steps.
        _want=$(awk '/softnet.tar.gz$/ {print $1}' "$_tmp/sums.txt")
        _got=$(shasum -a 256 "$_tmp/softnet.tar.gz" | awk '{print $1}')

        if [ -n "$_want" ] && [ "$_want" = "$_got" ]; then
            tar -xzf "$_tmp/softnet.tar.gz" -C "$_tmp"
            if [ -f "$_tmp/softnet" ]; then
                # root-owned, not writable by the invoking user: a SUID binary
                # in a user-writable path is not a boundary at all.
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
