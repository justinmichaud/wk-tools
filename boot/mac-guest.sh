# Boot driver: a macOS guest standing in for a machine in bench mode.
#
# Rehearses the bare-metal boot path end to end without taking the workstation
# away from its user. A guest shares its host's CPU and a paravirtualised GPU,
# so runs from here are marked accordingly and are comparable with nothing.
#
# The transition here is scriptable (`wk vm start`), unlike the MBP's, so
# BOOT_ARMING is `guest` -- neither one-shot, server, nor hands-on.

BOOT_ARMING=guest

# NODE_GUEST is a workspace name; t_start/t_stop/_ip map it to the tart VM
# themselves, but _vm_state wants the already-mapped name from _vm(). Passing
# the unmapped name silently answers "absent".

BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# WK_BENCH_GUEST overrides which vm workspace stands in for this machine, so
# two rehearsals can run against different guests.
NODE_GUEST="${WK_BENCH_GUEST:-wk-bench}"

# The path `wk bench staged` expects in bench mode. Created on first delivery;
# the guest's passwordless sudo is needed only for this.
BENCH_GUEST_ROOT=/var/wk

# `|| true` / `return 0` throughout: a guest that is off is a normal state, not a failure.
_guest_ip() {
    ( load_target vm >/dev/null 2>&1; _ip "$NODE_GUEST" 2>/dev/null ) || return 1
}

# A guest's address is not in any ssh config and changes with every boot, so
# the generic m_ssh in boot/machines.sh cannot be reused here.
m_ssh() {
    local ip; ip=$(_guest_ip) || return 1
    ( load_target vm >/dev/null 2>&1
      # shellcheck disable=SC2046 -- deliberate word splitting of the options.
      ssh $(_ssh_opts) "$WK_VM_USER@$ip" "$@" )
}

b_probe() {
    local id
    MODE_CHANNEL=none; MODE=unreachable
    m_ssh true >/dev/null 2>&1 || return 0
    MODE_CHANNEL=host
    id=$(m_ssh 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null' 2>/dev/null | tr -d '\r')
    if [ -n "$id" ]; then MODE="bench $id"; else MODE=host; fi
    return 0
}

# Load-bearing, not defensive: cmd/boot runs under set -euo pipefail, so an
# off guest (normal between runs) would otherwise abort the command silently.
_guest_boot_sec() {
    m_ssh 'sysctl -n kern.boottime' 2>/dev/null \
        | sed -n 's/.*{ *sec *= *\([0-9][0-9]*\).*/\1/p' || true
}

b_booted_at() {
    local sec; sec=$(_guest_boot_sec)
    [ -n "$sec" ] || return 0
    epoch_to_utc "$sec"
}

b_boot_id() { _guest_boot_sec; }

b_evidence() {
    local st
    st=$( load_target vm >/dev/null 2>&1; _vm_state "$(_vm "$NODE_GUEST")" 2>/dev/null || echo unknown )
    echo "guest=$NODE_GUEST (${st:-unknown})"
    m_ssh 'echo "marker=$(sed -n "s/^id=//p" /etc/wk-image 2>/dev/null)"' 2>/dev/null | tr -d '\r' || true
    return 0
}

b_arm() {
    local st
    st=$( load_target vm >/dev/null 2>&1; _vm_state "$(_vm "$NODE_GUEST")" 2>/dev/null )
    [ "$st" = absent ] && die "there is no guest '$NODE_GUEST'.
    Make one from the golden base and mark it as a benchmark install:
        wk vm new $NODE_GUEST
        (then write /etc/wk-image in it: id=, profile=)"
    if [ "$st" != running ]; then
        info "starting guest '$NODE_GUEST'"
        ( load_target vm >/dev/null 2>&1; t_start "$NODE_GUEST" >/dev/null )
    fi
    m_ssh 'test -f /etc/wk-image' 2>/dev/null \
        || die "'$NODE_GUEST' is running but carries no /etc/wk-image, so it is a
    workstation guest and not a benchmark install. A run in it would be refused
    by 'wk bench staged', which is the correct answer -- mark it first."
}

b_reboot() {
    ( load_target vm >/dev/null 2>&1; t_stop "$NODE_GUEST" >/dev/null )
    info "stopped '$NODE_GUEST' -- for a guest, leaving the role is leaving the machine"
}

b_diag() { m_ssh 'cat /var/log/wk-diag.txt 2>/dev/null || echo "(no diag on the guest)"'; }

# --- where a staged payload goes ---------------------------------------------
# Over ssh rather than as a local path: unlike the MBP, whose benchmark disk is
# mounted while staging, every other machine takes the payload over the wire.
b_bench_root() { printf '%s' "$BENCH_GUEST_ROOT"; }
b_bench_local() { return 1; }

# One file, for the manifest that publishes a delivery (cmd/bench, cmd_stage).
b_bench_put_file() {
    local src="$1" dest="$2" ip
    ip=$(_guest_ip) || die "'$NODE_GUEST' is not running"
    ( load_target vm >/dev/null 2>&1
      # shellcheck disable=SC2046 -- deliberate word splitting of the options.
      scp -q $(_ssh_opts) "$src" "$WK_VM_USER@$ip:$dest" )
}

b_bench_put() {
    local src="$1" dest="$2" ip
    ip=$(_guest_ip) || die "'$NODE_GUEST' is not running"
    m_ssh "sudo mkdir -p $(sh_quote "$dest") && sudo chown -R \$(id -un) $(sh_quote "$BENCH_GUEST_ROOT")" \
        || die "could not make $dest in '$NODE_GUEST'"
    ( load_target vm >/dev/null 2>&1
      # shellcheck disable=SC2046 -- deliberate word splitting of the options.
      rsync -a --delete -e "ssh $(_ssh_opts)" "$src/" "$WK_VM_USER@$ip:$dest/" )
}

# Probed through the vm driver's own resolver (_tart_bin), not a bare
# `command -v tart`: a non-interactive ssh session's PATH is just
# /usr/bin:/bin:/usr/sbin:/sbin, so that answers "no" even with tart running.
b_probeable() { is_macos && ( load_target vm >/dev/null 2>&1; _tart_bin >/dev/null 2>&1 ); }

b_media() {
    local st
    if ! is_macos || ! command -v tart >/dev/null 2>&1; then
        printf 'a Tart guest, %s (managed on the macOS host)' "$NODE_GUEST"
        return 0
    fi
    st=$( load_target vm >/dev/null 2>&1; _vm_state "$(_vm "$NODE_GUEST")" 2>/dev/null || echo absent )
    printf 'a Tart guest, %s (%s); no physical media' "$NODE_GUEST" "${st:-unknown}"
}

# No medium to carry: cloned from the golden base and thrown away.
b_reprovision() {
    cat <<REPROV
wk vm base
    the golden guest every vm workspace is cloned from
wk vm new $NODE_NAME
wk bench stage <ws> --to $NODE_NAME
REPROV
}
