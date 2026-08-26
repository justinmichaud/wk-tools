# Loading a target driver, and the defaults every driver inherits.

# Commands under cmd/ never talk to podman, tart or ssh directly -- they call
# the contract below and nothing else, so "add a target" means "add one file
# under targets/" rather than "audit every command".

# Required of a driver:
#   t_create <name> [base]   bring the environment into existence
#   t_exec   <name> <cmd..>  run a command in it
#   t_enter  <name>          interactive shell
#   t_destroy <name>         remove it and everything it created
#   t_info   <name>          the lifecycle in one word: "absent", "creating",
#                            the driver's word for an existing environment
#                            ("running"/"exited"/"present"), or "unreachable"
#   t_list                   "<name>\t<state>" per line

# Optional, defaulted here. A driver overrides only what is genuinely
# different about it; re-stating a default is how the two copies drift apart.
#   t_src <name>       where the WebKit checkout is *inside* the target
#   t_arch <name>      the architecture the target runs natively
#   t_tools <name>     where wk-tools is inside the target
#   t_sync_tools <n>   push wk-tools in (no-op when it is bind-mounted)
#   t_sync             refresh the target's own far-side state, if it has any
#   t_ssh_host <name>  ssh destination, for Zed and the generated alias
#   t_needs_base       0 if `wk new` must resolve a base snapshot first
#   t_start <name>       bring a stopped workspace's environment back; says so
#                        rather than pretending for a target with nothing to
#                        start (a remote build machine is always reachable)
#   t_stop <name>        stop a workspace's environment, leaving it on disk;
#                        refuses and says why for a target with no such notion
#                        (a remote build machine, a workspace stopping itself)
#   t_cores/t_mem_mb   the resources the target actually has
#   t_load             the load already on the target, for the polite sizing
#   t_ccache_dir       where ccache keeps its cache inside the target
#   t_store_init       create the host-side directories this target needs
#   t_created <name>   is creation's completion marker there? (see .wk-ready)
#   t_ready            block until a new workspace finished initialising
#   t_exec_build       t_exec, for a build specifically (see below)
#   t_state_put        keep a copy of the build status where the target is
#   t_has_wk / t_wk    run `wk` on the target's own machine, if that is one

# targets/local.sh is the degenerate case: the target is the machine
# already running the command (see "am I a workspace?" below).
# t_cores/t_mem_mb differ from lib/resources.sh: a vm target must be sized
# from the *guest*'s memory, not the host's.

t_src()        { echo "/src/WebKit"; }

# `native` unless creation said otherwise; only the container driver can
# currently differ from the host. Not "what would I build for" -- that
# question has a second answer once cross builds exist. See lib/arch.sh.
t_arch()       { echo native; }
t_tools()      { echo "/opt/wk-tools"; }
# The bind-mounted store cache in a container; inert on the Apple ports
# (no ccache). Somebody else's machine answers with its own directory --
# see targets/remote.sh.
t_ccache_dir() { echo "/ccache"; }
t_sync_tools() { :; }

# `wk sync` against a target with far-side state to refresh; everything
# else keeps its base in this machine's store, where `wk sync` *is* the
# operation (cmd/sync). Returning 1 says "no far side".
t_sync()       { return 1; }

# Ask this target whatever a report will need, so every target can be asked
# at once. Nothing to do for a target that is this machine.
t_prefetch()   { :; }

# How this target's checkouts are wired, for whoever re-asserts it: three
# lines, any of which may be empty --
#   <name of the local-copy remote>   (mirror, shared)
#   <its url, on the target>
#   <an ssh config the checkout must use, if not ~/.ssh/config>
# A contract hook, not a table in the caller, since only the driver knows
# (a shared build box has ssh aliases and history a container has neither).
t_wiring_args() { printf '\n\n\n'; }
t_ssh_host()   { echo "wk-$1"; }

# Everything before an editor is pointed at this target over ssh -- an sshd,
# a key, an alias. Nothing for a target already an ssh destination; the
# container driver is the one with work to do here.
t_ssh_prepare() { :; }
t_needs_base() { return 0; }

# No notion of starting a single workspace by default: a machine this target
# only *reaches* (remote) is always reachable from here, so there is nothing
# to bring up. container.sh and vm.sh, which do have something to start,
# override this; targets/local.sh overrides with the more specific reason a
# workspace cannot start itself.
t_start() { info "'$WK_TARGET' has no notion of starting a single workspace -- nothing to bring up for '$1'"; }

# No notion of stopping a single workspace by default: a machine this target
# only *reaches* (remote) is not this command's to shut down, and a workspace
# is not its own local's to stop from inside itself (targets/local.sh
# overrides with the more specific reason). container.sh and vm.sh are the
# two drivers that do have one and override this. Refuses loudly rather than
# silently doing nothing, which would read as "stopped" about a workspace
# left running.
t_stop() { die "the '$WK_TARGET' target has no notion of stopping a single workspace -- '$1' is left running"; }
t_store_init() { store_init; }

# The workspace user's home directory, as seen from inside it -- needed to
# name one file from both sides (a detached build's log). In a container
# that's a host bind mount, making following it free; see t_spawn.
t_home()       { echo "$HOME"; }

# t_pull <name> <src-in-target> <dest-on-host> -- copy one file out, byte
# for byte. The obvious spelling, `t_exec <ws> cat <file>` into a redirect,
# silently corrupts binary data (measured: 1396 bytes came out as 1399).
#
# Right by default for `local`; anything over a network overrides it
# (`podman cp`, `scp`). `wk profile` copies a recording, exactly the binary
# the `cat` spelling corrupts.
t_pull() {
    local name="$1" src="$2" dest="$3"
    cp -f "$src" "$dest"
}

# t_pull_dir <name> <src-dir-in-target> <dest-dir-on-host> [--exclude PAT]...
# The same for a directory, a separate hook rather than a loop over t_pull
# since a per-file round trip over ssh is minutes of latency for nothing.
# *Contents* are replaced, not merged, so a re-stage can't leave last
# build's binaries behind. Excludes reduce a build tree to a *product* tree
# (Apple's WebKitBuild/<config> is ~39 GB of mostly intermediates); a
# driver that cannot honour them refuses rather than copying everything.
# _t_pull_dir_excludes -- the `--exclude PAT` parsing every override needs,
# in one place, since targets/remote.sh and targets/vm.sh were carrying an
# identical copy. Leaves _T_PULL_EXCLUDES, ready for `${_T_PULL_EXCLUDES[@]+...}`.
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

# The two ssh options every driver reaching a real machine wants: never
# interactive, bounded connect. The rest of each list genuinely differs
# (guest keepalives, ControlMaster muxing) and stays with the driver.
# _ssh_opts_base <connect-timeout>
_ssh_opts_base() {
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=${1:-10}"
}

# t_spawn <name> <log> <pidfile> <cmd...> -- start a command detached from
# *this* process and return at once; `log`/`pidfile` are paths inside the
# target. Over ssh, `nohup cmd &` survives the connection closing; under
# `podman exec` it does not (measured: a `setsid nohup` told to sleep 3 was
# gone before it wrote anything), so the container driver overrides this.
# A process nobody forked can't be waited for, so a caller decides
# "finished" from what it left behind -- why yocto's wrapper prints a marker.
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    t_exec "$name" bash -lc "setsid nohup $(sh_quote "$@") \
        > $(sh_quote "$log") 2>&1 < /dev/null & echo \$! > $(sh_quote "$pidf")"
}

# This workspace's checkout branch, or `-` when unknowable without
# starting something (with several workspaces open, guessing from the name
# builds the wrong tree). Asked of the target, never a record: the default
# execs into the driver; targets/container.sh overrides since an overlay's
# HEAD is a file on the host.
t_branch() {
    local out
    # Anything not present and answering is `-`: exec'ing into a container
    # still provisioning, or a connection error, would die mid-listing.
    case "$(t_info "$1")" in
        absent|creating|broken|unreachable) echo -; return 0 ;;
    esac
    out=$(t_exec "$1" git -C "$(t_src "$1")" rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '\r') || out=""
    printf '%s' "${out:--}"
}

# --- creation's completion marker -------------------------------------------
# `.wk-ready`, written as the *last* act of creating a workspace, by every
# driver. Present means creation finished; absent means still going or
# killed -- promoted to the contract because without it a remote or vm
# workspace cut mid-clone looked finished, and `wk build` built the rubble.
# Next to the artifact, never a record on the driving side: the far side for
# a remote, the container's own home for a container. The deviation is vm --
# a freshly cloned guest is not running and visible only from this host, so
# its marker lives in the host-side workspace directory instead.
WK_READY_MARKER=".wk-ready"

# Asked separately from t_info: the interesting case is a marker present
# with the environment *not*, a workspace removed by hand (rule 5). Defaults
# to yes, so a target with no marker mechanism reads as finished.
t_created() { return 0; }

# Waits until a freshly created workspace is usable. Polls the driver's own
# lifecycle answer; synchronous-and-total creation answers `present` on the
# first pass. A driver with something better to say overrides it
# (targets/container.sh prints the failed firstrun's last output).
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
t_cores()      { envelope_cores; }   # t_cores <name>
t_mem_mb()     { envelope_mem_mb; }  # t_mem_mb <name>

# Interactive exec: a full-screen UI needs a pty, and ssh doesn't allocate
# one unless asked. Defaults to the plain exec.
t_exec_tty()   { t_exec "$@"; }

# lldb options a target needs before it can launch anything. Nothing, for a
# machine that behaves like one -- targets/container.sh overrides and says why.
t_lldb_opts()  { :; }               # t_lldb_opts

# Same exec as everywhere except where a build must be serialised: applied
# to t_exec, the lock would also make `wk run`/`wk status` block for no reason.
t_exec_build() { t_exec "$@"; }

# Build state for a target that is a machine of its own: `wk build` writes
# status/log on the side driven from, but a build machine has two sides
# (ssh or a local shell), and seeing only the started half would report
# `build=none` about a build running in front of you. A driver with a far
# side keeps the canonical copy there: t_state_put pushes it, t_wk answers
# by running `wk` there instead of guessing. Both default to "no far side".
t_state_put() { cat >/dev/null; }   # t_state_put <name>, content on stdin
t_has_wk()    { return 1; }         # is there a far side that can answer?
t_wk()        { return 1; }         # t_wk <args...>, its exit status is the answer
# Runs on the far machine and returns immediately: a build must not depend
# on an ssh session staying up for an hour.
t_wk_detach() { return 1; }   # t_wk_detach <args...>

# t_wk with a terminal, for far-side commands that prompt a human.
t_wk_tty()    { t_wk "$@"; }

# In whole cores, as `build_jobs polite` subtracts it. Defaults to this
# machine's own; a driver whose target is another machine answers for that
# machine instead.
t_load() { awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0; }

# Per-workspace target registry. `wk build bug-238 ...` must reach the same
# place `wk new bug-238 --target vm` created, without restating the target
# on every command, so the choice is recorded once, at creation. Kept on
# the host under XDG state, not $WK_STORE, since $WK_STORE is itself
# target-dependent and this is what resolves that.
target_register() {
    local name="$1" target="$2" d
    d="$(wk_state_dir)/targets"
    mkdir -p "$d"
    printf '%s\n' "$target" > "$d/$name"
}

target_forget() { rm -f "$(wk_state_dir)/targets/$1"; }

# Which target a *named* workspace lives on, so `wk build`, `wk pr` and the
# macOS dispatcher cannot reach three different machines for one name.
# Three sources in order: an explicit WK_TARGET overrides everything; the
# registry is the fast path, only a cache of a recomputable fact; this
# machine's own stores are asked directly. The store walk fixes a real
# failure: a `wk new` that dies before registering (an ssh cut, a killed
# driver) leaves a workspace with no record of which machine, and every
# command falls back to `container` and answers "no such workspace" about a
# complete checkout. A file test per target, nothing started, no ssh; loaded
# in a subshell so nothing leaks into the caller.
#
# The container target is the one exception, and worth special-casing rather
# than living with: on a macOS host its workspaces (registry entry and all)
# are inside the podman VM, so the directory test above always misses one --
# unregistered or not -- and every *other* caller of "does this name exist"
# is saved from that by forwarding the whole command into the VM before this
# ever runs (only where=workspace commands with forward=no ask it out here).
# t_info asks the VM directly over the same podman connection Zed's transport
# uses (container.sh's _hpodman) -- still no ssh, and a stopped machine
# answers "absent" in the time one failed connection takes, not a timeout.
ws_exists() { # <name>
    local name="$1" t
    target_of "$name" >/dev/null 2>&1 && return 0
    for t in $(target_all 2>/dev/null); do
        ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
          load_target "$t" >/dev/null 2>&1
          [ -d "$(wk_ws_dir "$name")" ] && exit 0
          [ "$t" = container ] && command -v t_info >/dev/null 2>&1 \
              && [ "$(t_info "$name" 2>/dev/null)" != absent ]
        ) && return 0
    done
    return 1
}

ws_target() {
    local name="$1" t
    if [ -n "${WK_TARGET:-}" ]; then printf '%s' "$WK_TARGET"; return 0; fi
    if target_of "$name" 2>/dev/null; then return 0; fi
    for t in $(target_all 2>/dev/null); do
        # lib/store.sh inside the subshell: not every caller has it (`wk pr`
        # doesn't), and sourcing it sets a default $WK_STORE that must not
        # outlive the question.
        if ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
             load_target "$t" >/dev/null 2>&1
             [ -d "$(wk_ws_dir "$name")" ] ); then
            printf '%s' "$t"
            return 0
        fi
    done
    default_target
}

# The target a workspace was created with, or empty if it is not registered.
# Container workspaces on Linux predate the registry and are the default, so
# "not registered" must resolve to container rather than to an error.
target_of() {
    local f="$(wk_state_dir)/targets/$1"
    [ -f "$f" ] && cat "$f" || return 1
}

# --- am I a workspace? -------------------------------------------------------
# A workspace is a whole machine, and the `wk` inside one has to act on it
# rather than reach one from outside. Claude only ever runs inside a
# workspace, so this path has to work for it to build or test anything.
#
# Provisioning writes a marker file saying so and naming the checkout
# (container/firstrun.sh, targets/vm.sh). A file, not an environment
# variable, since it must be true for everything that reaches in -- an ssh
# command, a hook, a `bash -c` from an agent -- not only inheriting shells.
# When absent this machine is a host, and every reader must treat that as
# the ordinary case, not a broken workspace.
wk_marker() { echo "${WK_MARKER:-$HOME/.wk-workspace}"; }

in_workspace() { [ -f "$(wk_marker)" ]; }

# One `key=value` field from a marker file -- the tolerant reader in
# lib/common.sh, which the status files use too.
marker_field() { kv_field "$1" "$2"; }

wk_marker_field() { marker_field "$(wk_marker)" "$1"; }

# --- am I a machine that *hosts* workspaces? ---------------------------------
# A shared build box is neither a workstation nor a workspace: it holds
# several workspaces at once, so the marker above is the wrong shape. This
# one says "this machine is the far end of a remote target", and names which:
#   target=devbox-arm64-2
#   root=/home/you/wk
# Written by `wk remote setup`, and what makes `wk` usable *on* the box: a
# bare `wk ls` there resolves to the same driver, paths and job policy as
# the workstation driving it, with the ssh step dropped (WK_REMOTE_LOCAL,
# targets/remote.sh). A file, not an exported variable, for the same reason
# as the workspace marker: true for `ssh box wk status` and a cron line.
wk_remote_marker()   { echo "${WK_REMOTE_MARKER:-$HOME/.wk-remote}"; }
in_remote_host()     { [ -f "$(wk_remote_marker)" ]; }
wk_remote_field()    { marker_field "$(wk_remote_marker)" "$1"; }

# The workspace this machine *is*, or empty on a host: makes the workspace
# argument optional inside one (`wk build jsc-release`, claude/CLAUDE.md).
wk_self() { wk_marker_field name; }


# For a workspace not in the registry: `container` on a host (the default,
# and what every workspace predating the registry is); `local` inside one,
# since there's nothing else in there.
default_target() {
    if in_workspace; then echo local; return 0; fi
    local t; t=$(wk_remote_field target)
    if [ -n "$t" ]; then echo "$t"; return 0; fi
    echo container
}

# The config to build, run or test when the caller names none. jsc-release
# is right on a host and in a container, but meaningless in a macOS guest,
# which can build nothing but the Apple ports -- so a bare `wk run` there
# would look for a JSCOnly binary that cannot exist. The marker carries the
# answer, written by whoever wrote it: container/firstrun.sh knows the
# container builds JSCOnly, targets/vm.sh knows the guest's tree.
default_config() {
    local c=""
    in_workspace && c=$(wk_marker_field config)
    printf '%s' "${c:-jsc-release}"
}

# The config this workspace was last *built* with (`config=` in
# build.status), or empty when it has never been built here.

# What makes `wk test <ws> --layout` usable: the fallback default is
# jsc-release, which resolves to `--jsc-only`, a port with no
# WebKitTestRunner or ImageDiff -- the wrong tree for layout tests. The
# config a person means is the one they just built, and the build says
# which that was. Evidence, not a preference: an empty answer means
# "nothing has been built" rather than a guess.
last_built_config() {
    local name="$1"
    ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
      load_target "$(ws_target "$name")" >/dev/null 2>&1
      kv_field "$(wk_ws_dir "$name")/build.status" config 2>/dev/null ) || true
}

# --- generated ssh aliases ---------------------------------------------------
# Written under ~/.ssh/config.d, not ~/.ssh/config itself, so it can be
# rewritten freely without touching hand-maintained entries. Rewritten, not
# appended: a VM's address changes on every boot, and an alias only ever
# added would point at a stale IP while Zed times out against it.

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

# ssh_alias_set <name> <hostname> <user> [identity] [extra-line...]
# The extra lines are for a target not reached by address at all: a
# container has no interface and is reached by a ProxyCommand over `podman
# exec`. Passed in rather than branched on here, since which lines are
# needed is the driver's knowledge.
ssh_alias_set() {
    local name="$1" hostname="$2" user="$3" conf extra
    conf=$(wk_ssh_conf)
    ensure_dir "$(dirname "$conf")" 0700
    ssh_alias_remove "$name"

    # A per-target identity only when there is one: a macOS guest has a key
    # of its own; a container authorises this machine's zed key (below).
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
# One key per machine, for reaching a container workspace's sshd over
# `podman exec` (targets/container.sh).

# Not one of the deploy keys under $WK_STORE/secrets: those are push-only
# GitHub credentials, read-only to a workspace. This is the opposite
# direction -- this machine authenticating to its own containers -- so it is
# machine-local, generated on demand, regenerable at any moment.

# Its own key rather than reusing ~/.ssh/id_ed25519, same reason the vm
# driver has one: a person's key may be passphrase-protected, held in an
# agent, or absent, and an editor launch is not the place to discover that.
zed_key() { echo "$(wk_state_dir)/ssh/zed_ed25519"; }

zed_key_pub() {
    local k; k=$(zed_key)
    if [ ! -f "$k" ]; then
        ensure_dir "$(dirname "$k")" 0700
        ssh-keygen -q -t ed25519 -N '' -C "wk zed key ($(hostname -s 2>/dev/null || echo host))" \
            -f "$k" || return 1
        changed "generated this machine's zed key ($k)"
    fi
    cat "$k.pub"
}

# Every registered target, plus container, which is the default and is what
# anything unregistered is. Used by commands that report on everything rather
# than acting on one named workspace.
target_all() {
    local d f t seen=" container "
    echo container

    # On a shared build machine the workspaces were created from somewhere
    # else, so nothing here is registered -- without this a bare `wk status`
    # on the box would report nothing about the ones it has.
    t=$(wk_remote_field target)
    if [ -n "$t" ]; then seen="$seen$t "; echo "$t"; fi

    d="$(wk_state_dir)/targets"
    if [ -d "$d" ]; then
        for f in "$d"/*; do
            [ -f "$f" ] || continue
            t=$(cat "$f")
            case "$seen" in *" $t "*) continue ;; esac
            seen="$seen$t "
            echo "$t"
        done
    fi

    # Every machine configured, whether or not a workspace is currently
    # pinned to it -- a machine you set up is a target you have. Two
    # directories, since a target has two halves: the registry in this
    # repository (every device gets by pulling) and the machine-local conf
    # (this device's own view). Both are enumerated.

    # The registry is skipped on a machine that is the far end of a target
    # (a build box or the podman VM, both pushed the whole repository):
    # otherwise a delegated `wk ls` there would pay an ssh timeout for every
    # machine it has no route, key or business reaching. Neither drives
    # anything -- a build box answers for itself, from the marker -- while a
    # *peer* workstation does drive things and keeps the whole registry.
    local me=""
    if ! in_remote_host && [ -z "${WK_IN_VM:-}" ]; then
        me=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')
        for d in "$(target_registry_dir)"; do
            [ -d "$d" ] || continue
            for f in "$d"/*.conf; do
                [ -f "$f" ] || continue
                t=$(basename "$f" .conf)
                case "$seen" in *" $t "*) continue ;; esac
                # ...and never itself: without this, a bare `wk status` on
                # the workstation would ssh to itself asking what it already
                # knows, and report itself unreachable with no sshd inside.
                [ -n "$me" ] && [ "$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')" = "$me" ] && continue
                seen="$seen$t "
                echo "$t"
            done
        done
    fi
}

# Call <fn> once per configured remote machine -- every target_all entry
# except container, vm and local, which are this machine already. <fn> gets
# the target name as $1 plus any extra arguments; its own exit status folds
# into the worst one seen, for_each_machine's return.

# `wk key register`, `wk push --all` and `wk sudo --all` each walked this
# list with their own copy of the skip case; one walk means it can only
# drift in one place.
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

# --- the podman machine (macOS) -----------------------------------------------
# Every workspace container lives inside one podman machine, so `wk
# start`/`wk stop` need to run a command inside it from the host, and know
# its lifecycle state before deciding whether there is anything to do.

# The machine's lifecycle state, or "absent" if $1 was never created --
# podman errors on an unknown machine name rather than reporting one, so
# that has to be read back off the failure.
_machine_state() {
    podman machine inspect "$1" --format '{{.State}}' 2>/dev/null || echo absent
}

# Run a command inside the podman machine from the host, or in place already
# inside a workspace (WK_IN_VM) or on Linux, where there is no machine to
# reach into. Named apart from _vm() (targets/vm.sh's Tart guest) so
# sourcing both does not clobber either.

# </dev/null is essential, not defensive: a caller feeding this from a
# `while read` loop over a heredoc has its stdin read by podman-machine-ssh
# otherwise, and the first iteration swallows every line meant for the rest.
_in_machine() {
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        podman machine ssh "${WK_MACHINE:-wk}" "$@" </dev/null
    else
        bash -c "$*" </dev/null
    fi
}

# --- named targets -----------------------------------------------------------
# A target name is either one of the four built-in kinds -- container, vm,
# remote, local -- or a machine configured under targets/hosts/<name>.conf.

# `remote` is the one kind with several instances: there is exactly one
# podman, one Tart, one local machine, but a shared build box isn't unique,
# so the machine's own name is the target (`wk new bug-238 --target
# devbox-arm64-2`) and its conf says which driver to use and how to reach it.

# One place in this repository, pulled by every device. A target's conf is
# true of the *machine* regardless of who drives it, and its addressing is
# accepted as published (CLAUDE.md). No per-device override file (CLAUDE.md,
# "One path, not two"); a key or secret is genuinely per-device and does not
# live in a target conf, while the environment is genuinely per-invocation
# and still wins over one (_target_reset_vars). No new machinery: the
# registry is committed files, pushed by `wk remote setup`/`t_sync_tools`.
target_registry_dir() { echo "$WK_ROOT/targets/hosts"; }
target_registry_conf(){ echo "$(target_registry_dir)/$1.conf"; }

# The driver a target name selects. The built-in kinds are their own kind;
# anything else must have a conf, and one that names no kind falls through
# to `remote`, the only kind worth naming after a machine.
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

# Per-target driver state, cleared before every load: more than one target
# can be loaded in a process (`wk status` walks every target), and without
# this the second remote machine would inherit the first one's host, root
# and measured capacity. The environment still wins over a conf, so what it
# supplied is snapshotted once and restored on each load, named here rather
# than in the driver since the reset must happen *before* the conf is sourced.
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
    # Per-machine CMake defaults come from the conf about to be sourced, so
    # the previous target's must not survive into this one.
    WK_TARGET_CMAKE=""
    unset _WK_REMOTE_PROBED _WK_REMOTE_HOME _WK_REMOTE_CORES \
          _WK_REMOTE_LOAD _WK_REMOTE_MEM _WK_REMOTE_REF_PROBED _WK_REMOTE_DOWN
}

# --- what state is this workspace in? ----------------------------------------
#   absent       nothing of it exists anywhere
#   creating     something of it exists, and creation never finished
#   present      it exists and creation finished
#   broken       creation finished and the environment is gone -- something
#                outside wk removed it (rule 5)
#   unreachable  the machine that holds it did not answer in time

# Derived on every call from evidence next to the artifacts, stored nowhere.
# Three pieces, in order: the driver's own answer (t_info), where
# `creating`/`unreachable` come from; the `base-id` file for a target that
# pins a base snapshot (the container driver writes it last, so one without
# it was interrupted); t_created, when the environment is absent but a
# workspace directory is here (with the marker, something removed a
# finished environment; without it, this is unfinished-creation rubble).
#
# `ws.status` is not evidence and is never believed about a workspace that
# exists: a claim by the creating process, read only for whether that
# process is still alive (wait_ready), or, for a marker since emptied,
# whether it ever said it finished (rule 5). The registry is not consulted
# either: a cache of which target a workspace was created with, and a
# workspace that predates it must not read as half-made.
ws_state() {
    local name="$1" env ws
    env=$(t_info "$name" 2>/dev/null || echo absent)
    ws=$(wk_ws_dir "$name")

    case "$env" in
        creating|unreachable) echo "$env"; return 0 ;;
    esac

    if [ "$env" = absent ]; then
        [ -d "$ws" ] || { echo absent; return 0; }
        # A ws directory with no environment behind it. The marker decides
        # which: with it, something removed a finished environment by hand;
        # without it, it's rubble from an interrupted creation or `wk rm`
        # (rule 3 says remake).
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

# The word `wk ls`/`wk status` print in STATE: the driver's own answer,
# except a workspace whose lifecycle is not `present` reports that instead
# -- a container inside a never-finished workspace is not "running".
ws_display_state() {
    local st; st=$(ws_state "$1")
    case "$st" in
        present) t_info "$1" ;;
        *)       echo "$st" ;;
    esac
}

# What the creating process claimed, and its transcript. A claim, never the
# record -- see ws_state. Beside the workspace directory, not inside it: a
# re-run of `wk new` over a half-made workspace *destroys* that directory
# first (rule 3), and the log the driver is writing to must not be the file
# it deletes -- the fd would survive, pointing at nothing. Removed by `wk
# rm`, with the workspace they describe.
ws_state_dir()   { echo "$WK_STORE/create"; }
ws_status_file() { echo "$(ws_state_dir)/$1.status"; }
ws_create_log()  { echo "$(ws_state_dir)/$1.log"; }

# Where the command that remakes a workspace has to be typed. `wk new` is a
# workstation command -- a machine that only *hosts* workspaces refuses it
# (is_lifecycle in `wk`) since the registry lives on the workstation. So
# printing it bare on a build box sends somebody straight to a refusal.
ws_remake_hint() {
    if in_remote_host; then
        printf 'from the workstation:  wk new %s --target %s' "$1" "$(wk_remote_field target)"
    else
        printf 'wk new %s%s' "$1" "${WK_TARGET:+ --target $WK_TARGET}"
    fi
}

# --- one gate: wait_ready ----------------------------------------------------
# Block until this workspace is usable, honest about every other outcome.
# The one place anything asks "may I act on this yet", so zed, a build and
# the babysitter cannot each answer it differently:
#   present      return, immediately in the normal case
#   creating     wait -- and say what it is waiting for, once
#   creating,    the driver died with the connection that started it: say so
#   driver dead  and name the fix (`wk new`, remade from scratch -- rule 3)
#   broken       refuse, and name the repair
#   unreachable  refuse, with the timeout that decided it -- never a hang
#   absent       refuse

# Foreground by design: if this waiter is killed, only the waiting stops --
# creation is detached and continues, and re-running the same command is the
# resume. A waiter that detached itself and opened an editor would be
# opening windows into a session that had gone.
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
            die "'$name' lives on a machine that did not answer (${WK_SSH_TIMEOUT:-10}s).
    Nothing is wrong with the workspace as far as this end can tell -- it
    cannot be reached to ask. Try again, or check the route:
        ssh -o BatchMode=yes ${WK_REMOTE_HOST:-the machine} true"
            ;;
        creating)
            if ! detach_alive "$sf"; then
                # A barrier, not a refusal (lib/common.sh's distinction): this
                # command *can* proceed. A clone that finished and never got
                # its marker looks exactly like one cut in the middle, and
                # only the person looking at it can tell which. --force
                # returns and acts on the workspace as it is, repeating the
                # warning when the command ends, as everywhere else.
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
# A lock (hold_lock, lib/common.sh) is a process on this machine and dies
# with it -- right for a command that does its work and exits, but covers
# nothing for work *detached into the workspace*: `wk sysimage build --stage
# image --detach` starts bitbake through t_spawn and returns, so the lock is
# gone within the second while the build runs for hours. A `wk build` in the
# same workspace then takes the workspace lock and puts a second writer into
# the same checkout, corrupting both.

# Not a longer-lived lock: "is anything running in here" has an answer at
# the artifact instead -- a detached job writes its pid into the workspace's
# home directory, and the workspace can be asked whether that pid is alive.

# The whole interface: **a job detached into a workspace writes
# `$(t_home)/<job>.pid`, and removes nothing on the way out** -- a stale
# file is expected and costs one liveness check. `image/yocto.sh` writes
# `yocto-<stage>.pid` through it, and anything added later is serialised
# against `wk build` for free by doing the same.

# Prints what it found, so a refusal can name it.
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

# The targets a report covers: `wk ls`/`wk status` must enumerate the same
# set here and in target_workspaces below (they didn't -- `wk status` walked
# every target while `wk ls` on macOS forwarded into the podman VM and
# answered for containers alone). WK_TARGET (may name several) wins; inside
# a workspace there is exactly one target.
# --- one round of probes instead of N ----------------------------------------
# A bare `wk status` walks every target serially, and an ssh machine costs
# a full connect -- ten seconds (WK_SSH_TIMEOUT) when off -- so four
# machines, two away, meant twenty seconds before the first row. The
# waiting now happens once, before the walk: one subshell per target asks
# its own machine and writes the answer where the serial pass will find it
# (t_prefetch, _remote_probe_file in targets/remote.sh); the report itself
# is untouched. This also warms the ssh multiplexing socket (ControlPersist,
# targets/remote.sh), so a down machine is down *once*, not once per question.

# Best-effort: a prefetch that fails leaves no file, and the walk asks the
# machine directly.
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

    # WK_NO_DELEGATE: one machine's contribution to somebody else's fleet
    # listing, so it reports what it holds and asks nobody -- without it, a
    # workstation asked by another workstation would ask every machine *it*
    # knows too, squaring the ssh work over the shared registry.

    # A machine's own target survives it: on a build box the target named
    # after that machine *is* this machine (WK_REMOTE_LOCAL, set by `wk
    # remote setup`), and dropping it would drop every workspace it holds.
    # Decided from the conf, so this costs no ssh.
    if [ -n "${WK_NO_DELEGATE:-}" ]; then
        local t
        for t in $(target_all); do
            case "$(target_kind "$t")" in
                remote) ( load_target "$t" >/dev/null 2>&1
                          [ -n "${WK_REMOTE_LOCAL:-}" ] ) || continue ;;
            esac
            printf '%s\n' "$t"
        done
        return 0
    fi

    target_all
}

# Every workspace a loaded target has, from both sides of it. The local
# store knows what this machine created; the driver knows what actually
# exists over there. Usually the same set, not always: a remote target's
# workspaces live on the far machine, so `wk ls` run *on* it has an empty
# store and a full ws directory. Listing only one side is how a machine
# reports "no workspaces" while holding six.
target_workspaces() {
    { list_workspaces 2>/dev/null || true
      t_list 2>/dev/null | cut -f1 || true
    } | grep -v '^[[:space:]]*$' | sort -u
}

load_target() {
    local t="${1:-container}" kind conf

    # $WK_STORE is target-dependent (vm keeps state under XDG on the host,
    # container's lives in /var/lib/wk) and drivers set it when sourced.
    # Reset first so a second load in one process doesn't inherit the first.
    : "${WK_STORE_DEFAULT:=${WK_STORE:-/var/lib/wk}}"
    WK_STORE="$WK_STORE_DEFAULT"

    kind=$(target_kind "$t") || die "unknown target '$t'.
    The built-in ones are container, vm, remote and local. Anything else is a
    machine, and needs a conf -- in the registry, so every device gets it:

        $(target_registry_conf "$t")
            WK_REMOTE_HOST=$t      # an ssh destination that already works
            WK_REMOTE_ROOT=/home/you/wk

    'wk remote setup $t' writes it for you."

    # Set before either file is read, since both are written in terms of the
    # name. WK_TARGET is the name, WK_TARGET_KIND the driver behind it:
    # `[ "$TARGET" = remote ]` was true only while one name existed.
    WK_TARGET="$t"
    WK_TARGET_KIND="$kind"
    export WK_TARGET WK_TARGET_KIND

    _target_reset_vars

    # And the *functions*: a definition outlives the load, so the first
    # driver's override stays live for the second (`wk status` on a build
    # machine walks container first, and every remote branch is then read
    # by the container driver's t_branch, answering `-` plausibly and
    # wrongly). Re-sourcing restores every default; function definitions
    # only, the running load_target keeps its own body.
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
