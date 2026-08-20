# Loading a target driver, and the defaults every driver inherits.
#
# Commands under cmd/ never talk to podman, tart or ssh directly -- they call
# the contract below and nothing else. That is what makes "add a target" mean
# "add one file under targets/" rather than "audit every command".
#
# Required of a driver:
#
#   t_create <name> [base]   bring the environment into existence
#   t_exec   <name> <cmd..>  run a command in it
#   t_enter  <name>          interactive shell
#   t_destroy <name>         remove it and everything it created
#   t_info   <name>          the lifecycle in one word: "absent", "creating",
#                            or the driver's word for an environment that
#                            exists ("running", "exited", "present"); a driver
#                            that reaches over a network may also answer
#                            "unreachable"
#   t_list                   "<name>\t<state>" per line
#
# Optional, defaulted here. A driver overrides only what is genuinely
# different about it; re-stating a default is how the two copies drift apart.
#
#   t_src <name>       where the WebKit checkout is *inside* the target
#   t_arch <name>      the architecture the target runs natively
#   t_tools <name>     where wk-tools is inside the target
#   t_sync_tools <n>   push wk-tools in (no-op when it is bind-mounted)
#   t_sync             refresh the target's own far-side state, if it has any
#   t_ssh_host <name>  ssh destination, for Zed and the generated alias
#   t_needs_base       0 if `wk new` must resolve a base snapshot first
#   t_start / t_stop   lifecycle, for targets that have one
#   t_cores/t_mem_mb   the resources the target actually has
#   t_load             the load already on the target, for the polite sizing
#   t_ccache_dir       where ccache keeps its cache inside the target
#   t_store_init       create the host-side directories this target needs
#   t_created <name>   is creation's completion marker there? (see .wk-ready)
#   t_ready            block until a new workspace finished initialising
#   t_exec_build       t_exec, for a build specifically (see below)
#   t_state_put        keep a copy of the build status where the target is
#   t_has_wk / t_wk    run `wk` on the target's own machine, if that is one
#
# One driver is the degenerate case of all this: targets/local.sh, where the
# target is the machine the command is already running on -- a workspace acting
# on itself. See "am I a workspace?" below for how that is detected.
#
# t_cores/t_mem_mb are separate from lib/resources.sh on purpose. A build has
# to be sized from the memory of the machine it runs on, and for a vm target
# that is the *guest*, not the host -- sizing a macOS VM build from the host's
# 32 GB when the guest was booted with 12 would pick a job count the guest
# cannot honour, which is the same class of mistake as sizing a container from
# MemAvailable instead of its cgroup limit.

t_src()        { echo "/src/WebKit"; }

# The architecture a workspace runs natively -- `native` unless something said
# otherwise at creation. Only the container driver can currently answer
# anything else, because only a container can be a different architecture from
# the host it runs on; a VM guest and a remote box are whatever they are.
#
# This is deliberately not "what would I build for": that question has a second
# answer once cross builds exist, and the two must not share a word. See
# lib/arch.sh.
t_arch()       { echo native; }
t_tools()      { echo "/opt/wk-tools"; }
# The bind-mounted store cache in a container, and inert on the Apple ports,
# which do not use ccache at all. A target that is somebody else's machine has
# to answer with a directory of its own -- see targets/remote.sh.
t_ccache_dir() { echo "/ccache"; }
t_sync_tools() { :; }

# `wk sync` against this target, when the target is a machine with state of its
# own to refresh. Everything else -- container, vm, local -- keeps its base in
# this machine's store, and there `wk sync` *is* the operation (cmd/sync); a
# driver hook would be a second name for the same thing. Returning 1 says "I
# have no far side", which is what cmd/sync branches on.
t_sync()       { return 1; }

# Ask this target whatever a report is going to need, so the asking can happen
# for every target at once. Nothing to do for a target that is this machine.
t_prefetch()   { :; }

# How this target's checkouts are wired, for whoever re-asserts it: three
# lines, any of which may be empty --
#
#   <name of the local-copy remote>   (mirror, shared)
#   <its url, on the target>
#   <an ssh config the checkout must use, if not ~/.ssh/config>
#
# A contract hook rather than a table in the command that re-asserts, because
# only the driver knows: a shared build box keeps its ssh aliases in a file
# under the wk root (its $HOME is several machines' and often several people's)
# and has a local copy of the history to fetch from, while a container has
# neither. Creation reads the same three lines, so a re-assertion cannot wire a
# workspace differently from the way it was made.
t_wiring_args() { printf '\n\n\n'; }
t_ssh_host()   { echo "wk-$1"; }
t_needs_base() { return 0; }
t_start()      { :; }
t_stop()       { :; }
t_store_init() { store_init; }

# The workspace user's home directory, as seen from inside the workspace.
#
# Needed by anything that has to name one file from both sides -- a detached
# build's log, which the far side writes and this side follows. In a container
# that directory is a host bind mount, which is what makes following it free;
# see t_spawn.
t_home()       { echo "$HOME"; }

# t_pull <name> <src-in-target> <dest-on-host>
#
# Copy one file out of a workspace, byte for byte.
#
# This exists because the obvious spelling -- `t_exec <ws> cat <file>` into a
# host-side redirect -- silently corrupts binary data. Measured: a 1396-byte
# file came out of a container workspace as 1399 bytes, and `xz` said
# "Compressed data is corrupt". `wkdev-enter` is an interactive-shell wrapper
# and its stdout is not a byte pipe; nothing about that is visible from the call
# site, which is what makes it worth a named primitive rather than a comment.
#
# The default is right for the `local` driver, where the workspace *is* this
# machine. Anything reached over a network needs its own -- see the container
# driver, and note that a `vm` or `remote` target has none yet because the one
# caller (the yocto builder) refuses those targets outright.
t_pull() {
    local name="$1" src="$2" dest="$3"
    cp -f "$src" "$dest"
}

# t_spawn <name> <log> <pidfile> <cmd...>
#
# Start a command in the workspace, detached from *this* process, and return at
# once. `log` and `pidfile` are paths inside the target.
#
# This exists because a multi-hour build must not depend on the process driving
# it staying alive, and because the obvious spelling of that does not work
# everywhere: over ssh, `nohup cmd &` genuinely survives the connection closing,
# which is what this default relies on. Under `podman exec` it does not --
# `setsid nohup` and all -- so the container driver overrides this with podman's
# own detached exec. Measured, not assumed: a `setsid nohup` started through
# `podman exec` and given nothing to do but sleep 3 and write a file was gone
# before it wrote anything.
#
# The exit status of a process nobody forked cannot be waited for, so a caller
# has to decide "finished" from what the command left behind -- which is why
# the yocto builder's wrapper ends by printing a marker line.
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    t_exec "$name" bash -lc "setsid nohup $(sh_quote "$@") \
        > $(sh_quote "$log") 2>&1 < /dev/null & echo \$! > $(sh_quote "$pidf")"
}

# The branch this workspace's checkout is on, or `-` when it cannot be known
# without starting something.
#
# `wk status` prints it per workspace, because "which branch is that one on"
# is the question you ask before every build and the one thing the listing
# could not answer -- and with several workspaces open at once, guessing from
# the name is how the wrong tree gets built.
#
# Asked of the target, never of a record: a checkout's branch is git's to
# answer. The default does that over the driver's own exec, which is right for
# a machine that is up; a driver whose checkout is readable from here without
# entering anything overrides it (targets/container.sh does -- an overlay's
# HEAD is a file on the host, and reading it costs nothing).
t_branch() {
    local out
    # Only a workspace that exists and finished being made has a branch to
    # ask about, and only a machine that answers can be asked. Anything else
    # is `-`: asking would mean an exec into a container that is still being
    # provisioned, or -- measured 2026-08-19, in `wk status` against a machine
    # that was off -- resolving a remote path, which reaches for the capacity
    # probe and dies with a connection error in the middle of a listing.
    case "$(t_info "$1")" in
        absent|creating|broken|unreachable) echo -; return 0 ;;
    esac
    out=$(t_exec "$1" git -C "$(t_src "$1")" rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '\r') || out=""
    printf '%s' "${out:--}"
}

# --- creation's completion marker -------------------------------------------
#
# One file name, written as the *last* act of creating a workspace, by every
# driver: `.wk-ready`. Present means creation finished; absent means it is
# still going or something killed it. It is the same pattern as the base
# snapshot's `sha` and the container's old `.wk-firstrun-complete`, promoted to
# the contract -- because without it a remote or vm workspace cut mid-clone
# looked finished to every command, and `wk build` built the rubble.
#
# Next to the artifact, never in a record on the driving side: the far side for
# a remote (so the box itself, and any other workstation, derive the same
# answer), the container's own home for a container. The one deviation is the
# vm target, and it is a property of the thing rather than a shortcut -- a
# freshly cloned guest is not running, so there is nothing in there to write
# to, and the guest is visible from this host and nowhere else; its marker
# therefore lives in the host-side workspace directory. Written by whoever
# performs the last step of creation, which for a container is provisioning
# inside it (container/firstrun.sh).
WK_READY_MARKER=".wk-ready"

# Is creation's completion marker there? Asked of the driver, because only it
# knows where the workspace's artifacts are -- and asked separately from
# t_info, because the interesting case is exactly the one where the marker is
# there and the environment is *not*: that is a workspace somebody removed by
# hand, not one that never finished (rule 5).
#
# Defaults to yes, so a target with no marker mechanism behaves as it did
# before this existed rather than reporting every workspace half-made.
t_created() { return 0; }

# Wait until a freshly created workspace is actually usable, and fail if it
# never gets there.
#
# The default polls the driver's own lifecycle answer, which is what `creating`
# means there: a driver whose creation is synchronous and total answers
# `present` on the first pass and this returns immediately. A driver with
# something better to say on failure overrides it (targets/container.sh prints
# the container's last output, which is where a failed firstrun explains
# itself).
t_ready() {
    local name="$1" i=0 max="${WK_READY_TIMEOUT:-300}"
    while [ "$i" -lt "$max" ]; do
        case "$(t_info "$name")" in
            creating) ;;
            # Creation has returned and left nothing behind, or left a machine
            # that cannot be asked. Neither gets better by waiting five
            # minutes for it.
            absent|unreachable) return 1 ;;
            *) return 0 ;;
        esac
        sleep 1; i=$((i + 1))
    done
    return 1
}
t_cores()      { envelope_cores; }   # t_cores <name>
t_mem_mb()     { envelope_mem_mb; }  # t_mem_mb <name>

# Interactive exec. Anything with a full-screen UI -- Claude, an editor, a
# pager -- needs a pty, and ssh does not allocate one for a command unless it
# is asked to. Defaults to the plain exec, which is right wherever the driver
# already gives you a terminal.
t_exec_tty()   { t_exec "$@"; }

# The exec `wk build` uses, which is the same one everywhere except where a
# build specifically has to be serialised. On a shared machine two of your own
# builds must not stack, and the lock that guarantees it belongs on the build
# alone: applied to t_exec it would also make `wk run`, `wk status` and every
# small probe block for up to an hour behind a build, which is a hang with no
# explanation attached.
t_exec_build() { t_exec "$@"; }

# Build state, for a target that is a machine of its own.
#
# `wk build` writes build.status and build.log on the side it was driven from,
# which is the only side that knows how the build ended. That is the whole
# answer for a container or a guest, because there is exactly one side. A build
# machine has two: a build can be started from the workstation over ssh or from
# a shell on the box, and a `wk status` that only ever saw the half it started
# would report `build=none` about a build running in front of you.
#
# So a driver that has a far side keeps the canonical copy over there, and
# these two hooks are how: t_state_put pushes the status as it changes, and
# t_wk answers the question by running `wk` on that machine instead of guessing
# from here. Both default to "there is no far side", which is true everywhere
# else and leaves those targets exactly as they were.
t_state_put() { cat >/dev/null; }   # t_state_put <name>, content on stdin
t_has_wk()    { return 1; }         # is there a far side that can answer?
t_wk()        { return 1; }         # t_wk <args...>, its exit status is the answer
# Start a `wk` command on the far machine and return immediately, leaving it
# running there. The connection that started it is then free to drop -- which
# is the whole point: a build must not depend on an ssh session staying up for
# an hour, and the machine doing the work is the machine that should own the
# process. Defaults to "there is no far side", where the caller detaches
# locally instead.
t_wk_detach() { return 1; }   # t_wk_detach <args...>

# t_wk with a terminal, for the far-side commands that have to ask a human
# something -- `wk sudo require` is going to prompt for a password, and ssh
# gives a command no tty unless it is asked to.
t_wk_tty()    { t_wk "$@"; }

# The load already on the target, in whole cores, as `build_jobs polite`
# subtracts it. Defaults to this machine's own -- correct for a container,
# which shares the host's kernel and therefore its load average, and for the
# degenerate local target, which *is* this machine. A driver whose target is
# another machine has to answer for that machine instead.
t_load() { awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0; }

# Per-workspace target registry.
#
# `wk build bug-238 ...` must reach the same place `wk new bug-238 --target vm`
# created, without the target being restated on every command -- and on macOS
# the dispatcher has to know the answer *before* it decides whether to forward
# into the podman VM. So the choice is recorded once, at creation.
#
# Kept on the host under XDG state rather than in $WK_STORE, because $WK_STORE
# is itself target-dependent and this is what resolves that.
wk_state_dir() { echo "${XDG_STATE_HOME:-$HOME/.local/state}/wk"; }

target_register() {
    local name="$1" target="$2" d
    d="$(wk_state_dir)/targets"
    mkdir -p "$d"
    printf '%s\n' "$target" > "$d/$name"
}

target_forget() { rm -f "$(wk_state_dir)/targets/$1"; }

# Which target a *named* workspace lives on, and the one answer every command
# uses -- so `wk build`, `wk pr` and the macOS dispatcher cannot reach three
# different machines for one name.
#
# Three sources, in order, and the last two are why this exists at all:
#
#   an explicit WK_TARGET, which is how a caller overrides everything;
#   the registry, which is the fast path and is only ever a cache of a fact
#   that can be recomputed (the audit in docs/HANDOFF-workspace-state.md
#   reclassifies it as one);
#   this machine's own stores, asked directly: whichever target has a
#   workspace directory of this name is the target it lives on.
#
# The store walk is what fixes a real failure. A `wk new` that dies before it
# registers -- an ssh cut, a killed driver, a wk-tools update mid-run -- leaves
# a workspace on a machine and no record of which machine, and every command
# then fell back to `container`, asked podman, and answered "no such
# workspace: <name>" about a checkout sitting on a build box. Measured
# 2026-08-19 with `wk pr db …`, whose clone was complete on devbox-arm64-2.
#
# A file test per configured target, nothing started and no ssh: a remote
# target's host-side store is on this machine (it holds the build log), and a
# vm's is too. Drivers are loaded in a subshell so nothing leaks into the
# caller -- load_target sets and exports the target's identity.
ws_target() {
    local name="$1" t
    if [ -n "${WK_TARGET:-}" ]; then printf '%s' "$WK_TARGET"; return 0; fi
    if target_of "$name" 2>/dev/null; then return 0; fi
    for t in $(target_all 2>/dev/null); do
        # lib/store.sh inside the subshell, because not every caller has it --
        # `wk pr` and the dispatcher itself do not -- and because sourcing it
        # sets a default $WK_STORE that must not outlive the question.
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
# A workspace is a whole machine -- a podman container or a macOS guest -- and
# the `wk` inside one has to act on that machine rather than try to reach one
# from outside. Claude only ever runs inside a workspace, so this is the path
# that has to work for it to build or test anything at all.
#
# Provisioning writes a marker file saying so and naming the checkout:
# container/firstrun.sh in a container, targets/vm.sh in a macOS guest. A file
# rather than an environment variable, because it has to be true for everything
# that reaches in -- an ssh command, a hook, a `bash -c` from an agent -- and
# not only for the shells that happened to inherit the right environment.
#
# When the marker is absent this machine is a host, and every path that reads it
# must behave exactly as it did before it existed.
wk_marker() { echo "${WK_MARKER:-$HOME/.wk-workspace}"; }

in_workspace() { [ -f "$(wk_marker)" ]; }

# One `key=value` field from a marker file -- the tolerant reader in
# lib/common.sh, which the status files use too.
marker_field() { kv_field "$1" "$2"; }

wk_marker_field() { marker_field "$(wk_marker)" "$1"; }

# --- am I a machine that *hosts* workspaces? ---------------------------------
# A shared build box is neither a workstation nor a workspace. It holds several
# workspaces at once, so the marker above -- which says "this machine IS a
# workspace" and names one -- is the wrong shape for it. This one says "this
# machine is the far end of a remote target", and names which:
#
#   target=devbox-arm64-2
#   root=/home/you/wk
#
# Written by `wk remote setup`, and what makes `wk` usable *on* the box: with
# it, a bare `wk ls` or `wk build bug-238 jsc-release` there resolves to the
# same driver, the same paths and the same job policy as the workstation
# driving it -- with the ssh step dropped (WK_REMOTE_LOCAL, targets/remote.sh).
#
# A file rather than an exported variable, for the same reason as the workspace
# marker: it has to be true for `ssh box wk status` and for a cron line, not
# only for shells that sourced a profile.
wk_remote_marker()   { echo "${WK_REMOTE_MARKER:-$HOME/.wk-remote}"; }
in_remote_host()     { [ -f "$(wk_remote_marker)" ]; }
wk_remote_field()    { marker_field "$(wk_remote_marker)" "$1"; }

# The workspace this machine *is*, or empty on a host. This is what makes the
# workspace argument optional inside one: `wk build jsc-release` rather than
# `wk build <name> jsc-release`, which is the interface claude/CLAUDE.md
# documents and the only one available in here.
wk_self() { wk_marker_field name; }

# ...and the other half of that: in here the name is not optional, it is
# *absent*. A workspace name on the command line inside a workspace is refused
# rather than accepted.
#
# It used to be accepted, silently, when it happened to be this workspace's own
# name -- `wk build bug-238 jsc-release` in bug-238 built exactly what
# `wk build jsc-release` builds. Which is worse than it looks: it is the host
# form typed in the wrong place, so the next thing typed is usually the host
# form for a *different* workspace, and that one cannot work in here at all.
# Two spellings of one command also means everything written about the in-here
# interface -- claude/CLAUDE.md, the skills, `--explain` -- is describing one of
# them and being read against the other. One form, and a refusal that names it.
#
#   refuse_ws_name <word> <cmd> [<what-to-type-instead>]
#
# Only this workspace's own name is refused, because it is the only word that
# can be *known* to be a workspace name: every other argument these commands
# take -- a config, a test path, a jsc script, an option -- is a word we have no
# registry to check it against, and refusing on a guess would break the
# arguments the command exists to pass through.
refuse_ws_name() {
    local word="${1:-}" cmd="${2:-}" form="${3:-}"
    in_workspace || return 0
    [ -n "$word" ] || return 0
    [ "$word" = "$(wk_self)" ] || return 0
    die "this is workspace '$(wk_self)', and there is no workspace argument in here --
    every command acts on this one. Drop the name:
        wk $cmd${form:+ $form}"
}

# The target to assume for a workspace that is not in the registry. On a host
# that is `container` -- the default, and what every workspace predating the
# registry is. Inside a workspace it is `local`, because there is nothing else
# in there: no podman, no tart, and one workspace, which is this machine.
default_target() {
    if in_workspace; then echo local; return 0; fi
    local t; t=$(wk_remote_field target)
    if [ -n "$t" ]; then echo "$t"; return 0; fi
    echo container
}

# The config to build, run or test when the caller names none.
#
# jsc-release is right on a host and in a container, where JSCOnly is what gets
# built. It is meaningless in a macOS guest, which can build nothing but the
# Apple ports -- so a bare `wk run` in there went looking for a JSCOnly binary
# that cannot exist, and reported the path it could not find rather than the
# reason. The marker carries the answer, written by whoever wrote the marker:
# container/firstrun.sh knows the container builds JSCOnly, and targets/vm.sh
# knows the guest's tree is $WK_VM_BASE_PREBUILD.
default_config() {
    local c=""
    in_workspace && c=$(wk_marker_field config)
    printf '%s' "${c:-jsc-release}"
}

# The config this workspace was last *built* with, from the record the build
# itself wrote (`config=` in build.status), or empty when it has never been
# built here.
#
# This is what makes `wk test <ws> --layout` usable: the fallback default is
# jsc-release, which resolves to `--jsc-only` -- a port that builds jsc and
# nothing else, so run-webkit-tests was being pointed at a tree with no
# WebKitTestRunner and no ImageDiff in it. The config a person means when they
# ask for layout tests is the one they just built, and the build says which that
# was.
#
# Evidence rather than a preference: it is the build's own record, next to the
# build, and an empty answer means "nothing has been built" rather than a guess.
last_built_config() {
    local name="$1"
    ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
      load_target "$(ws_target "$name")" >/dev/null 2>&1
      kv_field "$(wk_ws_dir "$name")/build.status" config 2>/dev/null ) || true
}

# --- generated ssh aliases ---------------------------------------------------
# Written to a file under ~/.ssh/config.d rather than to ~/.ssh/config itself,
# so it can be rewritten freely without ever touching hand-maintained entries.
#
# Rewritten rather than appended, because a VM's address changes on every boot:
# an alias that is only ever added would point at whatever the guest happened
# to get the first time, and Zed would sit there timing out against a stale IP.

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

# ssh_alias_set <name> <hostname> <user>
ssh_alias_set() {
    local name="$1" hostname="$2" user="$3" conf extra
    conf=$(wk_ssh_conf)
    ensure_dir "$(dirname "$conf")" 0700
    ssh_alias_remove "$name"

    # A per-target identity only when there is one. The container workspaces
    # authenticate as the invoking user through the podman machine and have no
    # key of their own.
    extra=""
    [ -n "${4:-}" ] && extra="    IdentityFile $4"

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

# Every registered target, plus container, which is the default and is what
# anything unregistered is. Used by commands that report on everything rather
# than acting on one named workspace.
target_all() {
    local d f t seen=" container "
    echo container

    # On a shared build machine the workspaces were created from somewhere
    # else, so nothing here is registered -- and without this a bare `wk
    # status` on the box would walk one target it does not have and report
    # nothing about the ones it does.
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

    # Every machine that has been configured, whether or not a workspace is
    # currently pinned to it. A machine you have set up is a target you have,
    # and `wk status` should say what is on it -- including on the machine
    # itself, where nothing is registered at all because workspaces are created
    # from the workstation.
    #
    # Two directories, because a target has two halves that belong in different
    # places: the registry in this repository, which every device gets by
    # pulling it, and the machine-local conf, which is this device's own view of
    # the same machine. Both are enumerated, so a machine that is only in the
    # registry is still a target here -- that is the entire point of it.
    # The registry is skipped on a machine that is the far end of a target,
    # and that is not a detail: the whole repository is what gets pushed to a
    # build box, registry included, so without this every delegated `wk ls`
    # there walked the entire fleet and tried to ssh to machines it has no
    # route, host key or reason to reach -- printing "Could not resolve
    # hostname" in the middle of somebody else's listing. A build box answers
    # for itself; it drives nothing. (Its own target was added above, from the
    # marker.) A *peer* is a workstation and does drive things, so it keeps the
    # whole registry.
    # ...and not inside the podman VM either, for the same reason: the whole
    # repository is rsynced in there, so the VM would walk every machine in the
    # registry and pay an ssh timeout each for machines it has no key, route or
    # business reaching. The VM holds the container store; it drives nothing.
    # (Measured 2026-08-19: a forwarded half doing four probes it could never
    # answer, which is most of what a bare `wk status` was waiting for.)
    local me=""
    if ! in_remote_host && [ -z "${WK_IN_VM:-}" ]; then
        me=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')
        for d in "$(target_registry_dir)" "$(target_conf_dir)"; do
            [ -d "$d" ] || continue
            for f in "$d"/*.conf; do
                [ -f "$f" ] || continue
                t=$(basename "$f" .conf)
                case "$seen" in *" $t "*) continue ;; esac
                # ...and never itself. A machine in the registry is in it on
                # every device, including the one it names: without this, a
                # bare `wk status` on the workstation would ssh to itself to
                # ask what it already knows -- and on a machine with no sshd
                # reachable from inside, report itself unreachable.
                [ -n "$me" ] && [ "$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')" = "$me" ] && continue
                seen="$seen$t "
                echo "$t"
            done
        done
    fi
}

# --- named targets -----------------------------------------------------------
# A target name is either one of the four built-in kinds -- container, vm,
# remote, local -- or the name of a machine configured under
# ~/.config/wk/targets/<name>.conf.
#
# The distinction exists because `remote` is the one kind you can have several
# of. There is exactly one podman, one Tart and one local machine, so those
# names identify a driver *and* the thing it drives; a shared build box does
# not, and `wk new bug-238 --target remote` on a laptop with three of them
# configured would be a coin toss. So the machine's own name is the target:
#
#   wk new bug-238 --target devbox-arm64-2
#
# and the conf next to that name says which driver to use and how to reach it.
# Everything downstream keeps working unchanged because the registry already
# records a target per workspace as an opaque string.
# A target is configured in two places, and which one a fact belongs in is the
# whole distinction:
#
#   targets/hosts/<name>.conf   in this repository, and therefore on every
#                               device that pulls it. What is true about the
#                               *machine* no matter who is driving it: the ssh
#                               destination, the CMake flags its toolchain
#                               needs, whether it is a build box or a
#                               workstation of its own.
#   ~/.config/wk/targets/…      this device's own view of the same machine, and
#                               what only it can know: a different ssh alias, a
#                               root somewhere else, a job ceiling for a link
#                               that is not the machine's fault.
#
# The registry exists because the machine-local half was the *only* half, and a
# machine you had configured on one device did not exist on the others: a
# reinstall lost every target, buildbox4's build flags had to be re-typed on
# every command from every device, and `wk status` on the Mac could not say a
# word about the Linux workstation because it had never heard of it. None of
# that is per-device knowledge; it is knowledge about the machine, and it
# belongs where the rest of this tooling lives.
#
# No new machinery, deliberately: the registry is committed files. A device
# gets a new machine by pulling, and `wk remote setup`/`t_sync_tools` already
# push the whole tree to the machines themselves. Adding one is an edit and a
# commit, not a command that writes to git on your behalf.
target_registry_dir() { echo "$WK_ROOT/targets/hosts"; }
target_registry_conf(){ echo "$(target_registry_dir)/$1.conf"; }
target_conf_dir() { echo "${XDG_CONFIG_HOME:-$HOME/.config}/wk/targets"; }
target_conf()     { echo "$(target_conf_dir)/$1.conf"; }

# The driver a target name selects. The built-in kinds are their own kind;
# anything else must have a conf, and a conf that does not say otherwise
# describes a remote machine -- the only kind there can be several of, and so
# the only kind worth naming.
target_kind() {
    case "$1" in
        container|vm|remote|local) echo "$1"; return 0 ;;
    esac
    # Either conf makes the target exist; the local one answers first, because
    # it is the overriding half -- a device that says a machine is something
    # other than what the registry says gets to. A conf that names no kind
    # falls through to the other one, and then to `remote`, which is the only
    # kind worth naming after a machine.
    local c k="" found=""
    for c in "$(target_conf "$1")" "$(target_registry_conf "$1")"; do
        [ -f "$c" ] || continue
        found=1
        [ -n "$k" ] && continue
        k=$(awk -F= '/^[[:space:]]*WK_TARGET_KIND[[:space:]]*=/ {
                gsub(/[ \t"'"'"']/, "", $2); print $2; exit }' "$c")
    done
    [ -n "$found" ] || return 1
    echo "${k:-remote}"
}

# Per-target driver state, cleared before every load.
#
# A driver is a sourced file that sets globals, and more than one target can be
# loaded in a single process -- `wk status` with no argument walks every target
# there is. Without this the second remote machine would inherit the first
# one's host, root and measured capacity, and confidently report its workspaces
# as living somewhere else.
#
# The environment still wins over a conf, so what it supplied is snapshotted
# once, before any conf has been read, and restored on each load. The variables
# are named here rather than in the driver because the reset has to happen
# *before* the conf is sourced, and the conf is what would otherwise be undone.
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
#
#   absent       nothing of it exists anywhere
#   creating     something of it exists, and creation never finished
#   present      it exists and creation finished
#   broken       creation finished and the environment is gone -- something
#                outside wk removed it (rule 5)
#   unreachable  the machine that holds it did not answer in time
#
# Derived on every call from evidence next to the artifacts, and stored nowhere.
# Three pieces of evidence, in this order:
#
#   the driver's own answer about the environment (t_info), which is where
#   `creating` and `unreachable` come from -- a driver knows whether its
#   completion marker is next to its artifacts, and whether it could ask;
#
#   the `base-id` file, for a target that pins a base snapshot: the container
#   driver writes it as the last act of creation, so a workspace whose
#   container exists without one was interrupted (and re-pinning it over a
#   surviving `changes/` layer is undefined behaviour with the overlay);
#
#   t_created, when the environment is absent but a workspace directory is
#   here: with the marker, the environment was removed out from under a
#   finished workspace, which is a different problem with a different repair
#   than a creation that never got that far.
#
# `ws.status` is not evidence and is never believed about a workspace that
# exists: it is a claim by the process that drove the creation. It is read in
# exactly two places, both of which are about that process rather than about
# the workspace -- whether it is still alive (wait_ready, `wk status`), and,
# for a target whose marker lives on a far side that has since been emptied,
# whether it ever said it finished. That second one is rule 5 by definition:
# `broken` *is* the record and the machine disagreeing, so deciding it without
# reading the record is not possible.
#
# The registry is not consulted either: it is a cache of which target a
# workspace was created with, and a workspace that predates it -- every
# container workspace on the Linux workstation does -- must not be read as
# half-made.
ws_state() {
    local name="$1" env ws
    env=$(t_info "$name" 2>/dev/null || echo absent)
    ws=$(wk_ws_dir "$name")

    case "$env" in
        creating|unreachable) echo "$env"; return 0 ;;
    esac

    if [ "$env" = absent ]; then
        [ -d "$ws" ] || { echo absent; return 0; }
        # A ws directory with no environment behind it. Which of the two things
        # that is, the marker decides: with it, this was a finished workspace
        # and something removed the environment by hand; without it, it is the
        # near-side half of a creation that never got to the far side, or the
        # leftovers of a `wk rm` that was killed -- rubble, and rule 3 says
        # remake.
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

# The word `wk ls` and `wk status` print in their STATE column: the driver's
# own answer (running, exited, present), except that a workspace whose
# lifecycle is not `present` is reported as that instead of as whatever the
# environment half of it happens to be. A container that exists inside a
# workspace that was never finished is not a workspace that is running -- and
# two commands disagreeing about that is exactly what the rules forbid.
ws_display_state() {
    local st; st=$(ws_state "$1")
    case "$st" in
        present) t_info "$1" ;;
        *)       echo "$st" ;;
    esac
}

# What the process that created this workspace claimed, while it was doing so,
# and the transcript of the attempt. A claim, never the record -- see ws_state.
#
# Beside the workspace directory rather than inside it, which is not tidiness:
# a re-run of `wk new` over a half-made workspace *destroys* that directory
# first (rule 3), and the log the driver is writing to at that moment must not
# be the file it deletes -- the fd would survive, pointing at nothing, and the
# words explaining what happened would be lost exactly when they are wanted.
# They are removed by `wk rm`, with the workspace they describe.
ws_state_dir()   { echo "$WK_STORE/create"; }
ws_status_file() { echo "$(ws_state_dir)/$1.status"; }
ws_create_log()  { echo "$(ws_state_dir)/$1.log"; }

# Where the command that remakes a workspace has to be typed.
#
# `wk new` is a workstation command: a machine that only *hosts* workspaces
# refuses it (is_lifecycle in `wk`), because the record of which target a
# workspace belongs to lives on the workstation. So printing it bare on a build
# box sends somebody straight to a refusal -- which is what the readiness
# refusal did, reported 2026-08-19 from a shell on devbox-arm64-2.
ws_remake_hint() {
    if in_remote_host; then
        printf 'from the workstation:  wk new %s --target %s' "$1" "$(wk_remote_field target)"
    else
        printf 'wk new %s%s' "$1" "${WK_TARGET:+ --target $WK_TARGET}"
    fi
}

# --- one gate: wait_ready ----------------------------------------------------
#
# Block until this workspace is usable, and be honest about every other
# outcome. The one place anything asks "may I act on this yet", so that zed,
# a build and the babysitter cannot each answer it differently:
#
#   present      return, immediately in the normal case
#   creating     wait -- and say what it is waiting for, once
#   creating,    the driver died with the connection that started it: say so
#   driver dead  and name the one command that fixes it (`wk new`, which
#                remakes from scratch -- rule 3, not repair)
#   broken       refuse, and name the repair
#   unreachable  refuse, with the timeout that decided it -- never a hang
#   absent       refuse
#
# Foreground by design. If this waiter is killed, only the waiting stops:
# creation is detached and continues, and re-running the same command is the
# resume. A waiter that detached itself and opened an editor afterwards would
# be opening windows into a session that had gone.
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
                # A barrier rather than a refusal, and the distinction is the
                # one lib/common.sh draws: this command *can* proceed. A clone
                # that finished and never got its marker -- a driver killed one
                # step from the end, a wk-tools update mid-run -- looks exactly
                # like one cut in the middle, and only the person looking at it
                # can tell which. Reported 2026-08-19: `wk claude db --force`
                # on the build machine, where the workspace was complete, the
                # refusal was absolute, and the repair it named is a command
                # that machine does not accept.
                #
                # Under --force this returns and the caller acts on the
                # workspace as it is; the warning is repeated when the command
                # ends, which is what --force buys everywhere else too.
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

# The targets a report covers, and the one place that question is answered.
#
# `wk ls` and `wk status` both claim to list "the workspaces", so they have to
# enumerate the same thing: same target list here, same per-target walk in
# target_workspaces below. They did not -- a bare `wk status` walked every
# target while `wk ls` on a macOS host was forwarded into the podman VM and
# answered for containers alone, so a vm or remote workspace appeared in one
# listing and not the other.
#
# An explicit WK_TARGET (which may name several, space separated -- that is how
# the macOS dispatcher hands over the host-side half) wins; inside a workspace
# there is exactly one target and it is this machine.
# --- one round of probes instead of N ----------------------------------------
#
# A bare `wk status` walks every target there is, and for a machine reached over
# ssh the first question costs a full connect -- ten seconds (WK_SSH_TIMEOUT)
# when the machine is off, off this network, or behind a jump host that is. That
# is per machine and it is serial, so the shared registry made it worse by
# exactly the amount it made `wk status` more useful: every device now knows
# every machine, and the ones not on this network are the norm rather than the
# exception. Four machines, two of them away, meant twenty seconds before the
# first row.
#
# So the waiting is done once, for all of them, before the walk: one subshell
# per target asks its own machine and writes the answer where the serial pass
# will find it (t_prefetch, and _remote_probe_file in targets/remote.sh). The
# report itself is untouched -- same order, same streams, same exit status --
# because nothing about *printing* was the problem. What the walk finds is a
# machine that has already answered, or one already known not to have.
#
# Two things fall out of it for free: the ssh multiplexing socket is warm by
# the time the walk asks its real questions (ControlPersist, targets/remote.sh),
# and a machine that is down is down *once* rather than once per question.
#
# Best-effort throughout. A prefetch that fails leaves no file and the walk asks
# the machine itself, exactly as it did before this existed.
prefetch_targets() {
    local t
    [ $# -gt 0 ] || return 0
    command -v mktemp >/dev/null 2>&1 || return 0
    WK_PREFETCH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/wk-probe.XXXXXX" 2>/dev/null) || return 0
    export WK_PREFETCH_DIR
    for t in "$@"; do
        # A subshell, so each target's driver load and its globals stay its own
        # -- the same reason load_target resets them (see _target_reset_vars).
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

    # WK_NO_DELEGATE: this walk is one machine's contribution to somebody
    # else's fleet listing, so it reports what this machine holds and asks
    # nobody. Without it a workstation asked by another workstation would go on
    # to ask every machine *it* knows -- and since the registry is shared, that
    # is the same set: every build box listed once per workstation, and the ssh
    # work squared.
    #
    # A machine's own target survives it: on a build box the target named after
    # that machine *is* this machine (WK_REMOTE_LOCAL, set in the conf
    # `wk remote setup` leaves there), and dropping it would drop every
    # workspace the box holds. Decided from the conf, so this costs no ssh.
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

# Every workspace a loaded target has, from both sides of it.
#
# The local store knows what this machine created -- what a workspace is pinned
# to, its build log -- and the driver knows what actually exists over there.
# Usually the same set, and not always: a remote target's workspaces live on
# the far machine, so `wk ls` run *on* that machine has an empty store and a
# full ws directory, and a workspace deleted out from under the store has the
# opposite. Listing only one side is how a machine ends up reporting "no
# workspaces" while holding six.
target_workspaces() {
    { list_workspaces 2>/dev/null || true
      t_list 2>/dev/null | cut -f1 || true
    } | grep -v '^[[:space:]]*$' | sort -u
}

load_target() {
    local t="${1:-container}" kind conf

    # $WK_STORE is target-dependent -- a vm workspace keeps its state under
    # XDG state on the host, a container's lives in /var/lib/wk -- and drivers
    # set it when they are sourced. Reset it first so loading a second target
    # in one process does not inherit the first one's store.
    : "${WK_STORE_DEFAULT:=${WK_STORE:-/var/lib/wk}}"
    WK_STORE="$WK_STORE_DEFAULT"

    kind=$(target_kind "$t") || die "unknown target '$t'.
    The built-in ones are container, vm, remote and local. Anything else is a
    machine, and needs a conf -- in the registry, so every device gets it:

        $(target_registry_conf "$t")
            WK_REMOTE_HOST=$t      # an ssh destination that already works
            WK_REMOTE_ROOT=/home/you/wk

    or in $(target_conf "$t") for this device alone."

    # Set before either file is read: a driver that can have several instances
    # has to know which one it is, and both the conf and the driver's own
    # defaults are written in terms of the name.
    #
    # WK_TARGET is the name, WK_TARGET_KIND is the driver behind it. Commands
    # that branch on what kind of thing they are talking to must use the kind:
    # `[ "$TARGET" = remote ]` was true for every remote target while there was
    # only one possible name for one.
    WK_TARGET="$t"
    WK_TARGET_KIND="$kind"
    export WK_TARGET WK_TARGET_KIND

    _target_reset_vars

    # And the *functions*, which is the other half of the same problem this
    # file already warns about for variables. A driver overrides the hooks it
    # needs and a function definition outlives the load, so the first driver's
    # override was still live when the second target was loaded: measured
    # 2026-08-19, `wk status` on a build machine walked container first, and
    # every remote workspace's branch was then read by the container driver's
    # t_branch -- which looks for an overlay that is not there and answered
    # `-` for all of them, plausibly and wrongly.
    #
    # Re-sourcing this file restores every default before the driver replaces
    # what it means to replace. It is only function definitions; the running
    # load_target keeps executing the body it was called with.
    # shellcheck disable=SC1090
    . "$WK_ROOT/lib/target.sh"

    # The confs first and the driver second, so that the driver's own
    # ${VAR:-default} assignments fill in only what the confs left unsaid.
    #
    # Registry, then local: last assignment wins in a sourced file, so the
    # device's own conf overrides the shared one line by line rather than all
    # or nothing. A local conf that sets one variable keeps the registry's
    # answer for every other.
    for conf in "$(target_registry_conf "$t")" "$(target_conf "$t")"; do
        [ -f "$conf" ] || continue
        # shellcheck disable=SC1090
        . "$conf"
    done
    # shellcheck disable=SC1090
    . "$WK_ROOT/targets/$kind.sh"
}
