# ./setup --stage benchkey -- the credentials the macOS benchmark lane needs.
#
# A ./setup stage rather than something the bench commands warn about, because
# the warning route was tried and it does not work. `wk bench mac-volume` printed
# four lines telling the reader to create ~/.config/wk/tailscale-authkey by hand
# and carried on when it was absent -- and it was absent every time, for two
# days, while the one thing it gates is a tailnet identity for the benchmark
# install. Without that identity the install cannot be reached from any driver
# that reaches this Mac over the tailnet (rpi5 does), which is why the A/B lane
# plants a job it cannot watch, and why collecting a finished result needs
# somebody to walk to the machine and pick a disk.
#
# So it is asked for, once, here -- next to the other things ./setup asks for --
# and stored 0600. Skipping is fine and says what is lost.
#
# Only on the Mac that has the benchmark volume: nothing else uses this.
#
# Sourced by ./setup, like every other stage, so WK_ROOT, the shell options and
# common.sh are already in scope. It used to re-derive its own WK_ROOT from
# `dirname $0` -- and `$0` in a sourced file is the *sourcing* script, so under
# `wk setup` it resolved two levels above the checkout and the stage died on a
# missing lib/common.sh. Stages do not have their own root.

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

# The Tailscale package, cached next to the key. Fetched here rather than during
# provisioning for the same reason the key is: a provisioning step that needs the
# network every time is a step that fails on the day the network is what is being
# fixed. No compiler is involved -- see bench/mac-bench-volume.sh for why the
# comment that claimed one was wrong.
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
