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
#   the fallback    `ssh -G <name>`, which is ssh resolving its own config --
#                   the HostName it would dial, the port, and the ProxyJump it
#                   would go through. That is a calculation, not a copy: edit
#                   the config and this changes with it.
#
#   a bridged board `boot/machines/<m>.conf` says which bridge a board sits
#                   behind, and `bridge/hosts/<bridge>.conf` says which address
#                   that bridge hands it (BR_LEASES, pinned by MAC). Two
#                   declared facts compose into a route, and neither is an
#                   address anybody typed twice.
#
#   mDNS            a `.local` lookup, for the machines that answer to one. Last,
#                   because it is the only one that costs a query on the wire.
#
# Everything here is read-only and bounded. It is called from inside probes that
# already run in parallel under a ceiling (`capped`, lib/common.sh), so nothing
# below may block without one of its own.

# The tailnet's view, once per process. `tailscale status` is a local call to
# the daemon -- no network -- but it is not free, and a fleet listing asks about
# every machine it has.
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
    _WK_TS_PEERS=$( "$cli" status --json 2>/dev/null | python3 -c '
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

# reach_bridged <machine> -- the address a board has on a bridge's segment,
# composed from two declared facts: which bridge the board sits behind
# (boot/machines/<m>.conf, MACH_BRIDGE) and which address that bridge pins for
# it (bridge/hosts/<bridge>.conf, BR_LEASES: <mac> <ip> <name>).
#
# Read, never sourced: a conf is a shell file, and a report has no business
# executing one to print a line of it.
reach_bridged() {
    local m="$1" bridge conf ip
    bridge=$(sed -n 's/^MACH_BRIDGE=//p' "$WK_ROOT/boot/machines/$m.conf" 2>/dev/null \
             | tail -1 | tr -d '"'"'"'')
    [ -n "$bridge" ] || return 0
    conf="$WK_ROOT/bridge/hosts/$bridge.conf"
    [ -f "$conf" ] || return 0
    # BR_LEASES is "<mac>,<ip>,<name>" -- comma-separated, one lease per field,
    # and the whole set may be one quoted block spanning lines. Matched on the
    # name rather than on the MAC, because the machine conf's own MACH_MAC is a
    # third copy of the same fact and matching on it would make this depend on
    # two files agreeing.
    ip=$(sed -n '/^BR_LEASES=/,/"[[:space:]]*$/p' "$conf" 2>/dev/null \
         | tr -d '"' | tr ' \t' '\n\n' | awk -F, -v n="$m" '$3 == n {print $2; exit}')
    [ -n "$ip" ] || { printf 'through %s' "$bridge"; return 0; }
    printf '%s  (through %s)' "$ip" "$bridge"
}

# reach_mdns <name> -- the address a `.local` name resolves to right now.
#
# Last and optional: it is the only lookup here that goes anywhere, and a name
# nothing answers for costs the resolver's own timeout. Bounded on both
# platforms, and silent when there is no answer.
reach_mdns() {
    local name="$1" ip=""
    case "$(wk_os)" in
        macos)
            ip=$(capped 2 dscacheutil -q host -a name "$name.local" 2>/dev/null \
                 | awk '/^ip_address:/ {print $2; exit}') ;;
        *)
            ip=$(capped 2 getent ahostsv4 "$name.local" 2>/dev/null \
                 | awk '{print $1; exit}') ;;
    esac
    [ -n "$ip" ] || return 0
    printf '%s  (mDNS %s.local)' "$ip" "$name"
}

# reach_without_tailnet <machine> -- the best non-tailnet path there is, and
# nothing when there is none.
#
# In order of how much it is worth: a bridge segment address (declared, pinned
# by MAC, and the only way onto that cable), then whatever ssh would dial if
# that is not merely the tailnet name again, then mDNS.
reach_without_tailnet() {
    local m="$1" out ssh_path ts_name
    out=$(reach_bridged "$m") || out=""
    [ -z "$out" ] || { printf '%s' "$out"; return 0; }

    ssh_path=$(reach_ssh "$m") || ssh_path=""
    # `ssh -G` answers with the name itself when no HostName is written down --
    # which for a tailnet machine means MagicDNS, and saying "reach it at its
    # tailnet name" under the heading "without tailscale" is worse than saying
    # nothing.
    ts_name="${ssh_path##*@}"
    ts_name="${ts_name%%:*}"
    ts_name="${ts_name%%  *}"
    if [ -n "$ssh_path" ] && [ "$ts_name" != "$m" ]; then
        printf '%s' "$ssh_path"
        return 0
    fi
    case "$ssh_path" in
        *"through "*) printf '%s' "$ssh_path"; return 0 ;;
    esac

    reach_mdns "$m"
}
