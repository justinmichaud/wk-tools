# How a machine is reached -- calculated, never written down.
#
# CLAUDE.md's rule is that a node is reached by its tailnet name and that how to
# reach it is not stored: a second copy of a fact goes stale, and a stale
# address reads exactly like broken hardware. That rule leaves a real question
# unanswered, though, and it is the question somebody actually has at 2am: the
# tailnet is down, or this machine is not on it -- what else is there?
#
# So this derives both, at read time, from things that are already true:
#
#   the tailnet     `tailscale status --json` on this machine, which is the
#                   tailnet's own answer about where a peer is and whether it is
#                   up. Nothing here caches it beyond one command's run.
#
#   the config      `ssh -G <name>`, which is ssh resolving its own config --
#                   the HostName it would dial, the port, and the ProxyJump it
#                   would go through. A calculation, not a copy. It answers only
#                   for the machines that genuinely cannot be on the tailnet:
#                   Igalia's build boxes through the company gateway, and moose's
#                   BMC through the phone in front of it. For everything else
#                   there is no entry, so it says nothing.
#
#   enumeration     a sweep for the machine's hardware address on the segments
#                   this machine can see (reach_enumerate). Last, because it is
#                   the only one that costs traffic -- and the only one that
#                   works for a board whose name the tailnet does not know.
#
# There is no name lookup below the tailnet, and that is deliberate. Every image
# this repo builds carries tailscale and joins on first boot -- bench systems
# included -- so a board the tailnet cannot name is a board with no uplink, and
# a second naming service would not find it either. A sweep will, if it is on a
# segment this machine can see.
#
# Everything here is read-only and bounded. It is called from inside probes that
# already run in parallel under a ceiling (`capped`, lib/common.sh), so nothing
# below may block without one of its own.

# The tailnet's view, once per process, and under a ceiling.
#
# `tailscale status` is a local call to the daemon -- no network -- but it is
# not free, and a fleet listing asks about every machine it has. Hence "once".
#
# The ceiling is the other half, and it was missing: this file's own preamble
# says nothing below it may block without a bound of its own, and this was the
# one thing in it that could. A *local* call is not the same as a call that
# returns -- a wedged tailscaled answers neither, and the CLI has no timeout of
# its own. With the daemon in that state (`the Tailscale CLI failed to start:
# Failed to save preferences`) every reach lookup in the walk hangs on it, and
# `wk status --web` never reaches the point of serving a page. That is the same shape of failure as the jump hop that
# stopped the fleet block, and it gets the same answer (`capped`,
# lib/common.sh), for the same reason: a probe that overruns must lose its own
# line, never the listing.
#
# Nothing is lost when it fires: reach_tailnet says nothing about a machine the
# tailnet has not answered for, and reach_without_tailnet is the whole point of
# there being a second answer.
_WK_TS_PEERS=""
_WK_TS_READ=""

wk_tailscale_cli() {
    if have tailscale; then echo tailscale; return 0; fi
    # macOS installs the CLI inside the app bundle when the Mac App Store build
    # is used; `wk doctor` looks in the same two places.
    local c=/Applications/Tailscale.app/Contents/MacOS/Tailscale
    [ -x "$c" ] && { echo "$c"; return 0; }
    return 1
}

# name<TAB>ip<TAB>online, one line per peer and one for this machine.
wk_tailscale_peers() {
    [ -z "$_WK_TS_READ" ] || { printf '%s' "$_WK_TS_PEERS"; return 0; }
    _WK_TS_READ=1
    local cli
    cli=$(wk_tailscale_cli) || return 0
    local raw
    raw=$(capped "${WK_TAILSCALE_TIMEOUT:-4}" "$cli" status --json 2>/dev/null) || raw=""
    [ -n "$raw" ] || return 0
    _WK_TS_PEERS=$( printf '%s' "$raw" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
def row(p, online):
    ips = p.get("TailscaleIPs") or [""]
    # The short name, because that is what every conf and ssh alias here uses;
    # the MagicDNS name is the same thing with the tailnet suffix on it.
    print("\t".join([p.get("HostName", ""), ips[0], "up" if online else "down"]))
self = d.get("Self")
if self:
    row(self, True)
for p in (d.get("Peer") or {}).values():
    row(p, p.get("Online"))
' 2>/dev/null ) || _WK_TS_PEERS=""
    printf '%s' "$_WK_TS_PEERS"
}

# reach_tailnet <name> -- "<ip> (up|down)", or nothing when the tailnet has
# never heard of it. Matched on the short name, which is what a machine is
# called everywhere else in this repository.
reach_tailnet() {
    local name="$1" line
    line=$(wk_tailscale_peers | awk -F'\t' -v n="$name" '$1 == n {print; exit}')
    [ -n "$line" ] || return 0
    printf '%s (%s)' "$(printf '%s' "$line" | cut -f2)" "$(printf '%s' "$line" | cut -f3)"
}

# reach_ssh <name> -- what `ssh <name>` would actually dial, as ssh resolves it.
#
# `ssh -G` performs the whole config resolution -- Host blocks, Include files,
# Match rules -- and prints the result without connecting. It is the only
# honest answer to "how would ssh reach this", and it is a calculation rather
# than a second copy of the config.
reach_ssh() {
    local name="$1" g host port jump user out
    have ssh || return 0
    g=$(ssh -G "$name" 2>/dev/null) || return 0
    host=$(printf '%s\n' "$g" | awk '$1 == "hostname" {print $2; exit}')
    port=$(printf '%s\n' "$g" | awk '$1 == "port" {print $2; exit}')
    user=$(printf '%s\n' "$g" | awk '$1 == "user" {print $2; exit}')
    jump=$(printf '%s\n' "$g" | awk '$1 == "proxyjump" {print $2; exit}')
    [ -n "$host" ] || return 0
    out="$host"
    [ "${port:-22}" = 22 ] || out="$out:$port"
    [ -z "$user" ] || out="$user@$out"
    [ -z "$jump" ] || [ "$jump" = none ] || out="$out  (through $jump)"
    printf '%s' "$out"
}

# --- enumeration: the one fallback there is ------------------------------------
#
# The rule is that a node this repo owns is on the tailnet and its name is the
# whole address, so this is not a second way to reach a machine -- it is what
# answers when the first way says nothing, and it answers by *looking* rather
# than by remembering.
#
# Nothing here is stored. Each call sweeps and reads the neighbour table again,
# because the answer is only true at the moment it is taken.

# Every IPv4 segment this machine is directly on. tailscale0 is excluded: it is
# a /32 on a mesh, with no broadcast domain to sweep and no neighbour table to
# fill, and the tailnet answers for itself.
reach_segments_local() {
    ip -4 -o addr show 2>/dev/null | awk '
        $2 == "lo" || $2 == "tailscale0" { next }
        { split($4, a, "/"); split(a[1], o, ".")
          print o[1] "." o[2] "." o[3] ".0/" a[2] }'
}

# reach_sweep <cidr> -- "<ip> <mac> <state>" for every address on that segment
# the kernel has a hardware address for, after giving it a reason to.
#
# nmap generates the traffic; the *neighbour table* is the answer. That is not a
# detail: unprivileged nmap probes tcp/80 and tcp/443 and calls a host with both
# closed `down` while the board it just ARPed for is sitting there. Reading the
# table instead also makes this indifferent to whether nmap had privilege, which
# is what lets it run as an ordinary user -- `wk` does not call sudo on a
# workstation.
#
# FAILED and INCOMPLETE are dropped: on a segment with DHCP those are stale
# leases, and one board here has been seen holding two addresses at once.
# The vantage is optional and defaults to this machine. A segment this machine is
# not on can only be swept from something that is -- a bridge phone, for the
# cable behind it -- and that is an ssh hop, so it is the caller's to name.
reach_sweep() { # <cidr> [vantage]
    local cidr="$1" van="${2:-}" pre=""
    [ -z "$van" ] || [ "$van" = local ] || pre="ssh -o BatchMode=yes -o ConnectTimeout=${WK_SSH_TIMEOUT:-10} $van"
    if [ -z "$pre" ]; then
        have nmap || return 1
        capped "${WK_SWEEP_TIMEOUT:-60}" nmap -sn -n --host-timeout 5s "$cidr" >/dev/null 2>&1 || true
        ip neigh show 2>/dev/null
    else
        # PATH because nmap and ip live in sbin on some of these, and a login
        # shell is not guaranteed.
        $pre "PATH=\$PATH:/usr/sbin:/sbin
             command -v nmap >/dev/null 2>&1 || exit 66
             nmap -sn -n --host-timeout 5s $(sh_quote "$cidr") >/dev/null 2>&1 || true
             ip neigh show 2>/dev/null" </dev/null
    fi | awk -v net="${cidr%/*}" -v bits="${cidr#*/}" '
        function n(a,   p, v, i) { split(a, p, "."); v = 0
            for (i = 1; i <= 4; i++) v = v * 256 + p[i]
            return v }
        function net_of(a) { return int(n(a) / hosts) }
        BEGIN { hosts = 2 ^ (32 - bits); base = net_of(net) }
        /^[0-9]+\./ && !/FAILED|INCOMPLETE/ {
            ip = $1; mac = ""; state = "?"
            for (i = 2; i <= NF; i++) {
                if ($i == "lladdr") mac = tolower($(i+1))
                else if ($i ~ /^(REACHABLE|STALE|DELAY|PROBE|PERMANENT|NOARP)$/) state = $i
            }
            if (mac == "") next
            if (net_of(ip) != base) next
            print ip, mac, state
        }'
}

# reach_enumerate <mac> -- the address that hardware is at right now, on a
# segment this machine can see, or nothing.
#
# The shared fallback: `wk status` uses it to say where an off-tailnet machine
# is, and `wk find` uses the same sweep to enumerate a whole segment. One
# implementation, so the two cannot disagree about what is on the wire.
reach_enumerate() { # <mac>
    local mac hit seg
    mac=$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z')
    [ -n "$mac" ] || return 1
    have nmap || return 1
    for seg in $(reach_segments_local); do
        hit=$(reach_sweep "$seg" | awk -v m="$mac" '$2 == m { print $1; exit }')
        [ -n "$hit" ] && { printf '%s  (found by sweeping %s -- not stored)' "$hit" "$seg"; return 0; }
    done
    return 1
}

# reach_without_tailnet <machine> -- where it is, when the tailnet does not say.
#
# Two answers, and neither is stored. `ssh -G` is a calculation over the config
# this repo ships -- which, for the machines that cannot be on the tailnet
# (Igalia's build boxes through the gateway, moose's BMC through a phone), is the
# honest answer. For everything else there is no entry at all, so it says nothing
# and the sweep answers instead.
reach_without_tailnet() {
    local m="$1" ssh_path ts_name mac

    # A machine the tailnet answers for needs no second answer, and asking for
    # one costs a sweep. This is called from inside the fleet walk, where each
    # machine has a few seconds -- so an unconditional sweep here does not slow
    # the listing down, it *loses* the line to its own ceiling. Measured: rpi4's
    # row became "no answer within 20s" the moment enumeration ran for every
    # machine, including the ones already up.
    [ -z "$(reach_tailnet "$m")" ] || return 0

    ssh_path=$(reach_ssh "$m") || ssh_path=""
    # `ssh -G` answers with the name itself when no HostName is written down --
    # which for a tailnet machine is MagicDNS, and saying "reach it at its
    # tailnet name" under the heading "without tailscale" is worse than silence.
    ts_name="${ssh_path##*@}"; ts_name="${ts_name%%:*}"; ts_name="${ts_name%%  *}"
    if [ -n "$ssh_path" ] && [ "$ts_name" != "$m" ]; then
        printf '%s' "$ssh_path"
        return 0
    fi

    # The hardware address is the one thing a board keeps across every image it
    # boots, so it is what a sweep can look for. Declared in the machine conf and
    # read, never sourced.
    mac=$(sed -n 's/^MACH_MAC=//p' "$WK_ROOT/boot/machines/$m.conf" 2>/dev/null \
          | tail -1 | tr -d '"'"'"' ')
    [ -n "$mac" ] || return 0

    # Bounded, and honest when the bound is the reason there is no answer. A
    # sweep is seconds-to-minutes and this is a listing; `wk find` is the command
    # whose whole job is to spend that time, so an unfinished sweep points at it
    # rather than reporting the machine absent.
    reach_enumerate "$mac" && return 0
    printf 'not on the tailnet -- wk find %s sweeps for it' "$m"
}
