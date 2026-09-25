# Target driver: a disposable macOS guest on Tart, the read half an unported bash caller loads (load_target vm). Creating, starting, stopping and destroying a guest, and building its base, are lib/wk/targets.py's Vm, lib/wk/guest.py and lib/wk/sysimage/guestbase.py.

WK_VM_USER="${WK_VM_USER:-admin}"

command -v wk_record_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
command -v _unpinned_host_key_opts >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"

WK_STORE="${WK_VM_STORE:-$(wk_record_dir)}"
WK_VM_KEY="$WK_STORE/vm/id_ed25519"

_vm() { echo "wk-$1"; }

# `tart list` mixes local VMs and cached OCI images, spelling Source "local" case-folded.
_vm_query() {
    local bin
    bin=$(tart_bin) || bin=""
    { [ -z "$bin" ] || "$bin" list --format json 2>/dev/null || echo '[]'; } | python3 -c '
import json, sys
q, arg = sys.argv[1], sys.argv[2]
try:
    vms = [v for v in json.load(sys.stdin) if str(v.get("Source", "")).lower() == "local"]
except ValueError:
    vms = []
if q == "state":
    print(next((v.get("State", "absent") for v in vms if v.get("Name") == arg), "absent"))
else:
    for v in vms:
        n = v.get("Name", "")
        if n.startswith("wk-") and n != arg:
            print(n[3:] + "\t" + v.get("State", ""))
' "$@"
}

_vm_state() { _vm_query state "$1"; }

t_src()   { echo "/Users/$WK_VM_USER/WebKit"; }
t_home()  { echo "/Users/$WK_VM_USER"; }
t_tools() { echo "/Users/$WK_VM_USER/wk-tools"; }
t_os()    { echo macos; }

t_mirror_dir() { mirror_in_guest; }

t_list() { _vm_query list "${WK_VM_BASE:-wk-base}"; }

t_created() { [ -f "$(wk_ws_dir "$1")/$WK_READY_MARKER" ]; }

t_info() {
    local st; st=$(_vm_state "$(_vm "$1")")
    [ "$st" = absent ] && { echo absent; return 0; }
    t_created "$1" || { echo creating; return 0; }
    echo "$st"
}

_ip() {
    [ "$(_vm_state "$(_vm "$1")")" = running ] || return 1
    "$(tart_bin)" ip "$(_vm "$1")" --wait 30 2>/dev/null | grep .
}

t_exec() {
    local name="$1"; shift
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk start $name)"
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts_base "$(wk_ssh_timeout)") $(_unpinned_host_key_opts) -o ServerAliveInterval=60 -o ServerAliveCountMax=10 \
        -i "$WK_VM_KEY" "$WK_VM_USER@$ip" "bash -lc $(sh_quote "$(sh_quote "$@")")"
}
