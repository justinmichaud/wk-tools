# Target driver: a disposable macOS VM, for building the Apple ports.
#
# Uses Tart. Two facts shape this driver:
#
#   Apple permits exactly two macOS VMs per host, and Virtualization.framework
#   enforces it -- a third fails with VZErrorDomain code 6. There is no
#   headroom, so t_create refuses at the limit rather than letting podman-style
#   "just make another one" habits produce an opaque failure.
#
#   `tart clone` uses APFS copy-on-write, so cloning a prepared image with
#   Xcode already installed is effectively free. That is why the base image is
#   pulled once and never rebuilt: it sidesteps both the Setup Assistant and a
#   multi-hour Xcode install.

WK_VM_IMAGE="${WK_VM_IMAGE:-ghcr.io/cirruslabs/macos-sequoia-xcode:latest}"
WK_VM_BASE="${WK_VM_BASE:-wk-base}"
WK_VM_MAX="${WK_VM_MAX:-2}"

_vm() { echo "wk-$1"; }

_tart() {
    have tart || die "tart is not installed -- see https://tart.run (FSL-1.1-ALv2; internal use is a Permitted Purpose)"
    tart "$@"
}

t_list() {
    _tart list --format json 2>/dev/null \
        | jq -r '.[] | select(.Name | startswith("wk-")) | "\(.Name)\t\(.State)"' \
        | sed 's/^wk-//'
}

t_info() {
    local v; v=$(_vm "$1")
    _tart list --format json 2>/dev/null \
        | jq -r --arg n "$v" '.[] | select(.Name==$n) | .State' \
        | grep . || echo absent
}

_running_count() {
    _tart list --format json 2>/dev/null \
        | jq -r '[.[] | select(.State=="running")] | length'
}

t_create() {
    local name="$1"
    local v; v=$(_vm "$name")

    local running; running=$(_running_count)
    if [ "${running:-0}" -ge "$WK_VM_MAX" ]; then
        die "$running macOS VMs are already running.
    Apple's licence permits $WK_VM_MAX per host and Virtualization.framework enforces it;
    a third would fail with VZErrorDomain code 6. Stop one first: wk vm stop <name>"
    fi

    # Pull the prepared image once. It carries Xcode, so nothing here has to
    # drive an installer or get past Setup Assistant.
    if ! _tart list --format json | jq -e --arg n "$WK_VM_BASE" '.[]|select(.Name==$n)' >/dev/null 2>&1; then
        info "pulling base image $WK_VM_IMAGE (large, one time only)"
        _tart pull "$WK_VM_IMAGE"
        _tart clone "$WK_VM_IMAGE" "$WK_VM_BASE"
    fi

    info "cloning $WK_VM_BASE -> $v (APFS copy-on-write)"
    _tart clone "$WK_VM_BASE" "$v"
    _tart set "$v" --cpu "$(envelope_cores)" --memory "$(( $(envelope_mem_mb) ))"
}

t_start() {
    local v; v=$(_vm "$1")
    _tart run --no-graphics "$v" &
    info "started $v; waiting for ssh"
    local ip i=0
    while [ $i -lt 60 ]; do
        ip=$(_tart ip "$v" 2>/dev/null || true)
        [ -n "$ip" ] && { echo "$ip"; return 0; }
        sleep 2; i=$((i+1))
    done
    die "$v did not come up"
}

t_exec() {
    local name="$1"; shift
    local v; v=$(_vm "$name")
    local ip; ip=$(_tart ip "$v" 2>/dev/null) || die "$v is not running (wk vm start $name)"
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "admin@$ip" "$@"
}

t_enter() {
    local name="$1"
    local v; v=$(_vm "$name")
    local ip; ip=$(_tart ip "$v" 2>/dev/null) || die "$v is not running (wk vm start $name)"
    exec ssh -t -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "admin@$ip"
}

t_destroy() {
    local name="$1"
    local v; v=$(_vm "$name")
    _tart stop "$v" 2>/dev/null || true
    _tart delete "$v"
    info "deleted VM $v"
}
