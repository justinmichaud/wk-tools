# Target driver: a disposable macOS VM on Tart. `tart clone` is APFS copy-on-write, so a golden base with Xcode is built once and every workspace is a free clone; its WebKit checkout is `--shared` off the host's mirror, mounted in.

. "$WK_ROOT/bench/mac-window-probe.sh"
. "$WK_ROOT/bench/mac-quiet-desktop.sh"
. "$WK_ROOT/bench/mac-pyobjc.sh"

# macOS 26.6.2 with Xcode 27 beta 6 -- ghcr.io/cirruslabs/macos-tahoe-xcode:27-beta-6 as it stood on 2026-09-15. A Cirrus Labs `-xcode` tag is the *Xcode* version, not the macOS one, and the first Saturday of every month re-pushes those tags onto whatever macOS base is newest, so only a digest names one image; the base staleness record hashes this string.
WK_VM_IMAGE="${WK_VM_IMAGE:-ghcr.io/cirruslabs/macos-tahoe-xcode@sha256:f441eb487a18b4588c096adcff5eb48fddca550909e01c472580872b48c166b0}"
WK_VM_BASE="${WK_VM_BASE:-wk-base}"
WK_VM_MAX="${WK_VM_MAX:-2}"
WK_VM_USER="${WK_VM_USER:-admin}"

# The image's admin/admin (Cirrus Labs) is kept: macOS refuses the change from inside the guest, and only the guest's own login window ever asks for it.
WK_VM_PASSWORD="${WK_VM_PASSWORD:-admin}"

vm_login_note() {
    log "  the guest's own window logs in as $WK_VM_USER / $WK_VM_PASSWORD"
    log "  (wk itself uses an ssh key; this is for a prompt on the screen)"
    log "  wk vm check <name>   what is in front of that window, and what is"
    log "                       piling up in it"
}

command -v envelope_mem_mb >/dev/null 2>&1 || . "$WK_ROOT/lib/resources.sh"
command -v wk_record_dir  >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
WK_VM_BASE_CPUS="${WK_VM_BASE_CPUS:-}"
WK_VM_BASE_MEM_MB="${WK_VM_BASE_MEM_MB:-}"
_base_cpus()   { [ -n "$WK_VM_BASE_CPUS" ] && echo "$WK_VM_BASE_CPUS" || envelope_cores; }
_base_mem_mb() { [ -n "$WK_VM_BASE_MEM_MB" ] && echo "$WK_VM_BASE_MEM_MB" || envelope_mem_mb; }

# The prepared image's stock 140 GB does not fit even one build. A ceiling, not an allocation: the disk is sparse and the clones are copy-on-write.
WK_VM_DISK_GB="${WK_VM_DISK_GB:-320}"

# WK_VM_LOGIN_SETTLE: seconds a base's login is watched before its screen is called clear. Measured: Setup Assistant is up 4s after boot, and ssh answers before that.
WK_VM_LOGIN_SETTLE="${WK_VM_LOGIN_SETTLE:-45}"

# Twelve shells measured as nothing wrong; macOS pages below 15% free (memory_pressure).
WK_VM_SHELLS_WARN="${WK_VM_SHELLS_WARN:-12}"
WK_VM_MEM_FREE_WARN_PCT="${WK_VM_MEM_FREE_WARN_PCT:-15}"
WK_VM_SWAP_WARN_MB="${WK_VM_SWAP_WARN_MB:-1024}"

# In points. Tart pins the window's *minimum* content size to it, and AppKit drops fullScreenPrimary once minSize exceeds the screen.
WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"

WK_HOST_FREE_WARN_GB="${WK_HOST_FREE_WARN_GB:-80}"
WK_HOST_FREE_MIN_GB="${WK_HOST_FREE_MIN_GB:-25}"

WK_STORE="${WK_VM_STORE:-$(wk_record_dir)}"
WK_VM_DIR="$WK_STORE/vm"
WK_VM_KEY="$WK_VM_DIR/id_ed25519"

_tart() {
    local bin
    bin=$(tart_bin) || die "tart is not installed.
    Install the signed bundle (it needs the virtualization entitlement, so the
    .app must stay intact):
      mkdir -p ~/.local/share/tart ~/.local/bin
      curl -fsSLO https://github.com/cirruslabs/tart/releases/latest/download/tart.tar.gz
      tar -xzf tart.tar.gz -C ~/.local/share/tart/
      ln -sfn ~/.local/share/tart/tart.app/Contents/MacOS/tart ~/.local/bin/tart
    Licence: FSL-1.1-ALv2; internal use is a Permitted Purpose (README.md, Setup)."
    "$bin" "$@"
}

_vm() { echo "wk-$1"; }

# `tart delete` leaves the `tart run` process alive, and a runner for a VM that no longer exists goes on spending one of the host's VM slots.
_vm_delete() { # <vm name>
    local v="$1" rc=0 pids
    _tart stop "$v" >/dev/null 2>&1 || true
    _tart delete "$v" || rc=$?
    pids=$(_vm_runners "$v")
    if [ -n "$pids" ]; then
        # shellcheck disable=SC2086 -- one signal for the whole list.
        kill $pids 2>/dev/null || true
        pids=$(_vm_runners "$v")
        [ -z "$pids" ] || warn "a 'tart run' for '$v' is still alive (pid $pids) and holds a
    VM slot the next guest needs:  kill -9 $pids"
    fi
    return "$rc"
}

_vm_runners() { # <vm name>
    pgrep -f "tart run .*[[:space:]]$1\$" 2>/dev/null || true
}

# `tart list` mixes local VMs and cached OCI images, spelling Source "local" case-folded.
_vm_json() {
    local bin
    bin=$(tart_bin) || { echo '[]'; return 0; }
    "$bin" list --format json 2>/dev/null || echo '[]'
}

_vm_query() {
    _vm_json | python3 -c '
import json, sys
q, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
vms = [v for v in json.load(sys.stdin) if str(v.get("Source", "")).lower() == "local"]
if q == "state":
    print(next((v.get("State", "absent") for v in vms if v.get("Name") == arg), "absent"))
elif q == "running":
    for v in vms:
        if v.get("State") == "running":
            print(v["Name"])
elif q == "list":
    for v in vms:
        n = v.get("Name", "")
        if n.startswith("wk-") and n != arg:
            print(n[3:] + "\t" + v.get("State", ""))
' "$@"
}

_vm_state()      { _vm_query state "$1"; }

# Virtualization.framework counts every VM on this host against one limit, and the podman machine that carries the container workspaces is one of them -- so a guest is refused by a machine tart cannot see.
_running_vms() {
    _vm_query running | sed 's/^wk-//'
    # An `if`, not `&&`: a stopped podman machine is this function's last command, and under `pipefail` its 1 becomes _running_count's, which ends every caller.
    if _podman_running; then echo "podman machine ${WK_MACHINE:-wk}"; fi
}
_running_count() { _running_vms | awk 'END { print NR }'; }

t_src()   { echo "/Users/$WK_VM_USER/WebKit"; }
t_home()  { echo "/Users/$WK_VM_USER"; }
t_tools() { echo "/Users/$WK_VM_USER/wk-tools"; }

t_mirror_dir() { mirror_in_guest; }

t_list() {
    _vm_query list "$WK_VM_BASE"
}

t_created() { [ -f "$(wk_ws_dir "$1")/$WK_READY_MARKER" ]; }

t_info() {
    local st; st=$(_vm_state "$(_vm "$1")")
    [ "$st" = absent ] && { echo absent; return 0; }
    t_created "$1" || { echo creating; return 0; }
    echo "$st"
}

t_ssh_host() {
    local ip; ip=$(_ip "$1") || return 1
    echo "$WK_VM_USER@$ip"
}

WK_VM_AGENT_RW_SHARE=agent-rw   # the claude.ai login the CLI rotates in place, so every holder here reads one set of bytes (wk_agent_rw_dir); the other share is the mirror (WK_VM_MIRROR_SHARE)
_agent_rw_guest_dir() { guest_share_dir "$WK_VM_AGENT_RW_SHARE"; }

t_os() { echo macos; }

t_create() {
    local name="$1" why
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" = absent ] || die "workspace '$name' already exists"
    [ -d "$(wk_mirror)" ] || die "no WebKit mirror on this machine for '$name' to clone its checkout from
    ($(wk_mirror) does not exist):  wk sync    makes it"

    _ensure_base

    if why=$(vm_base_stale); then
        [ -z "${WK_VM_FORCE:-}" ] \
            || warn "WK_VM_FORCE=1 -- '$name' is cloned from a base that
  predates its own provisioning inputs: $why"
        [ -n "${WK_VM_FORCE:-}" ] \
            || die "'$WK_VM_BASE' predates its own provisioning inputs: $why.
  '$name' would be a clone of it, carrying the desktop settings of the day it
  was sealed -- which is how a guest comes up behind Setup Assistant, where
  nothing in the guest can clear it:
      wk vm base --rebuild     hours; existing guests are unaffected
  WK_VM_FORCE=1 clones it anyway."
    fi

    local running; running=$(_running_count)
    if [ "${running:-0}" -ge "$WK_VM_MAX" ]; then
        warn "$running VM(s) already running on this host; you will have to stop one before starting '$name':
$(_running_vms | sed 's/^/      /')"
    fi

    info "cloning $WK_VM_BASE -> $v (APFS copy-on-write)"
    local t0; t0=$(date +%s)
    local cpus mem
    cpus=$(_vm_cpus)
    mem=$(_vm_mem_mb)
    _tart clone "$WK_VM_BASE" "$v"
    # Set.swift assigns displayRefit unconditionally, so a `tart set` omitting --display-refit clears it. The serial is left alone: a changed one is a new machine to macOS, so it re-runs Setup Assistant, whose account pane a clone cannot answer (Apple's servers sit behind the egress filter) -- measured A/B 2026-09-09, one clone, one flag. The MAC is randomised because two guests on a network need distinct ones, and it does not move the pane.
    _tart set "$v" --cpu "$cpus" --memory "$mem" --random-mac \
        --display "$WK_VM_DISPLAY" --display-refit
    debug "clone took $(( $(date +%s) - t0 ))s"

    [ $(( $(date +%s) - t0 )) -gt 60 ] && \
        warn "the clone took $(( $(date +%s) - t0 ))s -- APFS copy-on-write may not be in play; check disk use"

    ensure_dir "$(wk_ws_dir "$name")"

    : > "$(wk_ws_dir "$name")/$WK_READY_MARKER"
}

_settle_desktop() { # <name> <ip>
    {
        printf 'WK_VM_PASSWORD=%s\n' "$(sh_quote "$WK_VM_PASSWORD")"
        wk_quiet_desktop_script
        cat "$WK_ROOT/bench/mac-pyobjc.sh" "$WK_ROOT/vm/desktop.sh"
    } | _ssh "$2" "bash -s" >/dev/null
}

# Read once and judged once, so what is shown is what the refusal was decided on.
_report_desktop() { # <name>
    local probe blocked
    probe=$(vm_desktop_probe "$1" 2>/dev/null) || return 0
    [ -n "$probe" ] || return 0
    log "  the guest's desktop, as it is now ('wk vm check $1' asks again):"
    render_findings <<FINDINGS || true
$(vm_desktop_findings "$probe")
FINDINGS

    blocked=$(vm_desktop_blockers "$probe")
    [ -n "$blocked" ] || return 0
    if [ -n "${WK_VM_FORCE:-}" ]; then
        warn "WK_VM_FORCE=1 -- '$1' is handed over with this in front of its desktop:
$(printf '%s' "$blocked" | sed 's/^/      /')"
        return 0
    fi
    die "'$1' is not usable: something is in front of its desktop.
$(printf '%s' "$blocked" | sed 's/^/      /')
    A clone cannot clear this itself -- Setup Assistant's account pane needs
    Apple's servers, and a guest's egress filter refuses them. It is cleared
    once, on the base every guest is cloned from:
      wk vm base --rebuild     hours; then re-create this guest
    WK_VM_FORCE=1 hands the guest over anyway."
}

# The only place a `tart run` states why it died -- "The number of VMs exceeds the system limit" is printed here and nowhere a person looks.
_runlog_tail() { # <path>
    if [ -s "$1" ]; then
        tail -5 "$1" | sed 's/^/      /'
        printf '    (%s)\n' "$1"
    else
        printf '    nothing -- %s is empty\n' "$1"
    fi
}

t_exec() {
    local name="$1"; shift
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    # ssh joins arguments with spaces: one already-quoted string, in a login shell for PATH.
    local cmd; cmd=$(sh_quote "$@")
    _ssh "$ip" "bash -lc $(sh_quote "$cmd")"
}

t_enter() {
    local name="$1"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    exec ssh -t $(_ssh_opts) "$WK_VM_USER@$ip" "cd $(t_src "$name") 2>/dev/null; exec \$SHELL -l"
}

_write_claude_config() {
    local name="$1" ip="$2" tools; tools=$(t_tools "$name")
    _ssh "$ip" "[ -d $(sh_quote "$tools/claude") ] || exit 1
        mkdir -p \$HOME/.claude
        for f in settings.json hooks CLAUDE.md skills; do
            ln -sfn $(sh_quote "$tools/claude")/\$f \$HOME/.claude/\$f
        done"
}

# A guest's checkout is made at first start, --shared from the host's mirror on its share, and converged on every start under the injected credential: the identity include, the wiring, then `git-webkit setup --defaults`, which no-ops once webkitscmpy.setup is true. The generated halves are piped, never expanded into a heredoc: they carry `$` of their own.
_write_checkout() { # <name> <ip>
    local name="$1" ip="$2" src mirror out rc=0 t0
    src=$(t_src "$name"); mirror=$(t_mirror_dir "$name")
    t0=$(date +%s)
    out=$({
        cat <<'EOF'
set -u
git config --global --replace-all include.path "$WK_TOOLS/dotfiles/gitconfig"
if [ -d "$WK_SRC/.git" ]; then
    echo checkout=present
elif [ ! -d "$WK_MIRROR" ]; then
    echo "checkout=no-mirror: $WK_MIRROR is not there. The share is mounted at boot, so"
    echo "  'wk vm stop', then 'wk vm start' -- or the host has no mirror yet: wk sync"
    exit 1
elif git clone --quiet --shared --branch main "$WK_MIRROR" "$WK_SRC"; then
    echo checkout=cloned
else
    echo checkout=clone-failed; exit 1
fi
[ ! -r "$HOME/.wk-egress" ] || . "$HOME/.wk-egress"
EOF
        wk_wiring_script "$src" "$mirror"
        wk_gitwebkit_setup_script "$src"
    } | _ssh "$ip" "env WK_SRC=$(sh_quote "$src") WK_MIRROR=$(sh_quote "$mirror") \
                        WK_TOOLS=$(sh_quote "$(t_tools "$name")") bash -s" 2>&1 | tr -d '\r') || rc=$?
    case "$out" in
        *checkout=cloned*) info "$name's WebKit checkout made from its mirror in $(( $(date +%s) - t0 ))s" ;;
    esac
    case "$out" in
        *setup=ok*) info "git-webkit is set up in $name" ;;
    esac
    [ "$rc" -eq 0 ] || printf '%s\n' "$out" | tail -5 | sed 's/^/    /' >&2
    return "$rc"
}

_install_claude_cli() { # <name> <ip>
    local name="$1" ip="$2" out
    out=$(wk_claude_cli_script | _ssh "$ip" "sh -s" 2>/dev/null | tr -d '\r') || return 1
    case "$out" in claude=installed) info "Claude CLI installed in $name" ;; esac
}

_write_lldbinit() {
    local name="$1" ip="$2"
    {
        printf '%s\n' "# wk: written by targets/vm.sh. See wk run --lldb, wk gui --lldb."
        printf '%s\n' "command script import $(t_src "$name")/Tools/lldb/lldb_webkit.py"
        cat "$WK_ROOT/dotfiles/lldbinit"
    } | _ssh "$ip" "cat > \$HOME/.lldbinit"
}

_write_shell_rc() { # <name> <ip>
    _ssh "$2" "bash -s $(sh_quote "$(t_tools "$1")") $(sh_quote "$(_agent_rw_guest_dir)")" < "$WK_ROOT/vm/shell-rc.sh"
}

command -v _tools_py >/dev/null 2>&1 || . "$WK_ROOT/lib/tools.sh"

# Start, stop and the clock are lib/wk/guest.py; this driver's own store is WK_VM_STORE there.
_guest_py() { WK_VM_STORE="$WK_STORE" WK_STORE="$(wk_machine_store)" PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" python3 -m wk.guest "$@"; }
_set_guest_clock() { _guest_py clock "$@"; }   # <name> <ip>

# A git bundle of this tree's HEAD (lib/wk/tools.py) rather than a mount: a guest's shares are the agent-rw directory and the mirror (lib/wk/guest.py's boot).
_guest_tools_push() { # <name> <ip>
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    _tools_py push "$(t_tools "$1")" "$WK_VM_USER@$2" $(_ssh_opts)
}

t_destroy() {
    local name="$1"
    local v; v=$(_vm "$name")

    [ "$v" != "$WK_VM_BASE" ] || die "refusing to delete the golden base (wk vm base --rebuild)"

    if [ "$(_vm_state "$v")" != absent ]; then
        _vm_delete "$v"
        info "deleted VM $v"
    fi

    local ws; ws=$(wk_ws_dir "$name")
    [ -d "$ws" ] && { rm -rf "$ws"; info "removed $ws"; }
    rm -f "$WK_VM_DIR/$name.run.log"
    rm -f "$WK_VM_DIR/$name.unfiltered"
}

command -v _unpinned_host_key_opts >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"

_ssh_opts() {
    # ServerAliveInterval: builds go quiet for long stretches, and without keepalives a NAT timeout drops the connection mid-build and reports it as a build failure.
    printf '%s' "$(_ssh_opts_base "$(wk_ssh_timeout)") $(_unpinned_host_key_opts) \
-o ServerAliveInterval=60 -o ServerAliveCountMax=10 -i $WK_VM_KEY"
}

_ssh() {
    local ip="$1"; shift
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts) "$WK_VM_USER@$ip" "$@"
}

# `ip` is the caller's own local, in scope through bash's dynamic scoping.
_base_ssh() { _ssh "$ip" "$@"; }

_ip() {
    local v; v=$(_vm "$1")
    [ "$(_vm_state "$v")" = running ] || return 1
    _tart ip "$v" --wait 30 2>/dev/null | grep .
}

_wait_ssh() {
    local ip="$1" i=0
    while [ "$i" -lt 60 ]; do
        _ssh "$ip" true 2>/dev/null && return 0
        sleep 2; i=$((i+1))
    done
    return 1
}

_host_free_gb() {
    df -g / 2>/dev/null | awk 'NR==2 {print $4}'
}

_check_host_disk() {
    local free; free=$(_host_free_gb)
    [ -n "$free" ] || return 0

    if [ "$free" -lt "$WK_HOST_FREE_MIN_GB" ]; then
        die "only ${free} GB free on the host.
    A macOS guest's disk is sparse: the guest believes it has ${WK_VM_DISK_GB} GB,
    but every byte it writes has to come from here. At this point a build will
    fail as an I/O error inside the guest, which names nothing useful.

      wk vm ls                     what exists
      wk rm <name>                 reclaim a workspace
      tart prune --space-budget 0  drop the OCI image cache"
    elif [ "$free" -lt "$WK_HOST_FREE_WARN_GB" ]; then
        warn "${free} GB free on the host -- a Release build tree is ~39 GB and a Debug one ~78 GB, so this may not be enough to finish"
    fi
}

_check_guest_limit() {
    local running; running=$(_running_count)
    [ "${running:-0}" -lt "$WK_VM_MAX" ] && return 0
    die "$running VM(s) are already running on this host:
$(_running_vms | sed 's/^/      /')
    Virtualization.framework permits $WK_VM_MAX and refuses the next one with
    VZErrorDomain code 6, in that guest's run log and nowhere else. Free a slot
    with 'wk vm stop <name>', or with 'podman machine stop ${WK_MACHINE:-wk}' --
    that machine carries the container workspaces, which survive it being down."
}

_podman_mem_mb() {
    have podman || { echo 0; return; }
    podman machine inspect "${WK_MACHINE:-wk}" --format '{{.Resources.Memory}}' 2>/dev/null || echo 0
}

_podman_running() {
    have podman || return 1
    [ "$(podman machine inspect "${WK_MACHINE:-wk}" --format '{{.State}}' 2>/dev/null)" = running ]
}

# An unreadable answer counts as busy: stopping a machine underneath something is the mistake this guards.
_podman_containers_running() {
    _podman_running || { echo 0; return 0; }
    podman machine ssh "${WK_MACHINE:-wk}" -- 'podman ps -q | grep -c . || true' \
        </dev/null 2>/dev/null | tr -dc '0-9' | grep . || echo 1
}

_vm_cpus()   { [ -n "${WK_VM_CPUS:-}" ] && echo "$WK_VM_CPUS" || envelope_cores; }
_vm_mem_mb() { [ -n "${WK_VM_MEM_MB:-}" ] && echo "$WK_VM_MEM_MB" || envelope_mem_mb; }

_vm_get() {
    _tart get "$1" --format json 2>/dev/null | python3 -c '
import json, sys
v = json.load(sys.stdin).get(sys.argv[1])
print("" if v is None else v)' "$2"
}

_vm_configured() { _vm_get "$(_vm "$1")" "$2"; }

_committed_mem_mb() {
    local skip="${1:-}" total=0 v m
    for v in $(_vm_query running); do
        [ -n "$skip" ] && [ "$v" = "$skip" ] && continue
        m=$(_vm_get "$v" Memory)
        [ -n "$m" ] && total=$(( total + m ))
    done
    echo "$total"
}

t_cores() {
    local c; c=$(_vm_configured "$1" CPU)
    [ -n "$c" ] && { echo "$c"; return 0; }
    _vm_cpus
}

t_mem_mb() {
    local m; m=$(_vm_configured "$1" Memory)
    [ -n "$m" ] && { echo "$m"; return 0; }
    _vm_mem_mb
}

_check_memory_budget() {
    local name="$1" mine="$2" podman_mb guests total budget spare total_mb
    podman_mb=0
    _podman_running && podman_mb=$(_podman_mem_mb)
    guests=$(_committed_mem_mb "$name")
    total=$(( mine + podman_mb + guests ))
    budget=$(envelope_mem_mb)

    [ "$total" -le "$budget" ] && return 0

    # An idle podman machine is not a reason to refuse a guest: it holds the whole envelope whether or not anything runs in it, and `wk` starts it again on the next container command.
    if [ "$podman_mb" -gt 0 ] && [ "$(_podman_containers_running)" = 0 ] \
       && [ $(( mine + guests )) -le "$budget" ]; then
        info "stopping the idle podman machine to free ${podman_mb}MB for '$name'"
        podman machine stop "${WK_MACHINE:-wk}" >/dev/null 2>&1 || true
        _podman_running || return 0
        warn "the podman machine did not stop; '$name' may not fit"
    fi
    _podman_running && podman_mb=$(_podman_mem_mb) || podman_mb=0
    total=$(( mine + podman_mb + guests ))
    [ "$total" -le "$budget" ] && return 0

    if [ -n "${WK_VM_SHARE:-}" ]; then
        warn "'$name' (${mine}MB) on top of ${podman_mb}MB podman + ${guests}MB of running guests exceeds the ${budget}MB envelope -- continuing because WK_VM_SHARE is set"
        return 0
    fi

    spare=$(( budget - podman_mb - guests ))
    total_mb=$(host_mem_mb)

    local advice
    if [ "$spare" -ge 4096 ]; then
        advice="      WK_VM_MEM_MB=$spare, then retry
          run it in the ${spare}MB that is actually free"
    else
        advice="      (only ${spare}MB is unspoken for, which is not enough to build in --
       freeing one of the above is the realistic option)"
    fi

    local rows=""
    [ "$podman_mb" -gt 0 ] && rows="$rows
      $(printf '%-26s %6s MB   running' "podman machine '${WK_MACHINE:-wk}'" "$podman_mb")"
    [ "$guests" -gt 0 ] && rows="$rows
      $(printf '%-26s %6s MB   running' "other macOS guest(s)" "$guests")"
    rows="$rows
      $(printf '%-26s %6s MB   requested' "macOS VM '$name'" "$mine")
      $(printf '%-26s %6s MB   (%s MB total, %s MB kept for the desktop)' \
            'host envelope' "$budget" "$total_mb" "$WK_RESERVE_MB")"

    die "not enough memory to start '$name'.
$rows

    Everything holding memory holds all of it, whether or not it is busy, so
    this refuses rather than letting you find out during a link.

      podman machine stop ${WK_MACHINE:-wk}
          free the whole envelope (workspaces and their state survive)
      wk vm stop <name>
          free a running guest
$advice
      WK_VM_SHARE=1, then retry
          proceed anyway"
}

_base_exists() { [ "$(_vm_state "$WK_VM_BASE")" != absent ]; }

_base_marker() { echo "$WK_VM_DIR/base.ready"; }

_base_ready() { _base_exists && [ -f "$(_base_marker)" ]; }

# The inputs that produced this base, as one hash: vm_base_stale recomputes and compares on every read, so a script edited here makes every base built before it read stale at once.
_base_inputs_hash() {
    {
        cat "$WK_ROOT/vm/provision-base.sh" "$WK_ROOT/vm/desktop.sh" "$WK_ROOT/bench/mac-pyobjc.sh"
        printf 'image=%s\nuser=%s\n' "$WK_VM_IMAGE" "$WK_VM_USER"
    } | python3 -c 'import hashlib,sys
print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])'
}

vm_base_stale() {
    local rec
    rec=$(marker_field "$(_base_marker)" inputs)
    if [ -z "$rec" ]; then
        echo "provisioned before this record existed"
        return 0
    fi
    [ "$rec" = "$(_base_inputs_hash)" ] && return 1
    echo "vm/provision-base.sh, vm/desktop.sh, bench/mac-pyobjc.sh, WK_VM_IMAGE or WK_VM_USER has changed since it was built"
    return 0
}

vm_base_findings() {
    local why
    _f() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }

    if ! _base_exists; then
        _f wrong "no golden base VM '$WK_VM_BASE' -- there is nothing for a guest to be cloned from" \
                 "wk vm base   (hours: the image pull, Xcode's first launch)"
    elif [ ! -f "$(_base_marker)" ]; then
        _f wrong "'$WK_VM_BASE' exists but provisioning never finished in it" \
                 "wk vm base --refresh   (re-runs provisioning; nothing is re-downloaded)"
    elif why=$(vm_base_stale); then
        _f wrong "'$WK_VM_BASE' predates its own provisioning inputs: $why -- every guest cloned from it carries what that base was built with" \
                 "wk vm base --rebuild   (hours; existing guests are unaffected)"
    else
        _f ok "golden base '$WK_VM_BASE' matches its provisioning inputs"
    fi

    unset -f _f
    return 0
}

_base_mark_ready() {
    ensure_dir "$WK_VM_DIR" 0700 >/dev/null
    printf 'image=%s
inputs=%s
finished=%s
' \
        "$WK_VM_IMAGE" "$(_base_inputs_hash)" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$(_base_marker)"
}

_ensure_base() {
    _base_ready && return 0

    if _base_exists; then
        warn "'$WK_VM_BASE' exists but was never finished (no completion marker)"
        log  "  destroying it and starting again -- an unprovisioned base is rubble,"
        log  "  and every workspace cloned from one inherits whatever it is missing."
        _vm_delete "$WK_VM_BASE" 2>/dev/null || true
    fi
    rm -f "$(_base_marker)"

    if ! _tart list --format json --source oci 2>/dev/null | python3 -c '
import json, sys
sys.exit(0 if any(v.get("Name") == sys.argv[1] for v in json.load(sys.stdin)) else 1)' "$WK_VM_IMAGE"; then
        info "pulling $WK_VM_IMAGE -- tens of GB, once only"
        _tart pull "$WK_VM_IMAGE"
    fi

    info "creating the golden base VM '$WK_VM_BASE'"
    local cpus mem
    cpus=$(_base_cpus)
    mem=$(_base_mem_mb)
    _tart clone "$WK_VM_IMAGE" "$WK_VM_BASE"
    _tart set "$WK_VM_BASE" --cpu "$cpus" --memory "$mem"

    _provision_base
}

# The base boots with the open network every time, and a workspace never does: provisioning installs from PyPI, and Setup Assistant's account pane needs Apple's servers. Booting it once each way changes its subnet, and `tart ip` answers with the lease it had before.
_start_base() { # -> ip
    local runlog="$WK_VM_DIR/base.run.log"
    if [ "$(_vm_state "$WK_VM_BASE")" != running ]; then
        nohup "$(tart_bin)" run --no-graphics "$WK_VM_BASE" >"$runlog" 2>&1 &
        disown 2>/dev/null || true
        info "booting the base VM (log: $runlog)"
    fi
    _tart ip "$WK_VM_BASE" --wait 300 2>/dev/null | grep . \
        || die "base VM did not boot. Its run log says:
$(_runlog_tail "$runlog")"
}

_provision_base() {
    local cpus mem
    cpus=$(_base_cpus)
    mem=$(_base_mem_mb)
    _check_guest_limit
    _check_memory_budget "$WK_VM_BASE" "$mem"
    ensure_dir "$WK_VM_DIR" 0700

    # tart can only grow a disk while the VM is off.
    local cur; cur=$(_vm_get "$WK_VM_BASE" Disk)
    if [ -n "$cur" ] && [ "$cur" -lt "$WK_VM_DISK_GB" ]; then
        info "growing the base disk ${cur}GB -> ${WK_VM_DISK_GB}GB"
        _tart set "$WK_VM_BASE" --disk-size "$WK_VM_DISK_GB"
    fi
    _tart set "$WK_VM_BASE" --cpu "$cpus" --memory "$mem"
    _check_host_disk

    if [ ! -f "$WK_VM_KEY" ]; then
        ssh-keygen -q -t ed25519 -N '' -C wk-vm -f "$WK_VM_KEY"
        changed "generated the macOS VM ssh key"
    fi

    # A first boot has Setup Assistant work to get through, hence the longer wait than a guest start uses.
    local runlog="$WK_VM_DIR/base.run.log"
    local ip; ip=$(_start_base)

    # The guest agent is how the key gets in the FIRST time, without typing the default password; `tart ip --wait` answers before the agent is listening.
    if _wait_ssh "$ip"; then
        debug "ssh already works in '$WK_VM_BASE'; no key to install"
    else
        info "installing the wk ssh key over the guest agent"
        local pub; pub=$(cat "$WK_VM_KEY.pub")
        _tart exec "$WK_VM_BASE" /bin/sh -c \
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
             grep -qxF '$pub' ~/.ssh/authorized_keys 2>/dev/null || \
             echo '$pub' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys" \
        || die "could not reach the guest agent in '$WK_VM_BASE'.
    \`tart exec\` needs the Tart guest agent, which the Cirrus Labs images ship
    but a vanilla macOS image does not. Without it there is no way to get an ssh
    key in unattended -- log in once with the image's own credentials and append
    $WK_VM_KEY.pub to ~/.ssh/authorized_keys by hand, then re-run."

        _wait_ssh "$ip" || die "ssh key was installed but ssh still refuses. Its run log says:
$(_runlog_tail "$runlog")"
    fi

    # Before anything here speaks TLS: provisioning's first act is an HTTPS clone, which a stale clock fails as a not-yet-valid certificate. A refusal, since a base sealed at the wrong date hands it to every clone.
    _set_guest_clock "$WK_VM_BASE" "$ip" \
        || die "could not set the clock in '$WK_VM_BASE'. Passwordless sudo is what it
    needs, and the base is built from the image WK_VM_IMAGE names -- check that
    image rather than patching the guest:  ssh into it and run  sudo -n true"

    info "provisioning the base VM (Xcode licence, disk, desktop)"
    _guest_tools_push "$WK_VM_BASE" "$ip" \
        || die "the base cannot be provisioned without wk-tools in it (see above)"
    vm_login_note
    # Detached and polled, not a foreground `ssh <long command>`: provisioning is minutes, and a dropped connection (measured 2026-09-04: "Read from remote host: Connection reset by peer") takes a foreground run with it.
    command -v detach_remote >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    local plog="/tmp/wk-base-provision.log" prc="/tmp/wk-base-provision.rc"
    detach_remote _base_ssh "$plog" "$prc" -- \
        env WK_VM_DISPLAY="$WK_VM_DISPLAY" WK_VM_USER="$WK_VM_USER" \
            WK_VM_PASSWORD="$WK_VM_PASSWORD" \
            bash "$(t_tools "$WK_VM_BASE")/vm/provision-base.sh" \
        || die "could not start base provisioning in '$WK_VM_BASE'"
    local prov_rc; prov_rc=$(detach_wait_remote _base_ssh "$plog" "$prc")
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$WK_VM_USER@$ip:$plog" "$WK_VM_DIR/base-provision.log" 2>/dev/null || true
    [ "$prov_rc" = 0 ] || die "base provisioning failed (rc=$prov_rc).
    What it printed is in $WK_VM_DIR/base-provision.log; the base is rubble
    until this finishes, and a re-run starts it again:  wk vm base --refresh"

    _unblock_desktop "$ip" || die "Setup Assistant is still on '$WK_VM_BASE''s screen.
    What it printed is above; the base is not sealed behind a pane, because
    every guest cloned from it would come up behind one too. It is running at
    $ip -- answer it at its own window, then  wk vm base --refresh"
    # After it, not only before: the flow turns diagnostic submission on.
    _settle_desktop "$WK_VM_BASE" "$ip" || warn "could not re-settle the base's desktop after Setup Assistant"

    # A dismissed pane comes back at the next login, so the base is judged on the screen it boots into, never on the one the flow left behind.
    info "rebooting the base to prove its screen comes up clear"
    _tart stop "$WK_VM_BASE"
    ip=$(_start_base)
    _wait_ssh "$ip" || die "'$WK_VM_BASE' rebooted to $ip but ssh never answered, so
    the screen it came up with cannot be read. Its run log says:
$(_runlog_tail "$WK_VM_DIR/base.run.log")"
    _wait_login_settled "$ip" \
        || die "Setup Assistant came back at '$WK_VM_BASE''s next login, so the flow
    that answered it did not finish -- every guest cloned from this base would
    come up behind it. The base is running at $ip: answer it at its own window,
    then  wk vm base --refresh"
    _check_base_screen "$ip"

    info "shutting the base VM down"
    _tart stop "$WK_VM_BASE"
    _base_mark_ready
    changed "golden base VM '$WK_VM_BASE' is ready"
}

# Driven over the Accessibility API: no preference the guest can write stops the pane (vm/desktop.sh), and the API answers a plain ssh session because the guest runs with SIP disabled.
_setup_assistant_state() { # <ip>
    local n   # `pgrep -c` is Linux-only; macOS pgrep refuses it, and a `|| true` made that look like a count of nothing.
    n=$(_ssh "$1" "pgrep -f 'Setup Assistant.app/Contents/MacOS' | grep -c . || true" 2>/dev/null \
        | tr -d '\r') || { echo unreachable; return 0; }
    case "$n" in
        "")  echo unreachable ;;
        0)   echo gone ;;
        *)   echo up ;;
    esac
}

_unblock_desktop() { # <ip>
    [ "$(_setup_assistant_state "$1")" = up ] || return 0
    info "driving Setup Assistant off the screen over the Accessibility API"
    _ssh "$1" '/usr/bin/python3 -' < "$WK_ROOT/vm/desktop-unblock.py" || return 1
    [ "$(_setup_assistant_state "$1")" = gone ]
}

# ssh answers before the login has drawn anything and Setup Assistant arrives seconds later, so a screen read straight after boot reads clear whatever is coming.
_wait_login_settled() { # <ip>
    local i=0
    while [ "$i" -lt "$WK_VM_LOGIN_SETTLE" ]; do
        [ "$(_setup_assistant_state "$1")" != up ] || return 1
        sleep 3; i=$((i + 3))
    done
    return 0
}

_check_base_screen() { # <ip>
    local reading uninvited
    reading=$( { wk_quiet_desktop_script; cat "$WK_ROOT/bench/mac-window-probe.sh"
                 echo wk_window_probe
               } | _ssh "$1" 'bash -s' 2>/dev/null | sed -n 's/^windows=//p') || reading=""
    [ -n "$reading" ] && [ "$reading" != '?' ] \
        || die "could not ask '$WK_VM_BASE' what is on its screen, and a base is not
    sealed unread: every guest cloned from it would come up behind whatever is
    there. The base is still running at $1 -- 'wk vm base --refresh' re-runs this."
    uninvited=$(wk_window_unexpected "$reading")
    [ -n "$uninvited" ] || { info "the base's screen is clear, so a clone's will be too"; return 0; }
    die "on the base's screen, and nothing wk put there: ${uninvited%;}
    Every guest cloned from this base comes up behind it, and a clone cannot
    clear it itself. The base is running now, at $1: answer it at its own
    window, then  wk vm base --refresh"
}

# What is in front of the desktop, one reason per line -- the faults a guest is refused over. A guest with the wrong pyobjc still builds; one behind Setup Assistant does not.
vm_desktop_blockers() { # <probe output>
    local probe="$1" v
    _v() { printf '%s\n' "$probe" | sed -n "s|^$1=||p" | tail -1; }

    v=$(_v windows)
    if [ -n "$v" ] && [ "$v" != '?' ]; then
        v=$(wk_window_unexpected "$v")
        [ -z "$v" ] || printf 'a window nothing here put there: %s\n' "${v%;}"
    fi
    [ "$(_v securityagent)" != up ] || printf 'an authentication sheet (SecurityAgent) is up\n'
    case "$(_v console_user)" in
        root|""|"?") printf 'nobody is logged in at the window, so there is no desktop\n' ;;
    esac
    [ "$(_v screenlock)" != on ] || printf 'the screen lock is on, so the guest comes up asking for a password\n'

    unset -f _v
    return 0
}

vm_desktop_findings() { # <probe output>
    local probe="$1" v
    _f() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }
    _v() { printf '%s\n' "$probe" | sed -n "s|^$1=||p" | tail -1; }
    local rebuild="wk vm base --rebuild, then re-create this guest"
    local restart="wk vm stop <name> && wk vm start <name>"

    v=$(_v console_user)
    case "$v" in
        root|""|"?") _f wrong "nobody is logged in at the window (console user '$v') -- there is no desktop to draw on" \
                              "$restart  (auto-login logs it back in; the account is a base setting)" ;;
        *)           _f ok "logged in at the window as $v" ;;
    esac

    v=$(_v pyobjc)
    case "$v" in
        "$WK_PYOBJC_VERSION") _f ok "pyobjc $v: a browser can be driven and held in front here" ;;
        ""|"?")  _f wrong "no pyobjc: run-benchmark cannot size the screen and nothing can keep MiniBrowser frontmost, so a benchmark here measures a throttled browser" \
                          "$restart  (the settle installs it)" ;;
        *)       _f wrong "pyobjc here is $v and this fleet measures with $WK_PYOBJC_VERSION" "$restart" ;;
    esac

    case "$(_v screenlock)" in
        off)     _f ok "screen lock off" ;;
        on)      _f wrong "the screen lock is on, so this guest comes up asking for a password" \
                          "$rebuild  -- vm/desktop.sh leaves the lock alone unless the account's password is the one wk set, and will not guess at it" ;;
        *)       _f note "screen lock could not be read (sysadminctl needs passwordless sudo in there)" ;;
    esac

    # Every setting the shared table names, judged in the one place: a second reading of the same row here is how a guest and a bench install drift apart.
    wk_quiet_desktop_findings "$probe" "$restart"
    wk_quiet_cpu_findings "$probe" "$restart"

    v=$(_v setupassistant_pending)
    [ -z "$v" ] \
        && _f ok "Setup Assistant already clicked through" \
        || _f wrong "Setup Assistant will put a modal pane on the desktop:$v" "$rebuild"

    # softwareupdated obeys /Library/Preferences; the per-user domain is what System Settings shows a person. Neither can turn the *check* off on Tahoe (vm/desktop.sh says what was measured), so what is judged here is the two that reboot a guest under a build. `?` is "no such key", which is not off.
    case "$(_v update_autoinstall_system)" in
        0)  _f ok "macOS updates will not install themselves" ;;
        "") _f note "the guest did not answer about Software Update, so whether it installs one under a build is unknown" \
                    "$restart  -- a start re-runs this probe" ;;
        *)  _f wrong "macOS updates are set to install themselves in there (AutomaticallyInstallMacOSUpdates=$(_v update_autoinstall_system)), which reboots the guest -- mid-build, if that is when one lands" "$rebuild" ;;
    esac

    case "$(_v update_download_system)" in
        0)  _f ok "no update downloads itself in there" ;;
        "") ;;   # the note above already says this guest answered nothing here
        *)  _f wrong "updates download themselves in there (AutomaticDownload=$(_v update_download_system)), which takes the host's disk and the guest's bandwidth mid-build" "$rebuild" ;;
    esac

    case "$(_v update_check):$(_v update_download)" in
        0:0) _f ok "Software Update offers off in the login account too" ;;
        *)   _f note "the account's own Software Update settings read check=$(_v update_check), download=$(_v update_download) -- what System Settings shows at that window, not what softwareupdated obeys" "$rebuild" ;;
    esac

    # Setup Assistant's "what is new in macOS" pane is a Software Update screen by another name: Buddy shows it whenever these keys do not already name the running system.
    v=$(_v os_product)
    if [ -z "$v" ] || [ "$v" = '?' ]; then
        _f note "the guest did not say which macOS it runs, so Setup Assistant's 'what is new in macOS' pane cannot be judged from here" \
                "$restart  -- a start re-runs this probe"
    elif [ "$(_v setupassistant_seen_product)" = "$v" ]; then
        _f ok "Setup Assistant has already seen macOS $v"
    else
        _f wrong "Setup Assistant will show its 'what is new in macOS' pane (it last saw $(_v setupassistant_seen_product), this guest runs $v)" "$rebuild"
    fi

    v=$(_v windows)
    if [ "$v" = '?' ] || [ -z "$v" ]; then
        _f note "the window server was not asked what is on that screen (no compiler in there to build the probe with)" \
                "$restart  -- a start builds and runs it again"
    else
        local uninvited; uninvited=$(wk_window_unexpected "$v")
        [ -z "$uninvited" ] \
            && _f ok "nothing on that screen but $(printf '%s' "$v" | tr ';' '\n' | grep -c ':0:') window(s) wk put there" \
            || _f wrong "on that screen right now, and nothing wk runs put it there: ${uninvited%;}" \
                        "it comes back on every boot and no setting a guest can write stops it (docs/defects): clear it on the base, once -- $rebuild"
    fi

    _f note "$(_v frontapp) has the focus" \
            "an unfocused window is a throttled window: a benchmark measured behind one measures the throttle"

    # A note, not a fault: SecurityAgent is up for a few seconds of every login, and this report is taken seconds after one. It is a fault only if it is still there when the guest is asked again, which is what the remedy asks for.
    [ "$(_v securityagent)" = down ] \
        && _f ok "no authentication sheet is up" \
        || _f note "SecurityAgent is up, which the frontmost-application reading above cannot see. Every login has one for a moment" \
                   "wk vm check <name>  -- still up means something in there is waiting for a password"

    _f note "the guest's own window logs in as $(_v user)" \
            "wk itself uses an ssh key; 'wk vm start' and 'wk vm enter' state that account's password"

    unset -f _f _v
    return 0
}

# `_ssh`, not t_exec: this must answer about a guest whose wk-tools copy is older than this file.
vm_desktop_probe() { # <name>
    local ip; ip=$(_ip "$1") || return 1
    [ -n "$ip" ] || return 1
    { wk_quiet_desktop_script
      cat "$WK_ROOT/bench/mac-window-probe.sh" "$WK_ROOT/bench/mac-pyobjc.sh" "$WK_ROOT/vm/desktop-probe.sh"
    } | _ssh "$ip" 'bash -s'
}


# A guest holds its whole memory allocation whether or not it is busy, and nothing wk runs in one outlives its ssh session, so what accumulates keeps its own process: an editor's remote server, an agent, or a detached build.
vm_load_probe() { # <name>
    local ip; ip=$(_ip "$1") || return 1
    [ -n "$ip" ] || return 1
    _ssh "$ip" 'bash -s' < "$WK_ROOT/vm/load-probe.sh"
}

vm_load_findings() { # <probe output>
    printf '%s\n' "$1" | python3 -c '
import re, sys

shells_warn, free_warn, swap_warn = (int(a) for a in sys.argv[1:4])

procs, vals = [], {}
for line in sys.stdin.read().splitlines():
    key, _, val = line.partition("=")
    if key == "proc":
        rss, _, comm = val.strip().partition(" ")
        if rss.isdigit():
            procs.append((int(rss), comm.strip()))
    elif key:
        vals[key] = val.strip()

def out(state, what, remedy=""):
    print("\t".join((state, what, remedy)))

def mb(kb):
    return int(kb / 1024)

FAMILIES = (
    ("shell", r"(^|/)(-?zsh|bash|sh|dash|tcsh|fish|login)$"),
    ("editor remote server", r"(zed-remote-server|\.zed_server/|\.vscode-server/)"),
    ("agent", r"(^|/)claude$|/claude/versions/"),
    ("ssh session", r"(^|/)sshd(-session)?$"),
)

groups = {}
for rss, comm in procs:
    for fam, pat in FAMILIES:
        if re.search(pat, comm):
            n, kb = groups.get(fam, (0, 0))
            groups[fam] = (n + 1, kb + rss)
            break

def named(fam):
    n, kb = groups.get(fam, (0, 0))
    return n, kb

shells, shell_kb = named("shell")
if shells > shells_warn:
    holders = ["%d %s process(es)" % (groups[f][0], f)
               for f in ("editor remote server", "agent", "ssh session")
               if f in groups]
    why = (" -- alongside " + ", ".join(holders)) if holders else ""
    out("wrong",
        "%d shells are resident in there, holding %d MB%s" % (shells, mb(shell_kb), why),
        "wk vm stop <name> && wk vm start <name> takes them all with it; closing the "
        "editor window does not, since its remote server outlives it")
else:
    out("ok", "%d shells resident (%d MB)" % (shells, mb(shell_kb)))

n, kb = named("editor remote server")
if n:
    out("note",
        "an editor remote server is running in there: %d process(es), %d MB" % (n, mb(kb)),
        "it outlives the editor window, and every terminal pane in it leaves a "
        "shell behind; a guest restart is what clears both")
n, kb = named("agent")
if n:
    out("note", "%d agent process(es) in there, %d MB" % (n, mb(kb)),
        "each `wk ai claude` session in a guest is one of these")

free = vals.get("mem_free_pct", "")
total = vals.get("mem_total_mb", "?")
if not free.isdigit():
    out("note", "memory pressure could not be read in there (memory_pressure said nothing)")
elif int(free) < free_warn:
    top = ", ".join("%s (%d MB)" % (c.rsplit("/", 1)[-1], mb(r))
                    for r, c in sorted(procs, reverse=True)[:3])
    out("wrong",
        "%s%% of the %s MB in that guest is free, and macOS calls that pressure: "
        "the biggest resident processes are %s" % (free, total, top),
        "wk vm stop <name> && wk vm start <name>; a build in there is otherwise "
        "paging, and every number it produces is about the paging")
else:
    out("ok", "%s%% of the %s MB in that guest is free" % (free, total))

# sysctl vm.swapusage, raw: "total = 2048.00M  used = 512.25M  free = ...".
m = re.search(r"used = ([0-9.]+)M", vals.get("swapusage", ""))
if m and float(m.group(1)) > swap_warn:
    out("note", "the guest is using %d MB of swap" % float(m.group(1)),
        "it holds a fixed allocation, so this is the guest paging inside itself: "
        "a build here is slower than its numbers say")
' "$WK_VM_SHELLS_WARN" "$WK_VM_MEM_FREE_WARN_PCT" "$WK_VM_SWAP_WARN_MB"
}
