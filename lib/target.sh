# Loading a target driver, and the defaults every driver inherits. Commands under cmd/ call only this contract, never podman, tart or ssh directly; everything below the required set is a default a driver overrides where it differs.
# Required: t_exec <name> <cmd..>, t_home <name> (the workspace user's home, seen from inside it: never this machine's), t_list ("<name><tab><state>" per line), t_info <name> (absent | creating | unreachable | the driver's word for one that exists).

t_src()        { echo "/src/WebKit"; }   # the WebKit checkout inside the target
t_arch()       { echo native; }      # only the container driver differs; see lib/arch.sh
t_os()         { echo linux; }       # the platform a build here runs on: linux | macos
t_tools()      { echo "/opt/wk-tools"; }   # where wk-tools is inside the target

t_mirror_dir() { echo ""; }          # <name>; empty means fetch from the upstreams

mirror_in_container() { printf '%s' "${WK_MIRROR:-$(wk_mirror)}"; }   # the machine's own path, bind-mounted read-only and named in the container's environment, so a `--shared` snapshot's alternates resolve on both sides
WK_VM_MIRROR_SHARE=mirror
guest_share_dir()  { printf '/Volumes/My Shared Files/%s' "$1"; }   # <share name>: where macOS automounts a tart share
mirror_in_guest()  { printf '%s/WebKit.git' "$(guest_share_dir "$WK_VM_MIRROR_SHARE")"; }
_ws_py() { PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" python3 -m wk.targets "$@"; }   # Target's answers (lib/wk/targets.py) for a loaded target
t_sync_tools()    { _ws_py sync-tools "$WK_TARGET" "$1"; }     # push wk-tools in; nothing when it is bind-mounted
t_needs_base()    { _ws_py needs-base "$WK_TARGET"; }          # 0 when `wk new` must resolve a base snapshot first
ws_state()        { _ws_py state "$WK_TARGET" "$1"; }
ws_creating_now() { _ws_py creating-now "$WK_TARGET" "$1"; }
wait_ready()      { _ws_py ready "$WK_TARGET" "$@"; }          # <name> [seconds]; foreground: killing it stops only the waiting

t_agent_sock() { return 1; }        # the ssh-agent socket crossing in (push_agent_load)

# What the store holds decides the remedy: nothing, one no workspace can use, or a usable one this workspace was made without.
agent_secret_store_remedy() { # <secret>
    local line
    wk_cred_present "$1" \
        || { printf "this machine's store holds no %s: 'wk key set %s' puts one there" "$1" "$1"; return 0; }
    line=$(wk_cred_check "$1" --stored)
    if [ "$(wk_cred_verdict "$line")" = bad ]; then
        printf "this machine's store holds a %s no workspace can use (%s): 'wk key set %s --replace' makes a new one" \
            "$1" "$(wk_cred_detail "$line" | sed -n 1p)" "$1"
    else
        printf "this machine's store holds a usable %s that this workspace was made without: 'wk rm' and 'wk new' remake it with one" "$1"
    fi
}

t_start() { info "'$WK_TARGET' has no notion of starting a single workspace -- nothing to bring up for '$1'"; }

t_stop() { die "the '$WK_TARGET' target has no notion of stopping a single workspace -- '$1' is left running"; }

_ssh_opts_base() { # never interactive, bounded connect; drivers add their own
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=${1:-10}"
}

# What every t_spawn runs, whichever driver detaches it: the job announces the pid this end signals -- the command's own, not this shell's, since what may be signalled is checked against the command line it must have (job_pid_adopt) -- and writes its own exit status beside it. That status is the only thing that can tell a job that finished from a job that was killed once the driver is gone -- a driver SIGTERMed mid-wait leaves the record with a pid the far side no longer has and no exit, which reads `died` for a build that succeeded (measured 2026-09-17, a toolchain stage that had installed its SDK). task_verdict reads it as the record's `exit_file`. The stale status goes before the pid is announced, so a reader that has seen the pid is never looking at the last run's.
t_spawn_script() { # <log> <pidf> <cmd...> -- the shell text the job runs under
    local log="$1" pidf="$2"; shift 2
    printf 'rm -f %s; %s > %s 2>&1 < /dev/null & _wk_job=$!; echo $_wk_job > %s; wait $_wk_job; echo $? > %s' \
        "$(sh_quote "$pidf.exit")" \
        "$(sh_quote "$@")" "$(sh_quote "$log")" \
        "$(sh_quote "$pidf")" "$(sh_quote "$pidf.exit")"
}

# The far side is reached over ssh: nohup outlives the session's SIGHUP and disown drops it from the job table (detach_remote, lib/detach.sh). No setsid: macOS ships none.
t_spawn() { # <name> <log> <pidf> <cmd...> -- detached from this process
    local name="$1" log="$2" pidf="$3"; shift 3
    t_exec "$name" bash -lc "nohup bash -c $(sh_quote "$(t_spawn_script "$log" "$pidf" "$@")") \
        > /dev/null 2>&1 < /dev/null & disown"
}

WK_READY_MARKER=".wk-ready"   # written as the last act of creating a workspace

t_cores()      { envelope_cores; }   # <name>; a vm target is sized from the guest
t_mem_mb()     { envelope_mem_mb; }  # t_mem_mb <name>

t_task_put()  { :; }   # <name> <task dir>; nothing to do where the record already sits in the store the building machine reports from -- a driver whose far side is another machine copies it there

t_has_wk()    { return 1; }         # is there a far side that can answer?

t_far_side()  { echo none; }        # answering | unreachable | stopped | no-wk | none (not a machine of its own)
t_answers()   { WK_FAR_WHY=""; return 0; }   # 0 when the machine behind this target answers; else 1, with why in WK_FAR_WHY. By exit status in the caller's shell, so the driver's one probe is memoised for every question after it
t_wk()        { return 1; }         # t_wk <args...>, its exit status is the answer
t_wk_tty()    { t_wk "$@"; }        # t_wk with a terminal, for far-side commands that prompt a human

t_load() { host_load; }             # <name>; whole cores, which build_jobs polite subtracts

ws_on_target() { # <target> <name>
    ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
      load_target "$1" >/dev/null 2>&1 || exit 1
      [ -d "$(wk_ws_dir "$2")" ] && exit 0
      if command -v t_info >/dev/null 2>&1; then
          case "$(t_info "$2" 2>/dev/null)" in
              absent|unreachable|"") ;;
              *) exit 0 ;;
          esac
      fi
      ws_creating_now "$2" )
}

_ws_ask() { # <target> <name> -- here | absent | silent<TAB>why, on fd 3; its own process (par_run), so the target is loaded and probed once here
    command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
    load_target "$1" >/dev/null 2>&1 || { printf 'absent\n' >&3; return 0; }
    if [ -d "$(wk_ws_dir "$2")" ]; then printf 'here\n' >&3; return 0; fi
    t_answers || { printf 'silent\t%s\n' "$WK_FAR_WHY" >&3; return 0; }
    if ws_on_target "$1" "$2"; then printf 'here\n' >&3; else printf 'absent\n' >&3; fi
}

ws_locate() { # <name> -- every target that answers for it, one per line
    local name="$1" t hits="" machines="" rec
    for t in $(target_here); do
        ws_on_target "$t" "$name" && hits="$hits $t"
    done
    if [ -n "$hits" ]; then printf '%s\n' $hits; return 0; fi

    machines=$(target_machines)
    [ -n "$machines" ] || return 0

    command -v par_run >/dev/null 2>&1 || . "$WK_ROOT/lib/par.sh"
    wk_atexit par_cleanup
    par_begin
    for t in $machines; do par_run "$t" _ws_ask "$t" "$name"; done
    par_wait
    for t in $machines; do
        rec=$(par_record "$t" | sed -n 1p)
        case "$rec" in
            here)   hits="$hits $t" ;;
            absent) ;;
            *)      warn "could not ask $t over ssh: ${rec#silent*	} -- what is there is not in this answer" ;;
        esac
    done
    par_end

    # shellcheck disable=SC2086 -- deliberate word splitting of the collected hits.
    [ -z "$hits" ] || printf '%s\n' $hits
    return 0
}

ws_target() { # <name>
    local name="$1"
    if [ -n "${WK_TARGET:-}" ]; then printf '%s' "$WK_TARGET"; return 0; fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the answering targets.
    set -- $(ws_locate "$name")
    case "$#" in
        # A container workspace whose VM is stopped looks like an unknown name.
        0) default_target; return 0 ;;
        1) printf '%s' "$1"; return 0 ;;
    esac
    die "workspace '$name' exists on targets: $* -- this cannot be
    resolved; remove one, or set WK_TARGET"
}

gc_creation_records() { # drop the creation records whose process and workspace are both gone
    local d n t found
    _ws_task_lib
    while IFS= read -r d; do
        [ -n "$d" ] || continue
        [ "$(task_field "$d" kind)" = new ] || continue
        task_alive "$d" && continue
        n=$(task_field "$d" name)
        found=""
        for t in $(target_all 2>/dev/null); do
            if ( load_target "$t" >/dev/null 2>&1; [ -d "$(wk_ws_dir "$n")" ] ); then
                found=1; break
            fi
        done
        [ -z "$found" ] || continue
        info "removing orphaned creation record for '$n' (no live creation, no workspace)"
        rm -rf "$d" "$(ws_create_log "$n")"
    done <<EOF
$(task_list)
EOF
}

wk_marker() { echo "${WK_MARKER:-$HOME/.wk-workspace}"; }

in_workspace() { [ -f "$(wk_marker)" ]; }

marker_field() { kv_field "$1" "$2"; }   # one key=value field from a marker file

wk_marker_field() { marker_field "$(wk_marker)" "$1"; }

wk_remote_marker()   { echo "${WK_REMOTE_MARKER:-$HOME/.wk-remote}"; }
in_remote_host()     { [ -f "$(wk_remote_marker)" ]; }
wk_remote_field()    { marker_field "$(wk_remote_marker)" "$1"; }

wk_self() { wk_marker_field name; }   # the workspace this machine *is*, or empty on a host

default_target() { # `container` on a host, `local` inside one, on a target's far end the target this host is
    if in_workspace; then echo local; return 0; fi
    if in_remote_host; then wk_fleet self; return; fi
    echo container
}

default_config() { # <name> -- the last build's config, else the target platform's
    local name="$1" c
    c=$(last_built_config "$name")
    if [ -n "$c" ]; then
        info "config: $c -- what '$name' was last built with"
    else
        case "$( load_target "$(ws_target "$name")" >/dev/null 2>&1 && t_os )" in
            macos) c=mac-release ;;
            *)     c=jsc-release ;;
        esac
    fi
    printf '%s' "$c"
}

last_built_config() {
    local name="$1"
    ( command -v task_find >/dev/null 2>&1 || . "$WK_ROOT/lib/task.sh"
      load_target "$(ws_target "$name")" >/dev/null 2>&1
      task_field "$(task_find build "$name")" config 2>/dev/null ) || true
}

# Rewritten, not appended to: a VM's address changes on every boot.
wk_ssh_conf() { echo "$HOME/.ssh/config.d/wk"; }

ssh_alias_remove() {
    local conf tmp
    conf=$(wk_ssh_conf)
    [ -f "$conf" ] || return 0
    grep -q "^Host wk-$1\$" "$conf" || return 0
    tmp=$(mktemp)
    awk -v h="Host wk-$1" '
        $0 == h { skip = 1; next }
        /^Host / { skip = 0 }
        !skip { print }
    ' "$conf" > "$tmp"
    mv "$tmp" "$conf"
}

ssh_alias_set() { # <name> <hostname> <user> [identity] [extra-line...]
    local name="$1" hostname="$2" user="$3" conf extra
    conf=$(wk_ssh_conf)
    # An empty argument to HostName or User is not an alias that fails to resolve: ssh refuses to read the whole file and every ssh on this machine stops working, wk's and everybody else's.
    [ -n "$hostname" ] && [ -n "$user" ] \
        || die "no address for '$name', so no ssh alias was written: an empty HostName
    makes ssh refuse to read $conf at all, and with it every other host in it"
    ensure_dir "$(dirname "$conf")" 0700
    ssh_alias_remove "$name"

    extra=""
    [ -n "${4:-}" ] && extra="    IdentityFile $4
    IdentitiesOnly yes"
    shift 4 2>/dev/null || shift $#
    while [ $# -gt 0 ]; do extra="$extra
    $1"; shift; done

    cat >> "$conf" <<EOF

Host wk-$name
    # generated by wk -- removed by wk rm
    HostName $hostname
    User $user
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
$extra
EOF
}

# Not a deploy key, and not ~/.ssh/id_ed25519, which may want a passphrase.
zed_key() { echo "$(wk_state_dir)/ssh/zed_ed25519"; }

zed_key_pub() { # WK_ZED_PUBKEY when preparing a workspace for the machine that asked
    [ -z "${WK_ZED_PUBKEY:-}" ] || { printf '%s\n' "$WK_ZED_PUBKEY"; return 0; }
    local k; k=$(zed_key)
    if [ ! -f "$k" ]; then
        ensure_dir "$(dirname "$k")" 0700
        ssh-keygen -q -t ed25519 -N '' -C "wk zed key ($(hostname -s 2>/dev/null || echo host))" \
            -f "$k" || return 1
        changed "generated this machine's zed key ($k)"
    fi
    cat "$k.pub"
}

target_all() { # container, vm, this machine's own target, and every build machine and peer in machines/
    local t seen=" container vm "
    echo container
    command -v wk_record_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
    if is_macos && { [ -n "${WK_VM_STORE:-}" ] || [ "$(wk_record_dir)" != "$WK_STORE" ]; }; then echo vm; fi   # tart is Apple's, so a guest exists only on a macOS host, and only where its store (targets/vm.sh) is not the container's: elsewhere -- a Linux workstation, the podman VM, a macOS host pointed at a local store -- wk_record_dir resolves to the container store, and every container workspace would answer a second time as a vm guest

    if in_remote_host; then t=$(wk_fleet self) || return 1; seen="$seen$t "; echo "$t"; fi

    # Skipped on the far end of a target: a delegated `wk ls` would pay an ssh
    # timeout per machine it has no route to.
    local me=""
    if ! in_remote_host && [ -z "${WK_IN_VM:-}" ]; then
        me=$(wk_host_name)
        for t in $(target_known); do
            case "$seen" in *" $t "*) continue ;; esac
            [ -n "$me" ] && [ "$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')" = "$me" ] && continue
            seen="$seen$t "
            echo "$t"
        done
    fi
}

target_is_here() { # <target>
    case "$(target_kind "$1" 2>/dev/null || echo unknown)" in
        remote) ( load_target "$1" >/dev/null 2>&1; [ -n "${WK_REMOTE_LOCAL:-}" ] ) ;;
        *) return 0 ;;
    esac
}

target_here() {
    local t
    for t in $(target_all); do target_is_here "$t" && printf '%s\n' "$t"; done
    return 0
}

target_machines() {
    local t
    for t in $(target_all); do target_is_here "$t" || printf '%s\n' "$t"; done
    return 0
}

for_each_machine() { # <fn> <args...> -- worst exit status wins
    local fn="$1"; shift
    local t rc worst=0
    for t in $(target_all); do
        case "$t" in container|vm|local) continue ;; esac
        rc=0; "$fn" "$t" "$@" || rc=$?
        [ "$rc" -gt "$worst" ] && worst=$rc
    done
    return "$worst"
}

peer_workstations() { for_each_machine _peer_workstation; return 0; }
_peer_workstation() {
    ( load_target "$1"; [ -n "${WK_REMOTE_PEER:-}" ] && t_has_wk ) 2>/dev/null && echo "$1"
    return 0
}

peer_cred_verdict() { # <peer> <name> -> that machine's verdict for it, every line
    local line
    line=$( ( load_target "$1"; t_wk key verdict "$2" ) 2>/dev/null | tr -d '\r') || line=""
    if [ -z "$line" ]; then
        printf 'unverified\t%s did not answer: unreachable, or an older wk-tools there without the verdict subverb (wk sync --tools %s)\n' "$1" "$1"
        return 1
    fi
    printf '%s\n' "$line"
}

far_side_reason() { # <target> <far side> -- why it is not answering, for a person; `unreachable` reads the WK_FAR_WHY of a t_answers in this shell
    case "$2" in
        unreachable) echo "unreachable over ssh${WK_FAR_WHY:+: $WK_FAR_WHY}" ;;
        stopped)     echo "the podman machine '${WK_MACHINE:-wk}' is stopped -- 'wk start' brings it up" ;;
        no-wk)       echo "no wk-tools there yet -- 'wk machine setup $1'" ;;
        *)           echo "not a machine of its own" ;;
    esac
}

machine_answers() { # <target> -- one probe: t_answers here, then the memoised word
    local side
    t_answers || side=unreachable
    [ -n "${side:-}" ] || side=$(t_far_side)
    [ "$side" != answering ] || return 0
    printf '%-22s %s\n' "$1" "$(far_side_reason "$1" "$side")"
    return 1
}

# podman errors on an unknown machine name, so "absent" is read off the failure.
_machine_state() {
    podman machine inspect "$1" --format '{{.State}}' 2>/dev/null || echo absent
}

# The one command line a podman-machine child runs, for the dispatcher's forward and the container driver's delegation alike. The VM is part of this machine, so its records name this host as itself. WK_STORE is not forwarded: it names the store of whichever machine reads it, and the VM's is its own -- `wk sysimage ls` with a WK_STORE here that does not exist is how a test proves the VM was asked at all (tests/test_slots.py).
vm_wk_cmd() { # <wk args...>
    printf 'WK_IN_VM=1 %sWK_ROW_LABEL=%s WK_HOST_SELF=1 %s%s/opt/wk-tools/wk %s' \
        "$(wk_forwarded_env)" \
        "$(sh_quote "${WK_ROW_LABEL:-$(wk_machine_name)}")" \
        "${WK_NO_DELEGATE:+WK_NO_DELEGATE=1 }" \
        "${WK_CONFIG:+WK_CONFIG=$(sh_quote "$WK_CONFIG") }" \
        "$(sh_quote "$@")"
}

# </dev/null: a caller in a `while read` loop else loses stdin to podman ssh.
_in_machine() {
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        podman machine ssh "${WK_MACHINE:-wk}" "$@" </dev/null
    else
        bash -c "$*" </dev/null
    fi
}

target_registry_conf() { wk_fleet path "$1"; }

target_known() { wk_fleet list --kind build --kind peer; }

_target_known_line() { # so a typo is answered by the names that do exist
    local names
    names=$(target_known | tr '\n' ' ')
    [ -n "$names" ] || return 0
    printf '\n    The machines here: %s' "${names% }"
}

target_kind() { # a non-built-in needs a conf; one naming no kind is `remote`
    case "$1" in
        container|vm|remote|local) echo "$1"; return 0 ;;
    esac
    local out WK_TARGET_KIND=""
    out=$(wk_fleet load "$1" --kind build --kind peer) || return 1
    eval "$out"
    echo "${WK_TARGET_KIND:-remote}"
}

# A second load in one process would inherit the first machine's host, root and
# capacity. The environment still wins over a conf.
_target_reset_vars() {
    if [ -z "${_WK_ENV_SEEDED:-}" ]; then
        _WK_ENV_REMOTE_HOST="${WK_REMOTE_HOST:-}"
        _WK_ENV_REMOTE_ROOT="${WK_REMOTE_ROOT:-}"
        _WK_ENV_REMOTE_REFERENCE="${WK_REMOTE_REFERENCE:-}"
        _WK_ENV_SEEDED=1
    fi
    WK_REMOTE_HOST="$_WK_ENV_REMOTE_HOST"
    WK_REMOTE_ROOT="$_WK_ENV_REMOTE_ROOT"
    WK_REMOTE_REFERENCE="$_WK_ENV_REMOTE_REFERENCE"
    WK_REMOTE_LOCAL=""
    WK_REMOTE_PEER=""
    WK_MAX_JOBS=""
    WK_TARGET_CMAKE=""
    WK_TARGET_LIBCXX=""
    unset _WK_REMOTE_PROBED _WK_REMOTE_HOME _WK_REMOTE_CORES \
          _WK_REMOTE_LOAD _WK_REMOTE_MEM _WK_REMOTE_IONICE _WK_REMOTE_OS \
          _WK_REMOTE_REF_PROBED _WK_REMOTE_DOWN _WK_REMOTE_WHY \
          _WK_PEER_LISTED _WK_PEER_ROWS \
          _WK_PEER_ROUTE_NAME _WK_PEER_ROUTE_USER _WK_PEER_ROUTE_SRC _WK_PEER_ROUTE_PROXY
}

# The creation's log outlives the workspace directory a re-run destroys first.
ws_create_log()   { echo "$WK_STORE/log/new-$1.log"; }

_ws_task_lib() { command -v task_find >/dev/null 2>&1 || . "$WK_ROOT/lib/task.sh"; }

walk_targets() {
    if [ -n "${WK_TARGET:-}" ]; then printf '%s\n' $WK_TARGET; return 0; fi
    if in_workspace; then default_target; return 0; fi

    # Without it a workstation asked by another asks every machine it knows too.
    if [ -n "${WK_NO_DELEGATE:-}" ]; then target_here; return 0; fi

    target_all
}

target_workspaces() { # both sides: what the store created, what the driver has
    { list_workspaces 2>/dev/null || true
      t_list 2>/dev/null | cut -f1 || true
    } | grep -v '^[[:space:]]*$' | sort -u
}

load_target() {
    local t="${1:-container}" kind

    : "${WK_STORE_DEFAULT:=${WK_STORE:-/var/lib/wk}}"
    WK_STORE="$WK_STORE_DEFAULT"

    kind=$(target_kind "$t") || die "unknown target '$t'.
    The built-in ones are container, vm, remote and local.$(_target_known_line)

    Anything else is a machine, and needs a conf -- in the registry, so every
    device gets it:

        $(target_registry_conf "$t")
            KIND=build
            WK_REMOTE_HOST=$t      # an ssh destination that already works
            WK_REMOTE_ROOT=/home/you/wk

    'wk machine setup $t --kind build' writes it for you."

    WK_TARGET="$t"
    WK_TARGET_KIND="$kind"
    export WK_TARGET WK_TARGET_KIND

    _target_reset_vars

    # Re-sourced so every default is restored before the driver overrides it.
    # shellcheck disable=SC1090
    . "$WK_ROOT/lib/target.sh"

    [ "$kind" = "$t" ] || eval "$(wk_fleet load "$t")"
    # shellcheck disable=SC1090
    . "$WK_ROOT/targets/$kind.sh"
}
