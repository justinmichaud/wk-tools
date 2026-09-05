# Boot driver: a macOS guest standing in for a machine in bench mode.

BOOT_ARMING=guest

BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

NODE_GUEST="${WK_BENCH_GUEST:-wk-bench}"

BENCH_GUEST_ROOT=/var/wk

# `|| true` / `return 0` throughout: an off guest is a normal state, and cmd/boot runs under set -euo pipefail.
_guest_ip() {
    ( load_target vm >/dev/null 2>&1; _ip "$NODE_GUEST" 2>/dev/null ) || return 1
}

# A guest's address is in no ssh config and changes with every boot, so boot/machines.sh's m_ssh cannot serve here.
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

b_bench_root() { printf '%s' "$BENCH_GUEST_ROOT"; }
b_bench_local() { return 1; }

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

# Via _tart_bin: a non-interactive ssh session's PATH lacks tart's directory, so `command -v tart` answers "no" even with tart running.
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

b_reprovision() {
    cat <<REPROV
wk vm base
    the golden guest every vm workspace is cloned from
wk vm new $NODE_NAME
wk bench stage <ws> --to $NODE_NAME
REPROV
}
