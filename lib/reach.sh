# Calculated, never written down: the tailnet, then `ssh -G` for a machine that cannot be on it, then a sweep for the hardware address, last because it alone costs traffic.
# Peers are read once per process under a ceiling: a wedged tailscaled has no timeout of its own and would hang the walk.
_WK_TS_PEERS=""
_WK_TS_READ=""

wk_tailscale_cli() {
    if have tailscale; then echo tailscale; return 0; fi
    local c=/Applications/Tailscale.app/Contents/MacOS/Tailscale  # App Store build
    [ -x "$c" ] && { echo "$c"; return 0; }
    return 1
}

wk_tailscale_peers() {  # name<TAB>ip<TAB>online, one line per peer plus one for this machine
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

reach_tailnet() {
    local name="$1" line
    line=$(wk_tailscale_peers | awk -F'\t' -v n="$name" '$1 == n {print; exit}')
    [ -n "$line" ] || return 0
    printf '%s (%s)' "$(printf '%s' "$line" | cut -f2)" "$(printf '%s' "$line" | cut -f3)"
}

reach_ssh() {  # <name>: what `ssh <name>` would dial, `ssh -G` resolving the config without connecting
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

# A fresh host key is generated on every image write, so pinning warns of a man-in-the-middle; with known-hosts at /dev/null ssh announces a new key every connection, hence LogLevel=ERROR.
_unpinned_host_key_opts() {
    printf '%s' "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
}

# tailscale0 is excluded: a /32 on a mesh, with no broadcast domain to sweep.
reach_segments_local() {
    ip -4 -o addr show 2>/dev/null | awk '
        $2 == "lo" || $2 == "tailscale0" { next }
        { split($4, a, "/"); split(a[1], o, ".")
          print o[1] "." o[2] "." o[3] ".0/" a[2] }'
}

# nmap generates the traffic but the *neighbour table* is the answer: unprivileged nmap calls a host with tcp/80 and tcp/443 closed `down`. FAILED and INCOMPLETE are stale DHCP leases.
reach_sweep() { # <cidr> [vantage]: "<ip> <mac> <state>" per address the kernel has a hardware address for; a segment this machine is not on is swept from the named vantage
    local cidr="$1" van="${2:-}" pre=""
    [ -z "$van" ] || [ "$van" = local ] || pre="ssh -o BatchMode=yes -o ConnectTimeout=$(wk_ssh_timeout) $van"
    if [ -z "$pre" ]; then
        have nmap || return 1
        capped 60 nmap -sn -n --host-timeout 5s "$cidr" >/dev/null 2>&1 || true
        ip neigh show 2>/dev/null
    else  # nmap and ip live in sbin on some of these, and no login shell is guaranteed
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

reach_without_tailnet() {  # <machine>: the ssh config's answer, else the sweep; the tailnet knows a fleet device by role names that need not be the machine name
    local m="$1" ssh_path ts_name mac n

    for n in "$m" $(kv_field "$WK_ROOT/boot/machines/$m.conf" NODE_SSH | tr -d '"'"'"' ') \
                  $(kv_field "$WK_ROOT/boot/machines/$m.conf" NODE_BENCH_SSH | tr -d '"'"'"' '); do
        [ -z "$(reach_tailnet "$n")" ] || return 0
    done

    ssh_path=$(reach_ssh "$m") || ssh_path=""
    # `ssh -G` answers with the name itself when no HostName is written down (MagicDNS).
    ts_name="${ssh_path##*@}"; ts_name="${ts_name%%:*}"; ts_name="${ts_name%%  *}"
    if [ -n "$ssh_path" ] && [ "$ts_name" != "$m" ]; then
        printf '%s' "$ssh_path"
        return 0
    fi

    mac=$(kv_field "$WK_ROOT/boot/machines/$m.conf" NODE_MAC | tr -d '"'"'"' ')
    [ -n "$mac" ] || return 0  # the one thing a board keeps across every image

    reach_enumerate "$mac" && return 0  # an unfinished sweep beats reporting absence
    printf 'not on the tailnet -- wk find %s sweeps for it' "$m"
}
