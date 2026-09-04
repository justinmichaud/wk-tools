# Loading a target driver, and the defaults every driver inherits.

# Commands under cmd/ call only the contract below, never podman, tart or ssh
# directly, so adding a target means adding one file under targets/.

# Required of a driver:
#   t_create <name> [base]   bring the environment into existence
#   t_exec   <name> <cmd..>  run a command in it
#   t_enter  <name>          interactive shell
#   t_destroy <name>         remove it and everything it created
#   t_info   <name>          "absent", "creating", "unreachable", or the
#                            driver's word for an existing environment
#   t_list                   "<name>\t<state>" per line

# Optional, defaulted here. A driver overrides only what is genuinely
# different about it.
#   t_src <name>       where the WebKit checkout is *inside* the target
#   t_arch <name>      the architecture the target runs natively
#   t_os               `linux` or `macos`; it decides the build system a config
#                      uses, so load_target must run before config_load
#   t_tools <name>     where wk-tools is inside the target
#   t_sync_tools <n>   push wk-tools in (no-op when it is bind-mounted)
#   t_sync             refresh this target's own furniture
#   t_ssh_host <name>  ssh destination, for Zed and the generated alias
#   t_ssh_user <name>  the account inside the workspace an editor logs into
#   t_ssh_proxy <name> what to run *here* to speak ssh to a workspace with no
#                      address to connect to
#   t_needs_base       0 if `wk new` must resolve a base snapshot first
#   t_start/t_stop     bring a workspace's environment back / stop it
#   t_cores/t_mem_mb   the resources the target actually has
#   t_load             the load already on the target, for the polite sizing
#   t_ccache_dir       where ccache keeps its cache inside the target
#   t_mirror_dir <n>   the bare mirror a checkout inside this target fetches
#                      from; empty means it has none
#   t_store_init       create the host-side directories this target needs
#   t_created <name>   is creation's completion marker there? (.wk-ready)
#   t_ready            block until a new workspace finished initialising
#   t_exec_build       t_exec, for a build specifically (see below)
#   t_status_put       write the build status where the target's own side is
#   t_has_wk / t_wk    run `wk` on the target's own machine, if that is one
#   t_delegates        must a command about a workspace here run on that
#                      machine rather than this one?


# WK_READY_TIMEOUT/WK_READY_WAIT: how long to poll a workspace still being
# created. WK_MARKER/WK_REMOTE_MARKER: the marker paths saying "this machine IS
# a workspace" / "IS a remote target's host", overridable for a test.
# WK_TARGET_REGISTRY: the directory of machine confs this run reads.

t_src()        { echo "/src/WebKit"; }
t_arch()       { echo native; }      # only the container driver differs; see lib/arch.sh
t_os()         { echo linux; }       # the platform a build here runs on: linux | macos
t_tools()      { echo "/opt/wk-tools"; }
t_ccache_dir() { echo "/ccache"; }   # inert on the Apple ports (no ccache)

# A target with no mirror fetches from the upstreams instead (ws_fetch_script).
t_mirror_dir() { echo ""; }          # t_mirror_dir <name>

# The two mirrors a workspace can be looking at, spelled once each: a second
# spelling is a fetch that silently goes to the network.
mirror_in_container()    { echo "/mirror/WebKit.git"; }   # the host's, bind-mounted read-only
mirror_beside_checkout() { echo "$1.git"; }               # <checkout>: a guest's own
t_sync_tools() { :; }

t_sync()       { :; }               # bring this target's own furniture up to date: its
                                    # tooling copy, and its store when it keeps one
t_prefetch()   { :; }                # ask this target whatever a report will need

# Three lines, any of which may be empty: the local-copy remote's name, its url
# on the target, and an ssh config the checkout must use if not ~/.ssh/config.
t_wiring_args() { printf '\n\n\n'; }
t_ssh_host()   { echo "wk-$1"; }

t_ssh_prepare() { :; }   # point an editor at this target over ssh; nothing for one already an ssh destination

# Both refuse rather than guess: a target reached by address has no proxy to
# run, and one with no ssh account of its own has no editor route.
t_ssh_user()   { return 1; }
t_ssh_proxy()  { return 1; }

# The deploy keys live in an agent *outside* the workspace and only this socket
# crosses in (push_agent_load, lib/store.sh). Refuses rather than guessing.
t_agent_sock() { return 1; }

# Asked of the machine that will run the agent, not of the caller: this
# machine's store is the answer for every target it hands a credential to.
t_agent_secret_present() { wk_agent_secret_present "$2"; }   # <name> <secret>
t_agent_secret_remedy() { agent_secret_store_remedy "$2"; } # <name> <secret>

agent_secret_store_remedy() { # <secret>
    printf "this machine's store holds no %s: 'wk key set %s' puts one there" "$1" "$1"
}

t_needs_base() { return 0; }

t_start() { info "'$WK_TARGET' has no notion of starting a single workspace -- nothing to bring up for '$1'"; }

t_stop() { die "the '$WK_TARGET' target has no notion of stopping a single workspace -- '$1' is left running"; }
t_store_init() { store_init; }

t_home()       { echo "$HOME"; }   # the workspace user's home dir, as seen from inside it

# `t_exec <ws> cat <file>` into a redirect silently corrupts binary data, so
# anything over a network overrides this.
t_pull() {
    local name="$1" src="$2" dest="$3"
    cp -f "$src" "$dest"
}

# A separate hook rather than a loop over t_pull: a per-file round trip over ssh
# is minutes of latency. *Contents* are replaced, not merged.
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

# `t_exec <ws> "cat > <file>"` corrupts binary data on the way in as surely as
# `cat <file>` does on the way out.
t_push() {
    local name="$1" src="$2" dest="$3"
    cp -f "$src" "$dest"
}

# *Contents* are replaced, not merged: the destination becomes a copy.
t_push_dir() {
    local name="$1" src="$2" dest="$3"
    mkdir -p "$dest"
    rsync -a --delete "$src/" "$dest/"
}

# `dir`, `file` or `absent` -- what lets a copy refuse before it moves any
# bytes, in one word each driver can answer over what it already has.
t_path_kind() {
    local name="$1" p="$2"
    if [ -d "$p" ]; then echo dir; elif [ -e "$p" ]; then echo file; else echo absent; fi
}

# Never interactive, bounded connect; the rest of each driver's options differ.
_ssh_opts_base() {
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=${1:-10}"
}

# t_spawn -- detached from *this* process; the paths are inside the target. Over
# ssh `nohup cmd &` survives the connection closing, under `podman exec` it does
# not. A process nobody forked cannot be waited for.
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    t_exec "$name" bash -lc "setsid nohup $(sh_quote "$@") \
        > $(sh_quote "$log") 2>&1 < /dev/null & echo \$! > $(sh_quote "$pidf")"
}

# `-` when unknowable without starting something. targets/container.sh
# overrides, an overlay's HEAD being a file on the host.
t_branch() {
    local out
    # Anything not present and answering is `-`: exec'ing into a container
    # still provisioning would die mid-listing.
    case "$(t_info "$1")" in
        absent|creating|broken|unreachable) echo -; return 0 ;;
    esac
    out=$(t_exec "$1" git -C "$(t_src "$1")" rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '\r') || out=""
    printf '%s' "${out:--}"
}

# --- creation's completion marker -------------------------------------------
# `.wk-ready`, written as the *last* act of creating a workspace, next to the
# artifact. vm is the deviation: a freshly cloned guest is visible only here.
WK_READY_MARKER=".wk-ready"

# The case t_info alone cannot see: a marker present with the environment
# *not*. Defaults to yes, so a target with no marker reads as finished.
t_created() { return 0; }

t_ready() {
    local name="$1" i=0 max="${WK_READY_TIMEOUT:-300}"
    while [ "$i" -lt "$max" ]; do
        case "$(t_info "$name")" in
            creating) ;;
            # Neither gets better by waiting five minutes.
            absent|unreachable) return 1 ;;
            *) return 0 ;;
        esac
        sleep 1; i=$((i + 1))
    done
    return 1
}
# Different from lib/resources.sh: a vm target is sized from the *guest*.
t_cores()      { envelope_cores; }   # t_cores <name>
t_mem_mb()     { envelope_mem_mb; }  # t_mem_mb <name>

t_exec_tty()   { t_exec "$@"; }     # interactive exec: a full-screen UI needs a pty, ssh doesn't allocate one unasked
t_lldb_opts()  { :; }               # lldb options a target needs before it can launch anything

# Applied to t_exec, the build lock would also make `wk run`/`wk status` block.
t_exec_build() { t_exec "$@"; }

# Unless the target is a *machine of its own*, which writes there instead: a
# second copy on the driving side would go stale.
t_status_put() { local n="$1" ws; ws="$(wk_ws_dir "$n")"; cat > "$ws/build.status"; }
t_has_wk()    { return 1; }         # is there a far side that can answer?

# A target whose workspaces belong to another machine says no, and `wk` hands
# the whole command over rather than each command deciding for itself.
t_delegates() { return 1; }
t_far_side()  { echo none; }        # answering | unreachable | no-wk | none (not a machine of its own)
t_wk()        { return 1; }         # t_wk <args...>, its exit status is the answer
t_wk_tty()    { t_wk "$@"; }        # t_wk with a terminal, for far-side commands that prompt a human

t_load() { awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0; }   # whole cores; build_jobs polite subtracts it

# Which target a *named* workspace lives on, so `wk build`, `wk pr` and the
# macOS dispatcher cannot reach three machines for one name. ws_on_target's
# three tests each see what the others cannot: the workspace directory (only
# where the store is here), t_info ("unreachable" is not evidence of existence),
# and the status file `wk new` writes while creating.
ws_on_target() {
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

# 0 when the machine itself never answered. The machine, not the name: asking
# about a name again costs a second round trip. Reads t_far_side.
machine_silent() { # <target>
    ( load_target "$1" >/dev/null 2>&1; [ "$(t_far_side)" = unreachable ] )
}

# One machine's answer, on fd 3: `here`, `absent`, or `silent`. Silence is a
# third thing -- a machine that is off must not make its workspaces read deleted.
_ws_ask() {
    if ws_on_target "$1" "$2"; then printf 'here\n'   >&3
    elif machine_silent "$1"; then  printf 'silent\n' >&3
    else                            printf 'absent\n' >&3
    fi
}

# Every target that answers for <name>, one per line. Two stages: this machine's
# own environments answer first and their answer is final; `--target` names the
# other. The machines are then asked all at once (lib/par.sh).
ws_locate() { # <name>
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

# `ws_on_target` says no both when the target answered and has not got it and
# when it did not answer, and only the first is evidence of absence.
ws_exists_on() { # <target> <name>
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
        # On a macOS host a container workspace whose VM is stopped looks the
        # same as an unknown name, so default_target's "container" answer
        # forwards the command into the VM to resolve.
        0) default_target; return 0 ;;
        1) printf '%s' "$1"; return 0 ;;
    esac
    die "workspace '$name' exists on targets: $* -- this cannot be
    resolved; remove one, or set WK_TARGET"
}

# `wk new` writes $WK_STORE/create/<name>.{status,log} as its first act, so a
# later command can tell a half-made workspace from a missing one. Checked
# against the process table, never a file that outlives it.
gc_creation_records() {
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

# --- am I a workspace? -------------------------------------------------------
# A workspace is a whole machine; the `wk` inside one acts on it rather than
# reaching one from outside. A marker file says so and names the checkout -- a
# file, not a variable, since it must hold for an ssh command or a hook.
wk_marker() { echo "${WK_MARKER:-$HOME/.wk-workspace}"; }

in_workspace() { [ -f "$(wk_marker)" ]; }

marker_field() { kv_field "$1" "$2"; }   # one key=value field from a marker file

wk_marker_field() { marker_field "$(wk_marker)" "$1"; }

# --- am I a machine that *hosts* workspaces? ---------------------------------
# A shared build box holds several workspaces at once, so the marker above is
# the wrong shape. This one names the remote target this machine is the far end
# of:  target=devbox-arm64-2 / root=/home/you/wk.
wk_remote_marker()   { echo "${WK_REMOTE_MARKER:-$HOME/.wk-remote}"; }
in_remote_host()     { [ -f "$(wk_remote_marker)" ]; }
wk_remote_field()    { marker_field "$(wk_remote_marker)" "$1"; }

wk_self() { wk_marker_field name; }   # the workspace this machine *is*, or empty on a host

# `container` on a host, `local` inside one: there is nothing else in there.
default_target() {
    if in_workspace; then echo local; return 0; fi
    local t; t=$(wk_remote_field target)
    if [ -n "$t" ]; then echo "$t"; return 0; fi
    echo container
}

# jsc-release is meaningless in a macOS guest, which can build nothing but the
# Apple ports, so the marker carries the answer.
default_config() {
    local c=""
    in_workspace && c=$(wk_marker_field config)
    printf '%s' "${c:-jsc-release}"
}

# Evidence, not a preference: `wk test <ws> --layout` needs the config just
# built, not the jsc-release default (the wrong tree for layout tests).
last_built_config() {
    local name="$1"
    ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
      load_target "$(ws_target "$name")" >/dev/null 2>&1
      kv_field "$(wk_ws_dir "$name")/build.status" config 2>/dev/null ) || true
}

# --- generated ssh aliases ---------------------------------------------------
# Under ~/.ssh/config.d, so it can be rewritten without touching hand-maintained
# entries. Rewritten, not appended: a VM's address changes on every boot.

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

# ssh_alias_set <name> <hostname> <user> [identity] [extra-line...] -- the extra
# lines are for a target not reached by address at all.
ssh_alias_set() {
    local name="$1" hostname="$2" user="$3" conf extra
    conf=$(wk_ssh_conf)
    ensure_dir "$(dirname "$conf")" 0700
    ssh_alias_remove "$name"

    # A per-target identity only when there is one: a macOS guest has a key of
    # its own; a container authorises this machine's zed key.
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

# --- the zed key --------------------------------------------------------------
# One key per machine, for reaching a container workspace's sshd over `podman
# exec`. Not a deploy key, and not ~/.ssh/id_ed25519, which may be
# passphrase-protected: this machine authenticates to its own containers.
zed_key() { echo "$(wk_state_dir)/ssh/zed_ed25519"; }

# Not this machine's whenever this machine is preparing a workspace for the one
# that asked (WK_ZED_PUBKEY).
zed_key_pub() {
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

# container and vm unconditionally, the build box this machine is the far end
# of, and every machine configured under targets/hosts/*.conf.
target_all() {
    local d f t seen=" container vm "
    # A missing podman or tart answers with nothing, not a loud failure.
    echo container
    echo vm

    # On a shared build machine the workspaces were created elsewhere, so
    # without this a bare `wk status` there would report nothing about them.
    t=$(wk_remote_field target)
    if [ -n "$t" ]; then seen="$seen$t "; echo "$t"; fi

    # Skipped on a machine that is the far end of a target: a delegated `wk ls`
    # would otherwise pay an ssh timeout for every machine it has no route to.
    local me=""
    if ! in_remote_host && [ -z "${WK_IN_VM:-}" ]; then
        me=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')
        for d in "$(target_registry_dir)"; do
            [ -d "$d" ] || continue
            for f in "$d"/*.conf; do
                [ -f "$f" ] || continue
                t=$(basename "$f" .conf)
                case "$seen" in *" $t "*) continue ;; esac
                # ...and never itself: it would ssh to itself and report itself
                # unreachable, with no sshd inside.
                [ -n "$me" ] && [ "$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')" = "$me" ] && continue
                seen="$seen$t "
                echo "$t"
            done
        done
    fi
}

# target_all, split by what asking one costs: `target_here` is this machine's
# own environments (decided from the conf, so the split costs no ssh).
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

# Its exit status folds into the worst one seen.
for_each_machine() {
    local fn="$1"; shift
    local t rc worst=0
    for t in $(target_all); do
        case "$t" in container|vm|local) continue ;; esac
        rc=0; "$fn" "$t" "$@" || rc=$?
        [ "$rc" -gt "$worst" ] && worst=$rc
    done
    return "$worst"
}

# 0 when the target's own machine can run `wk` for it; otherwise prints the
# fan-out line saying why not. Down and unprovisioned have different remedies.
machine_answers() {
    case "$(t_far_side)" in
        answering)   return 0 ;;
        unreachable) printf '%-22s %s\n' "$1" "unreachable over ssh ($(wk_ssh_timeout)s) -- off, or not on the tailnet" ;;
        no-wk)       printf '%-22s %s\n' "$1" "no wk-tools there yet -- 'wk remote setup $1'" ;;
        *)           printf '%-22s %s\n' "$1" "not a machine of its own" ;;
    esac
    return 1
}

# --- the podman machine (macOS) -----------------------------------------------

# podman errors on an unknown machine name rather than reporting one, so
# "absent" has to be read back off the failure.
_machine_state() {
    podman machine inspect "$1" --format '{{.State}}' 2>/dev/null || echo absent
}

# Named apart from _vm() (targets/vm.sh's Tart guest) so sourcing both does not
# clobber either. </dev/null is essential: a caller feeding this from a `while
# read` loop otherwise has its stdin read by podman-machine-ssh.
_in_machine() {
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        podman machine ssh "${WK_MACHINE:-wk}" "$@" </dev/null
    else
        bash -c "$*" </dev/null
    fi
}

# --- named targets -----------------------------------------------------------
# A target name is one of the four built-in kinds -- container, vm, remote,
# local -- or a machine configured under targets/hosts/<name>.conf. `remote` is
# the one kind with several instances: the machine's own name is the target.
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

# For the refusal below: a mistyped name is answered by the name that exists,
# not by instructions for adding the machine the typo invented.
_target_known_line() {
    local names
    names=$(target_known | tr '\n' ' ')
    [ -n "$names" ] || return 0
    printf '\n    The machines here: %s' "${names% }"
}

# Anything not a built-in kind must have a conf; one naming no kind is `remote`.
target_kind() {
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

# Cleared before every load: `wk status` loads more than one target in a
# process, and the second remote machine would otherwise inherit the first's
# host, root and measured capacity. The environment still wins over a conf.
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
    # Per-machine CMake defaults come from the conf about to be sourced.
    WK_TARGET_CMAKE=""
    WK_TARGET_LIBCXX=""
    unset _WK_REMOTE_PROBED _WK_REMOTE_HOME _WK_REMOTE_CORES \
          _WK_REMOTE_LOAD _WK_REMOTE_MEM _WK_REMOTE_IONICE _WK_REMOTE_OS \
          _WK_REMOTE_REF_PROBED _WK_REMOTE_DOWN \
          _WK_PEER_LISTED _WK_PEER_ROWS \
          _WK_PEER_ROUTE_NAME _WK_PEER_ROUTE_USER _WK_PEER_ROUTE_SRC _WK_PEER_ROUTE_PROXY
}

# --- what state is this workspace in? ----------------------------------------
#   absent       nothing of it exists anywhere
#   creating     something of it exists, and creation never finished
#   present      it exists and creation finished
#   broken       creation finished and the environment is gone (rule 5)
#   unreachable  the machine that holds it did not answer in time
# Derived on every call from evidence next to the artifacts, stored nowhere.
# `ws.status` is a claim by the creating process, never believed.
ws_state() {
    local name="$1" env ws
    env=$(t_info "$name" 2>/dev/null || echo absent)
    ws=$(wk_ws_dir "$name")

    case "$env" in
        creating|unreachable) echo "$env"; return 0 ;;
    esac

    if [ "$env" = absent ]; then
        [ -d "$ws" ] || { echo absent; return 0; }
        # A ws directory with no environment behind it: with the marker,
        # something removed a finished environment by hand; without it, it is
        # rubble from an interrupted creation.
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

ws_display_state() {   # the driver's own answer, except a workspace whose lifecycle is not `present` reports that instead
    local st; st=$(ws_state "$1")
    case "$st" in
        present) t_info "$1" ;;
        *)       echo "$st" ;;
    esac
}

# A claim, never the record (see ws_state). Beside the workspace directory: a
# re-run of `wk new` *destroys* that directory first, and the log being written
# must not be the file it deletes.
ws_state_dir()   { echo "$WK_STORE/create"; }
ws_status_file() { echo "$(ws_state_dir)/$1.status"; }
ws_create_log()  { echo "$(ws_state_dir)/$1.log"; }

# `wk new` is a workstation command, so printing it bare on a build box -- which
# refuses it -- sends somebody straight to a refusal.
ws_remake_hint() {
    if in_remote_host; then
        printf 'from the workstation:  wk new %s --target %s' "$1" "$(wk_remote_field target)"
    else
        printf 'wk new %s%s' "$1" "${WK_TARGET:+ --target $WK_TARGET}"
    fi
}

# --- one gate: wait_ready ----------------------------------------------------
# The one place anything asks "may I act on this yet". Foreground by design: if
# this waiter is killed only the waiting stops, creation being detached.
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
                # A barrier, not a refusal: a clone that finished and never got
                # its marker looks exactly like one cut in the middle.
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

# --- work a lock here cannot see --------------------------------------------
# A lock (hold_lock, lib/common.sh) dies with the process holding it, which
# covers nothing for work *detached into the workspace*. The interface instead:
# **a job detached into a workspace writes `$(t_home)/<job>.pid`.**
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

# --- one round of probes instead of N ----------------------------------------
# A bare `wk status` walks every target serially, and an ssh machine costs a full
# connect (WK_SSH_TIMEOUT) when off. One subshell per target asks its own machine
# and leaves the answer where the serial pass finds it; best-effort.
# WK_PREFETCH_DIR is set by this process for its own subshells.
prefetch_targets() {
    local t
    [ $# -gt 0 ] || return 0
    command -v mktemp >/dev/null 2>&1 || return 0
    WK_PREFETCH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/wk-probe.XXXXXX" 2>/dev/null) || return 0
    export WK_PREFETCH_DIR
    for t in "$@"; do
        # A subshell, so each target's driver load and globals stay its own.
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

    # WK_NO_DELEGATE: without it, a workstation asked by another would ask every
    # machine *it* knows too, squaring the ssh work over the shared registry.
    if [ -n "${WK_NO_DELEGATE:-}" ]; then target_here; return 0; fi

    target_all
}

# From both sides: the local store knows what this machine created, the driver
# knows what actually exists over there.
target_workspaces() {
    { list_workspaces 2>/dev/null || true
      t_list 2>/dev/null | cut -f1 || true
    } | grep -v '^[[:space:]]*$' | sort -u
}

load_target() {
    local t="${1:-container}" kind conf

    # $WK_STORE is target-dependent; reset first so a second load in one
    # process does not inherit the first.
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

    # Set before either file is read: both are written in terms of the name.
    WK_TARGET="$t"
    WK_TARGET_KIND="$kind"
    export WK_TARGET WK_TARGET_KIND

    _target_reset_vars

    # And the *functions*: a definition outlives the load, so re-sourcing
    # restores every default before the driver overrides its own.
    # shellcheck disable=SC1090
    . "$WK_ROOT/lib/target.sh"

    # Conf first, driver second, so the driver's ${VAR:-default} assignments
    # fill in only what the conf left unsaid.
    conf=$(target_registry_conf "$t")
    if [ -f "$conf" ]; then
        # shellcheck disable=SC1090
        . "$conf"
    fi
    # shellcheck disable=SC1090
    . "$WK_ROOT/targets/$kind.sh"
}

# --- the upstream line a workspace tracks -------------------------------------
# For an image workspace, straight from the profile's own conf -- CFG_RELEASE
# (image/configs/<profile>.conf), a flat list of VAR=value lines.
ws_image_base() { # <ws>
    local profile
    case "$1" in
        yocto-*)     profile="${1#yocto-}" ;;
        buildroot-*) profile="${1#buildroot-}" ;;
        *) return 1 ;;
    esac
    awk -F= '/^CFG_RELEASE=/ { print $2; found=1 } END { exit !found }' \
        "$WK_ROOT/image/configs/$profile.conf" 2>/dev/null
}

# The upstream WebKit line a checkout's HEAD descends from -- `main`, or a
# release like `2.52` -- printed as `?` when neither answer is confident. Run
# with $PWD inside the checkout: HEAD's tracked upstream when it names main or a
# release branch (`wpe/webkitglib/2.52` -> `2.52`), else the nearest such ref
# containing HEAD. Shipped into a workspace with `declare -f`, so the probe
# there runs this body.
ws_upstream_line() {
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
