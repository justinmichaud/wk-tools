# How a machine is reached -- calculated, never written down (CLAUDE.md: a
# second copy of a fact goes stale and reads like broken hardware). Derived
# at read time:
#   the tailnet     `tailscale status --json`. Not cached beyond one run.
#   the config      `ssh -G <name>` -- a calculation, not a copy. Answers
#                   only for machines that genuinely cannot be on the
#                   tailnet: Igalia's build boxes and moose's BMC.
#   enumeration     a sweep for the hardware address on segments this
#                   machine can see (reach_enumerate). Last: the only one
#                   that costs traffic.
# No name lookup below the tailnet: every image this repo builds joins on
# first boot, so a board the tailnet cannot name has no uplink either.
# Read-only and bounded, called from probes that already run in parallel
# under a ceiling (`capped`, lib/common.sh).

# The tailnet's view, once per process, under a ceiling: a wedged
# tailscaled has no timeout of its own and would otherwise hang the walk.
_WK_TS_PEERS=""
_WK_TS_READ=""

wk_tailscale_cli() {
    if have tailscale; then echo tailscale; return 0; fi
    local c=/Applications/Tailscale.app/Contents/MacOS/Tailscale  # App Store build
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
# never heard of it.
reach_tailnet() {
    local name="$1" line
    line=$(wk_tailscale_peers | awk -F'\t' -v n="$name" '$1 == n {print; exit}')
    [ -n "$line" ] || return 0
    printf '%s (%s)' "$(printf '%s' "$line" | cut -f2)" "$(printf '%s' "$line" | cut -f3)"
}

# reach_ssh <name> -- what `ssh <name>` would dial. `ssh -G` performs the
# whole config resolution and prints it without connecting.
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

# For a host whose key cannot be pinned: a fresh one is generated on every
# image write, so pinning would produce a man-in-the-middle warning.
# LogLevel=ERROR: with known-hosts at /dev/null, ssh announces a
# permanently-added key every connection. Lives here, not boot/machines.sh:
# targets/vm.sh needs it too, and boot depending on targets would be a cycle.
_unpinned_host_key_opts() {
    printf '%s' "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
}

# --- enumeration: the one fallback there is ------------------------------------
# What answers when the first way says nothing, by *looking*; nothing here
# is stored.

# Every IPv4 segment this machine is directly on. tailscale0 excluded: a
# /32 on a mesh with no broadcast domain to sweep.
reach_segments_local() {
    ip -4 -o addr show 2>/dev/null | awk '
        $2 == "lo" || $2 == "tailscale0" { next }
        { split($4, a, "/"); split(a[1], o, ".")
          print o[1] "." o[2] "." o[3] ".0/" a[2] }'
}

# reach_sweep <cidr> -- "<ip> <mac> <state>" for every address on that
# segment the kernel has a hardware address for. nmap generates the
# traffic; the *neighbour table* is the answer, not nmap's own verdict --
# unprivileged nmap calls a host with tcp/80 and tcp/443 closed `down`
# while the board it just ARPed for sits there.
#
# FAILED and INCOMPLETE are dropped as stale DHCP leases. The vantage
# defaults to this machine; a segment it's not on can only be swept from
# one that is (a bridge phone, for its cable), so it's the caller's to name.
reach_sweep() { # <cidr> [vantage]
    local cidr="$1" van="${2:-}" pre=""
    [ -z "$van" ] || [ "$van" = local ] || pre="ssh -o BatchMode=yes -o ConnectTimeout=${WK_SSH_TIMEOUT:-10} $van"
    if [ -z "$pre" ]; then
        have nmap || return 1
        capped "${WK_SWEEP_TIMEOUT:-60}" nmap -sn -n --host-timeout 5s "$cidr" >/dev/null 2>&1 || true
        ip neigh show 2>/dev/null
    else
        # nmap and ip live in sbin on some of these; a login shell isn't guaranteed.
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

# reach_enumerate <mac> -- the address that hardware is at right now.
# Shared by `wk status` and `wk find`, so the two can't disagree.
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

# reach_without_tailnet <machine> -- where it is, when the tailnet does not
# say: tries the ssh config's answer first, then falls back to the sweep.
reach_without_tailnet() {
    local m="$1" ssh_path ts_name mac

    # Called from inside the fleet walk, so an unconditional sweep here
    # would lose the line to its own ceiling.
    [ -z "$(reach_tailnet "$m")" ] || return 0

    ssh_path=$(reach_ssh "$m") || ssh_path=""
    # `ssh -G` answers with the name itself when no HostName is written down
    # (MagicDNS) -- worse than silence under "without tailscale".
    ts_name="${ssh_path##*@}"; ts_name="${ts_name%%:*}"; ts_name="${ts_name%%  *}"
    if [ -n "$ssh_path" ] && [ "$ts_name" != "$m" ]; then
        printf '%s' "$ssh_path"
        return 0
    fi

    mac=$(kv_field "$WK_ROOT/boot/machines/$m.conf" MACH_MAC | tr -d '"'"'"' ')
    [ -n "$mac" ] || return 0  # the one thing a board keeps across every image

    reach_enumerate "$mac" && return 0  # an unfinished sweep beats reporting absence
    printf 'not on the tailnet -- wk find %s sweeps for it' "$m"
}
