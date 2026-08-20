# Boot driver: a macOS guest standing in for a machine in its benchmark role.
#
# Not a machine that boots an image -- a guest that *is* one. It exists for two
# reasons, and the first is the reason it was written:
#
#   the whole path can be rehearsed.  Building in one guest and running in
#   another exercises every step of the bare-metal design -- stage the product
#   across a machine boundary, find it in the other role, run it there, record
#   what it ran on, read the result back -- with nothing hands-on in the middle
#   and nothing that needs a reboot. What it cannot rehearse is the number: a
#   guest shares a CPU with a desktop and its GPU is paravirtualised, which is
#   the whole reason the real role is bare metal. Runs from here are marked
#   accordingly and are not comparable with anything.
#
#   some questions are about the mechanism, not the machine.  "Does the staged
#   tree carry everything run-benchmark needs", "does the payload survive the
#   crossing", "is the record complete" -- all answerable in a guest, in
#   minutes, without taking the workstation away from its user for an hour.
#
# The transition here is scriptable, which is exactly what the MBP's is not:
# putting this machine into its role is `wk vm start`. So the arming model is
# neither one-shot, nor server, nor hands-on -- it is `guest`, and cmd/boot
# treats it as one more way to arrive at the same place.

BOOT_ARMING=guest

BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# The guest, and where its payloads live *inside* it.
MACH_GUEST="${WK_BENCH_GUEST:-wk-bench}"

# The same path a real benchmark install uses, deliberately: `/var/wk` is where
# `wk bench staged` looks when it finds itself in the image role, and a
# rehearsal that used a different path would be rehearsing a different thing.
# It is created on first delivery -- a guest has passwordless sudo, and this is
# the only thing here that needs it.
BENCH_GUEST_ROOT=/var/wk

# Every one of these ends in `|| true` or an explicit `return 0` for the same
# reason: a machine that is off is a normal state, not a failure.
_guest_ip() {
    # Through the vm driver, which owns everything about guests -- their
    # names, their addresses, their ssh options -- rather than reimplementing
    # any of it here.
    ( load_target vm >/dev/null 2>&1; _ip "$MACH_GUEST" 2>/dev/null ) || return 1
}

# Every command to the machine goes over ssh to the guest. The generic m_ssh in
# boot/machines.sh cannot do it: a guest's address is not in anyone's ssh
# config and changes with every boot.
m_ssh() {
    local ip; ip=$(_guest_ip) || return 1
    ( load_target vm >/dev/null 2>&1
      # shellcheck disable=SC2046 -- deliberate word splitting of the options.
      ssh -o BatchMode=yes -o ConnectTimeout="${WK_SSH_TIMEOUT:-10}" \
          $(_ssh_opts) "$WK_VM_USER@$ip" "$@" )
}

b_probe() {
    local id
    ROLE_CHANNEL=none; ROLE=unreachable
    m_ssh true >/dev/null 2>&1 || return 0
    ROLE_CHANNEL=normal
    id=$(m_ssh 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null' 2>/dev/null | tr -d '\r')
    if [ -n "$id" ]; then ROLE="image $id"; else ROLE=workstation; fi
    return 0
}

# `|| true` on both, and it is load-bearing rather than defensive: cmd/boot
# runs under `set -euo pipefail`, so a guest that is merely *off* -- which is
# the normal state of a benchmark machine between runs -- would otherwise end
# the command in silence, with no output and no error, at the first probe.
# (Measured: `wk boot benchvm --status` printed nothing and exited 1.)
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
    st=$( load_target vm >/dev/null 2>&1; _vm_state "$MACH_GUEST" 2>/dev/null || echo unknown )
    echo "guest=$MACH_GUEST (${st:-unknown})"
    m_ssh 'echo "marker=$(sed -n "s/^id=//p" /etc/wk-image 2>/dev/null)"' 2>/dev/null | tr -d '\r' || true
    return 0
}

# Arming is starting the guest, and there is nothing to record on the machine
# that the machine does not already say: it either answers with a marker or it
# does not. So the record stays where cmd/boot puts it and the arming itself is
# a state change nobody has to remember.
b_arm() {
    local st
    st=$( load_target vm >/dev/null 2>&1; _vm_state "$MACH_GUEST" 2>/dev/null )
    [ "$st" = absent ] && die "there is no guest '$MACH_GUEST'.
    Make one from the golden base and mark it as a benchmark install:
        wk vm new $MACH_GUEST
        (then write /etc/wk-image in it -- see docs/HANDOFF-benchmarking.md)"
    if [ "$st" != running ]; then
        info "starting guest '$MACH_GUEST'"
        ( load_target vm >/dev/null 2>&1; t_start "$MACH_GUEST" >/dev/null )
    fi
    m_ssh 'test -f /etc/wk-image' 2>/dev/null \
        || die "'$MACH_GUEST' is running but carries no /etc/wk-image, so it is a
    workstation guest and not a benchmark install. A run in it would be refused
    by 'wk bench staged', which is the correct answer -- mark it first."
}

b_reboot() {
    ( load_target vm >/dev/null 2>&1; t_stop "$MACH_GUEST" >/dev/null )
    info "stopped '$MACH_GUEST' -- for a guest, leaving the role is leaving the machine"
}

b_diag() { m_ssh 'cat /var/log/wk-diag.txt 2>/dev/null || echo "(no diag on the guest)"'; }

# --- where a staged payload goes ---------------------------------------------
#
# Inside the guest, so it is reached over ssh rather than as a path here. That
# is the difference this driver exists to exercise: the MBP's benchmark disk is
# *mounted* while it is being staged onto, and every other machine in the fleet
# is a machine you have to send the payload to.
b_bench_root() { printf '%s' "$BENCH_GUEST_ROOT"; }
b_bench_local() { return 1; }

# One file, for the manifest that publishes a delivery (cmd/bench, cmd_stage).
b_bench_put_file() {
    local src="$1" dest="$2" ip
    ip=$(_guest_ip) || die "'$MACH_GUEST' is not running"
    ( load_target vm >/dev/null 2>&1
      # shellcheck disable=SC2046 -- deliberate word splitting of the options.
      scp -q $(_ssh_opts) "$src" "$WK_VM_USER@$ip:$dest" )
}

b_bench_put() {
    local src="$1" dest="$2" ip
    ip=$(_guest_ip) || die "'$MACH_GUEST' is not running"
    m_ssh "sudo mkdir -p $(sh_quote "$dest") && sudo chown -R \$(id -un) $(sh_quote "$BENCH_GUEST_ROOT")" \
        || die "could not make $dest in '$MACH_GUEST'"
    ( load_target vm >/dev/null 2>&1
      # shellcheck disable=SC2046 -- deliberate word splitting of the options.
      rsync -a --delete -e "ssh $(_ssh_opts)" "$src/" "$WK_VM_USER@$ip:$dest/" )
}
