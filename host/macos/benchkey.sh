# ./setup --stage benchkey -- the credentials the macOS benchmark lane needs.
#
# Without a tailnet identity, the benchmark install cannot be reached from a
# driver that reaches this Mac over the tailnet (rpi5 does): the A/B lane plants
# a job it cannot watch, and collecting a result needs somebody to walk over.
# Asked for once, here, next to the other things ./setup asks for, and stored
# 0600. Skipping is fine and says what is lost.
#
# Only on the Mac that has the benchmark volume: nothing else uses this.
#
# Sourced by ./setup, so WK_ROOT, the shell options and common.sh are already
# in scope: re-deriving WK_ROOT from `dirname $0` would resolve two levels
# above the checkout, since `$0` in a sourced file is the *sourcing* script.

is_macos || { debug "benchkey: macOS only"; return 0; }

KEY="${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}"

if [ -s "$KEY" ]; then
    info "tailscale auth key already present ($KEY)"
else
    log "The benchmark install needs its own tailnet identity. Without one it is"
    log "reachable only at a DHCP address on this LAN -- and not at all from a"
    log "driver that reaches this Mac by its tailnet name, which is how the A/B"
    log "lane is driven. This is the difference between a run you can watch and"
    log "a run you have to walk over to collect."
    if wk_tailscale_authkey >/dev/null; then
        info "benchkey: stored"
    else
        warn "no key stored. The lane still works, but the benchmark install will"
        warn "be unobservable: 'wk bench mac-ab' plants a job it cannot watch, and"
        warn "collecting the result needs the startup manager and a person."
        warn "Re-run './setup --stage benchkey' whenever you have a key."
    fi
fi

# Cached next to the key, for the same reason: a provisioning step that needs
# the network every time fails on the day the network is what's being fixed.
PKG="$HOME/.config/wk/Tailscale-macos.pkg"
if [ -s "$PKG" ]; then
    info "tailscale package already cached ($(du -h "$PKG" | awk '{print $1}'))"
elif [ -s "$KEY" ]; then
    name=$(curl -fsS 'https://pkgs.tailscale.com/stable/' 2>/dev/null \
           | grep -oE 'Tailscale-[0-9.]+-macos\.pkg' | sort -u | tail -1)
    if [ -n "$name" ]; then
        mkdir -p "$(dirname "$PKG")"
        if curl -fsSL -o "$PKG.part" "https://pkgs.tailscale.com/stable/$name" \
           && mv "$PKG.part" "$PKG"; then
            info "cached $name"
        else
            rm -f "$PKG.part"
            warn "could not download $name; provisioning will try again"
        fi
    else
        warn "could not work out which Tailscale package to fetch"
    fi
fi
