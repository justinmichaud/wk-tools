# Loading a target driver, and the defaults every driver inherits. Commands
# under cmd/ call only this contract, never podman, tart or ssh directly.
# Required: t_create <name> [base], t_exec <name> <cmd..>, t_enter <name>,
# t_destroy <name>, t_list ("<name><tab><state>" per line), t_info <name>
# (absent | creating | unreachable | the driver's word for one that exists).
# Everything below is a default a driver overrides only where it differs.

t_src()        { echo "/src/WebKit"; }   # the WebKit checkout inside the target
t_arch()       { echo native; }      # only the container driver differs; see lib/arch.sh
t_os()         { echo linux; }       # the platform a build here runs on: linux | macos
t_tools()      { echo "/opt/wk-tools"; }   # where wk-tools is inside the target
t_ccache_dir() { echo "/ccache"; }   # inert on the Apple ports (no ccache)

t_mirror_dir() { echo ""; }          # <name>; empty means fetch from the upstreams

mirror_in_container()    { echo "/mirror/WebKit.git"; }   # the host's, bind-mounted read-only
mirror_beside_checkout() { echo "$1.git"; }               # <checkout>: a guest's own
t_sync_tools() { :; }               # push wk-tools in; nothing when it is bind-mounted

t_sync()       { :; }               # refresh this target's furniture: its tooling copy, and its store
t_prefetch()   { :; }                # ask this target whatever a report will need

# Three lines, any may be empty: remote name, its url there, ssh config to use.
t_wiring_args() { printf '\n\n\n'; }
t_ssh_host()   { echo "wk-$1"; }    # ssh destination, for Zed and the generated alias

t_ssh_prepare() { :; }   # point an editor at this target over ssh; nothing for one already an ssh destination

t_ssh_user()   { return 1; }        # the account inside the workspace an editor logs into
t_ssh_proxy()  { return 1; }        # what to run here to reach an addressless workspace

t_agent_sock() { return 1; }        # the ssh-agent socket crossing in (push_agent_load)
t_egress_filtered() { return 1; }   # <name>; 0 when everything this workspace reaches goes through wk's allowlisting proxy

t_agent_secret_present() { wk_agent_secret_present "$2"; }   # <name> <secret>
t_agent_secret_remedy() { agent_secret_store_remedy "$2"; } # <name> <secret>

agent_secret_store_remedy() { # <secret>
    printf "this machine's store holds no %s: 'wk key set %s' puts one there" "$1" "$1"
}

t_needs_base() { return 0; }        # 0 when `wk new` must resolve a base snapshot first

t_start() { info "'$WK_TARGET' has no notion of starting a single workspace -- nothing to bring up for '$1'"; }

t_stop() { die "the '$WK_TARGET' target has no notion of stopping a single workspace -- '$1' is left running"; }
t_store_init() { store_init; }      # create the host-side directories this target needs

t_home()       { echo "$HOME"; }   # the workspace user's home dir, as seen from inside it

# `t_exec <ws> cat <file>` into a redirect corrupts binary data both ways.
t_pull() {
    local name="$1" src="$2" dest="$3"
    cp -f "$src" "$dest"
}

_t_pull_dir_excludes() {
    _T_PULL_EXCLUDES=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --exclude) _T_PULL_EXCLUDES+=("--exclude" "${2:-}"); shift 2 ;;
            *) die "t_pull_dir: unknown option $1" ;;
        esac
    done
}

t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    _t_pull_dir_excludes "$@"
    mkdir -p "$dest"
    rsync -a --delete ${_T_PULL_EXCLUDES[@]+"${_T_PULL_EXCLUDES[@]}"} "$src/" "$dest/"
}

t_push() {
    local name="$1" src="$2" dest="$3"
    cp -f "$src" "$dest"
}

t_push_dir() { # contents replaced, not merged: the destination becomes a copy
    local name="$1" src="$2" dest="$3"
    mkdir -p "$dest"
    rsync -a --delete "$src/" "$dest/"
}

t_path_kind() { # dir | file | absent, so a copy can refuse before moving bytes
    local name="$1" p="$2"
    if [ -d "$p" ]; then echo dir; elif [ -e "$p" ]; then echo file; else echo absent; fi
}

_ssh_opts_base() { # never interactive, bounded connect; drivers add their own
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=${1:-10}"
}

# `nohup cmd &` survives an ssh close but not `podman exec` -- hence setsid.
t_spawn() { # <name> <log> <pidf> <cmd...> -- detached from this process
    local name="$1" log="$2" pidf="$3"; shift 3
    t_exec "$name" bash -lc "setsid nohup $(sh_quote "$@") \
        > $(sh_quote "$log") 2>&1 < /dev/null & echo \$! > $(sh_quote "$pidf")"
}

t_branch() { # `-` when unknowable without starting something
    local out
    case "$(t_info "$1")" in
        absent|creating|broken|unreachable) echo -; return 0 ;;
    esac
    out=$(t_exec "$1" git -C "$(t_src "$1")" rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '\r') || out=""
    printf '%s' "${out:--}"
}

WK_READY_MARKER=".wk-ready"   # written as the last act of creating a workspace

t_created() { return 0; }     # is the marker there? yes for a target that keeps none

t_ready() {
    local name="$1" i=0 max="${WK_READY_TIMEOUT:-300}"
    while [ "$i" -lt "$max" ]; do
        case "$(t_info "$name")" in
            creating) ;;
            absent|unreachable) return 1 ;;   # neither improves with waiting
            *) return 0 ;;
        esac
        sleep 1; i=$((i + 1))
    done
    return 1
}
t_cores()      { envelope_cores; }   # <name>; a vm target is sized from the guest
t_mem_mb()     { envelope_mem_mb; }  # t_mem_mb <name>

t_exec_tty()   { t_exec "$@"; }     # interactive exec: a full-screen UI needs a pty, ssh doesn't allocate one unasked
t_lldb_opts()  { :; }               # lldb options a target needs before it can launch anything

t_exec_build() { t_exec "$@"; }   # separate: the build lock must not block `wk run`

t_status_put() { local n="$1" ws; ws="$(wk_ws_dir "$n")"; cat > "$ws/build.status"; }
t_has_wk()    { return 1; }         # is there a far side that can answer?

t_delegates() { return 1; }         # must a command about a workspace here run there?
t_far_side()  { echo none; }        # answering | unreachable | no-wk | none (not a machine of its own)
t_wk()        { return 1; }         # t_wk <args...>, its exit status is the answer
t_wk_tty()    { t_wk "$@"; }        # t_wk with a terminal, for far-side commands that prompt a human

t_load() { awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0; }   # whole cores; build_jobs polite subtracts it

ws_on_target() { # <target> <name>
    ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
      command -v detach_alive >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
      load_target "$1" >/dev/null 2>&1 || exit 1
      [ -d "$(wk_ws_dir "$2")" ] && exit 0
      if command -v t_info >/dev/null 2>&1; then
          case "$(t_info "$2" 2>/dev/null)" in
              absent|unreachable|"") ;;
              *) exit 0 ;;
          esac
      fi
      sf=$(ws_status_file "$2")
      [ -f "$sf" ] && detach_alive "$sf" )
}

machine_silent() { # <target> -- 0 when the machine itself never answered
    ( load_target "$1" >/dev/null 2>&1; [ "$(t_far_side)" = unreachable ] )
}

_ws_ask() { # <target> <name> -- here | absent | silent, on fd 3
    if ws_on_target "$1" "$2"; then printf 'here\n'   >&3
    elif machine_silent "$1"; then  printf 'silent\n' >&3
    else                            printf 'absent\n' >&3
    fi
}

ws_locate() { # <name> -- every target that answers for it, one per line
    local name="$1" t hits="" machines="" silent=""
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
        case "$(par_record "$t" | sed -n 1p)" in
            here)   hits="$hits $t" ;;
            absent) ;;
            *)      silent="$silent $t" ;;
        esac
    done
    par_end

    [ -z "$silent" ] || warn "could not ask$silent over ssh ($(wk_ssh_timeout)s) --
    off, or not on the tailnet; what is there is not in this answer"

    # shellcheck disable=SC2086 -- deliberate word splitting of the collected hits.
    [ -z "$hits" ] || printf '%s\n' $hits
    return 0
}

ws_exists_on() { # <target> <name> -- unlike ws_on_target, silence is not absence
    ws_on_target "$1" "$2" && return 0
    ( load_target "$1" >/dev/null 2>&1
      [ "$(t_info "$2" 2>/dev/null)" = unreachable ] )
}

ws_exists() { # <name>
    [ -z "${WK_TARGET:-}" ] || { ws_exists_on "$WK_TARGET" "$1"; return $?; }
    [ -n "$(ws_locate "$1")" ]
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

gc_creation_records() { # drop create/<name>.status whose process and workspace are gone
    local sf n t found
    [ -d "$(ws_state_dir)" ] || return 0
    for sf in "$(ws_state_dir)"/*.status; do
        [ -f "$sf" ] || continue
        n=$(basename "$sf" .status)
        detach_alive "$sf" && continue
        found=""
        for t in $(target_all 2>/dev/null); do
            if ( load_target "$t" >/dev/null 2>&1; [ -d "$(wk_ws_dir "$n")" ] ); then
                found=1; break
            fi
        done
        [ -z "$found" ] || continue
        info "removing orphaned creation record for '$n' (no live creation, no workspace)"
        rm -f "$sf" "$(ws_create_log "$n")"
    done
}

wk_marker() { echo "${WK_MARKER:-$HOME/.wk-workspace}"; }

in_workspace() { [ -f "$(wk_marker)" ]; }

marker_field() { kv_field "$1" "$2"; }   # one key=value field from a marker file

wk_marker_field() { marker_field "$(wk_marker)" "$1"; }

wk_remote_marker()   { echo "${WK_REMOTE_MARKER:-$HOME/.wk-remote}"; }
in_remote_host()     { [ -f "$(wk_remote_marker)" ]; }
wk_remote_field()    { marker_field "$(wk_remote_marker)" "$1"; }

wk_self() { wk_marker_field name; }   # the workspace this machine *is*, or empty on a host

default_target() { # `container` on a host, `local` inside one: nothing else is there
    if in_workspace; then echo local; return 0; fi
    local t; t=$(wk_remote_field target)
    if [ -n "$t" ]; then echo "$t"; return 0; fi
    echo container
}

default_config() {
    local c=""
    in_workspace && c=$(wk_marker_field config)
    printf '%s' "${c:-jsc-release}"
}

last_built_config() {
    local name="$1"
    ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
      load_target "$(ws_target "$name")" >/dev/null 2>&1
      kv_field "$(wk_ws_dir "$name")/build.status" config 2>/dev/null ) || true
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

target_all() { # container, vm, this machine's own target, and targets/hosts/*.conf
    local d f t seen=" container vm "
    echo container
    echo vm

    t=$(wk_remote_field target)
    if [ -n "$t" ]; then seen="$seen$t "; echo "$t"; fi

    # Skipped on the far end of a target: a delegated `wk ls` would pay an ssh
    # timeout per machine it has no route to.
    local me=""
    if ! in_remote_host && [ -z "${WK_IN_VM:-}" ]; then
        me=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')
        for d in "$(target_registry_dir)"; do
            [ -d "$d" ] || continue
            for f in "$d"/*.conf; do
                [ -f "$f" ] || continue
                t=$(basename "$f" .conf)
                case "$seen" in *" $t "*) continue ;; esac
                [ -n "$me" ] && [ "$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')" = "$me" ] && continue
                seen="$seen$t "
                echo "$t"
            done
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

machine_answers() {
    case "$(t_far_side)" in
        answering)   return 0 ;;
        unreachable) printf '%-22s %s\n' "$1" "unreachable over ssh ($(wk_ssh_timeout)s) -- off, or not on the tailnet" ;;
        no-wk)       printf '%-22s %s\n' "$1" "no wk-tools there yet -- 'wk remote setup $1'" ;;
        *)           printf '%-22s %s\n' "$1" "not a machine of its own" ;;
    esac
    return 1
}

# podman errors on an unknown machine name, so "absent" is read off the failure.
_machine_state() {
    podman machine inspect "$1" --format '{{.State}}' 2>/dev/null || echo absent
}

# </dev/null: a caller in a `while read` loop else loses stdin to podman ssh.
_in_machine() {
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        podman machine ssh "${WK_MACHINE:-wk}" "$@" </dev/null
    else
        bash -c "$*" </dev/null
    fi
}

target_registry_dir() { echo "${WK_TARGET_REGISTRY:-$WK_ROOT/targets/hosts}"; }
target_registry_conf(){ echo "$(target_registry_dir)/$1.conf"; }

target_known() {
    local d f
    d=$(target_registry_dir)
    [ -d "$d" ] || return 0
    for f in "$d"/*.conf; do
        [ -f "$f" ] || continue
        basename "$f" .conf
    done
}

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
    local c k=""
    c=$(target_registry_conf "$1")
    [ -f "$c" ] || return 1
    k=$(awk -F= '/^[[:space:]]*WK_TARGET_KIND[[:space:]]*=/ {
            gsub(/[ \t"'"'"']/, "", $2); print $2; exit }' "$c")
    echo "${k:-remote}"
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
    WK_MAX_JOBS=""
    WK_TARGET_CMAKE=""
    WK_TARGET_LIBCXX=""
    unset _WK_REMOTE_PROBED _WK_REMOTE_HOME _WK_REMOTE_CORES \
          _WK_REMOTE_LOAD _WK_REMOTE_MEM _WK_REMOTE_IONICE _WK_REMOTE_OS \
          _WK_REMOTE_REF_PROBED _WK_REMOTE_DOWN \
          _WK_PEER_LISTED _WK_PEER_ROWS \
          _WK_PEER_ROUTE_NAME _WK_PEER_ROUTE_USER _WK_PEER_ROUTE_SRC _WK_PEER_ROUTE_PROXY
}

# absent: nothing of it exists. creating: something does, and creation never
# finished. present: both. broken: creation finished and the environment is
# gone (rule 5). unreachable: its machine did not answer.
ws_state() {
    local name="$1" env ws
    env=$(t_info "$name" 2>/dev/null || echo absent)
    ws=$(wk_ws_dir "$name")

    case "$env" in
        creating|unreachable) echo "$env"; return 0 ;;
    esac

    if [ "$env" = absent ]; then
        [ -d "$ws" ] || { echo absent; return 0; }
        command -v status_field >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
        if t_created "$name" 2>/dev/null \
           || [ "$(status_field "$(ws_status_file "$name")" state)" = present ]; then
            echo broken
        else
            echo creating
        fi
        return 0
    fi

    if t_needs_base && [ ! -f "$ws/base-id" ]; then echo creating; return 0; fi
    echo present
}

ws_display_state() {   # the driver's own word, or the lifecycle state when not `present`
    local st; st=$(ws_state "$1")
    case "$st" in
        present) t_info "$1" ;;
        *)       echo "$st" ;;
    esac
}

# Beside the workspace directory, which a re-run of `wk new` destroys first.
ws_state_dir()   { echo "$WK_STORE/create"; }
ws_status_file() { echo "$(ws_state_dir)/$1.status"; }
ws_create_log()  { echo "$(ws_state_dir)/$1.log"; }

ws_remake_hint() { # `wk new` is a workstation command; a build box refuses it
    if in_remote_host; then
        printf 'from the workstation:  wk new %s --target %s' "$1" "$(wk_remote_field target)"
    else
        printf 'wk new %s%s' "$1" "${WK_TARGET:+ --target $WK_TARGET}"
    fi
}

# Foreground by design: killing this waiter stops only the waiting.
wait_ready() {
    local name="$1" timeout="${2:-${WK_READY_WAIT:-1800}}"
    local st sf waited=0 said="" stage

    command -v detach_alive >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    sf=$(ws_status_file "$name")

    while :; do
        st=$(ws_state "$name")
        case "$st" in
        present)
            [ -z "$said" ] || info "'$name' is ready"
            return 0
            ;;
        absent)
            die "no such workspace: $name"
            ;;
        broken)
            die "'$name' exists as a record and not as a $WK_TARGET workspace: creation
    finished, and the environment is gone -- something outside wk removed it.
    Repair:  wk rm $name    (then 'wk new $name' if you still want it)"
            ;;
        unreachable)
            die "'$name' lives on a machine that did not answer ($(wk_ssh_timeout)s).
    Nothing is wrong with the workspace as far as this end can tell -- it
    cannot be reached to ask. Try again, or check the route:
        ssh -o BatchMode=yes ${WK_REMOTE_HOST:-the machine} true"
            ;;
        creating)
            if ! detach_alive "$sf"; then
                barrier "'$name' was never finished creating, and nothing is creating it now
    (the process that was is gone, with whatever connection started it).
    Usually there is nothing in one worth keeping, so remake it:
        $(ws_remake_hint "$name")
    --force uses it as it is, which is right when you can see that the
    checkout is complete and only the marker is missing."
                return 0
            fi
            if [ -z "$said" ]; then
                said=1
                stage=$(status_field "$sf" stage)
                info "waiting for '$name' to finish being created${stage:+ (at: $stage)}"
                log  "  follow it:  tail -f $(ws_create_log "$name")"
                log  "  this end can be killed; creation is detached and continues"
            fi
            ;;
        esac

        if [ "$waited" -ge "$timeout" ]; then
            die "'$name' was still $st after ${timeout}s.
    Creation is detached, so it may still be going: 'wk status $name' says
    whether the driver is alive, and $(ws_create_log "$name") says what it is doing."
        fi
        sleep 2; waited=$((waited + 2))
    done
}

# A lock says nothing about work detached into the workspace, so such a job
# writes `$(t_home)/<job>.pid` instead.
ws_busy_reason() {
    local name="$1" ws p pid job
    ws=$(wk_ws_dir "$name")
    [ -d "$ws/home" ] || return 1
    for p in "$ws"/home/*.pid; do
        [ -f "$p" ] || continue
        pid=$(tr -dc '0-9' < "$p" 2>/dev/null) || true
        [ -n "$pid" ] || continue
        job=$(basename "$p" .pid)
        if t_exec "$name" kill -0 "$pid" >/dev/null 2>&1; then
            printf '%s (pid %s in the workspace)' "$job" "$pid"
            return 0
        fi
    done
    return 1
}

prefetch_targets() {
    local t
    [ $# -gt 0 ] || return 0
    command -v mktemp >/dev/null 2>&1 || return 0
    WK_PREFETCH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/wk-probe.XXXXXX" 2>/dev/null) || return 0
    export WK_PREFETCH_DIR
    for t in "$@"; do
        ( load_target "$t" >/dev/null 2>&1 && t_prefetch ) >/dev/null 2>&1 &
    done
    wait
}

prefetch_done() {
    [ -n "${WK_PREFETCH_DIR:-}" ] || return 0
    rm -rf "$WK_PREFETCH_DIR"
    unset WK_PREFETCH_DIR
}

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
    local t="${1:-container}" kind conf

    : "${WK_STORE_DEFAULT:=${WK_STORE:-/var/lib/wk}}"
    WK_STORE="$WK_STORE_DEFAULT"

    kind=$(target_kind "$t") || die "unknown target '$t'.
    The built-in ones are container, vm, remote and local.$(_target_known_line)

    Anything else is a machine, and needs a conf -- in the registry, so every
    device gets it:

        $(target_registry_conf "$t")
            WK_REMOTE_HOST=$t      # an ssh destination that already works
            WK_REMOTE_ROOT=/home/you/wk

    'wk remote setup $t' writes it for you."

    WK_TARGET="$t"
    WK_TARGET_KIND="$kind"
    export WK_TARGET WK_TARGET_KIND

    _target_reset_vars

    # Re-sourced so every default is restored before the driver overrides it.
    # shellcheck disable=SC1090
    . "$WK_ROOT/lib/target.sh"

    conf=$(target_registry_conf "$t")
    if [ -f "$conf" ]; then
        # shellcheck disable=SC1090
        . "$conf"
    fi
    # shellcheck disable=SC1090
    . "$WK_ROOT/targets/$kind.sh"
}

ws_image_base() { # <ws> -- CFG_RELEASE from image/configs/<profile>.conf
    local profile
    case "$1" in
        yocto-*)     profile="${1#yocto-}" ;;
        buildroot-*) profile="${1#buildroot-}" ;;
        *) return 1 ;;
    esac
    awk -F= '/^CFG_RELEASE=/ { print $2; found=1 } END { exit !found }' \
        "$WK_ROOT/image/configs/$profile.conf" 2>/dev/null
}

# Run with $PWD inside the checkout, and shipped in by `declare -f`.
ws_upstream_line() { # `main`, a release like `2.52`, or `?` when unsure
    _u=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || _u=''
    _b=''
    if [ -n "$_u" ]; then
        _br=${_u#*/}
        case "$_br" in
            main) _b=main ;;
            webkitglib/*) _b=${_br#webkitglib/} ;;
        esac
    fi
    if [ -z "$_b" ]; then
        _rel=$(git for-each-ref --format='%(refname)' \
                --contains HEAD 'refs/remotes/*/webkitglib/*' 2>/dev/null \
                | sed 's#.*/webkitglib/##' | sort -t. -k1,1n -k2,2n | tail -1)
        if [ -n "$_rel" ]; then
            _b=$_rel
        elif git for-each-ref --format='%(refname)' \
                --contains HEAD 'refs/remotes/*/main' 2>/dev/null | grep -q .; then
            _b=main
        fi
    fi
    printf '%s' "${_b:-?}"
}
