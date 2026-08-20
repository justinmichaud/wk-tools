# Target driver: a shared, multi-user build machine.
#
# No containers here. These boxes are other people's build machines too, and
# they typically have no rootless podman, no overlay support and no appetite
# for a stranger's storage tree. So a workspace is a plain checkout under your
# own home directory.
#
# The design consequence is that isolation is gone -- there is no firewall, no
# read-only base and no disposable layer. Anything running here is trusted, and
# `wk claude` deliberately refuses to run against a remote target, as does
# `wk verify`, which measures a boundary this target has never claimed.
#
# What matters instead is being a good guest. Every build here is sized from
# the *remote* machine's load and free memory, niced to the floor, and
# serialised against other wk builds by the same user.
#
# This is also the only target you can have several of, which is why one is
# named after the machine rather than after the driver:
#
#   wk new bug-238 --target devbox-arm64-2
#
# with ~/.config/wk/targets/devbox-arm64-2.conf holding whatever differs from
# the defaults below:
#
#   WK_REMOTE_HOST=devbox-arm64-2  # ssh destination; defaults to the target name
#   WK_REMOTE_ROOT=/home/you/wk    # defaults to ~/wk on the box
#   WK_TARGET_CMAKE=-DFOO=ON       # extra CMake flags for builds on this machine
#   WK_REMOTE_REFERENCE=/var/...   # a shared checkout to clone from; see below
#   WK_REMOTE_LOCAL=1              # this *is* the machine; run without ssh
#   WK_REMOTE_PEER=1               # a workstation of its own, not a build box
#   WK_REMOTE_TOOLS=Development/…  # its wk-tools, if not $root/tools
#
# ...or, for a machine every device should know about, in the shared registry
# instead: targets/hosts/<name>.conf, which is in this repository (see
# target_registry_conf in lib/target.sh).
#
# --- peers -------------------------------------------------------------------
#
# WK_REMOTE_PEER marks the one case that is not a build box: another
# *workstation*. The Linux workstation and this Mac are both machines that own
# workspaces -- containers or guests, a store, a mirror, a git checkout of this
# repository -- and neither is the far end of the other. Before this, the only
# way to make one visible from the other was to provision it as a build box,
# which would have been actively wrong: `wk remote setup` writes ~/.wk-remote,
# and that marker makes the machine refuse `wk sync`, `wk gc` and `wk new` *on
# itself* (`wk` dispatch, in_remote_host). A workstation cannot be told it is
# somebody's build box without ceasing to be a workstation.
#
# So a peer is a target that can be *asked* and not driven:
#
#   asked    t_has_wk is true without the marker, so `wk status` and `wk ls`
#            delegate to the machine's own wk and report what it says. That is
#            the whole reason peers exist: `wk status` on the Mac said nothing
#            about the Linux box, because it had never heard of it.
#   not driven  no tooling is pushed (it has its own checkout, kept by git),
#            and creation and destruction are refused -- a workstation's
#            workspaces are containers and guests, which this driver knows
#            nothing about and would replace with a plain checkout.
#
# The bare name `remote` still works for a one-off, and then WK_REMOTE_HOST
# has to come from the environment -- there is no machine name to infer it
# from.
#
# `wk remote setup <target>` provisions a machine for all of this, and needs no
# root on it. Nothing here ever does: a shared box is somebody else's, and a
# tool that wants sudo on it is a tool that does not get used.

# The ssh destination defaults to the target's own name, because a machine you
# can already reach is a machine that already has an entry in ~/.ssh/config --
# with whatever ProxyJump, user and key it needs. Re-stating that here would be
# a second place to keep it right. `remote` is excluded: it names the driver,
# not a host.
if [ -z "${WK_REMOTE_HOST:-}" ] && [ "${WK_TARGET:-remote}" != remote ]; then
    WK_REMOTE_HOST="$WK_TARGET"
fi
WK_REMOTE_HOST="${WK_REMOTE_HOST:-}"

# Left empty on purpose: the default is $HOME/wk *on the remote box*, and this
# side does not know what that expands to. _remote_probe resolves it, once.
WK_REMOTE_ROOT="${WK_REMOTE_ROOT:-}"

# A ceiling on the job count, on top of the polite calculation. Not a
# performance knob -- the point is that a 96-core machine shared by six people
# should not hand any one of them 48 jobs just because the load average
# happened to be low in the second we looked.
# No job ceiling of its own. It had one -- WK_REMOTE_MAX_JOBS, 16 by default --
# and a fixed number is exactly the thing this repo's resource policy says not
# to have: it was too small on a 250 GB build box and would be too large on a
# small one, and it went stale the moment the machine changed. The job count is
# derived per build from what that machine has free *at the time*
# (_remote_probe reads MemAvailable and the load average on every invocation),
# which is the number that decides whether a link step survives.
#
# WK_MAX_JOBS is still honoured if something in the environment sets it, as a
# deliberate one-off; nothing configures it any more.

# Host-side state, per machine. $WK_STORE defaults to /var/lib/wk, which is
# right inside the podman VM and wrong on a workstation driving a build
# somewhere else; and it has to be per target, because two remote machines can
# each have a workspace of the same name and `wk status` walks the store of
# every target it knows.
#
# What lives here is only what this side produces: the build log and the build
# status. The checkout, the build tree and the ccache are all on the far end,
# where they belong.
# On the machine itself the state *is* the workspace directory: `$root/ws/<name>`
# already holds the checkout, and putting build.log and build.status beside it
# is what makes one copy canonical no matter which end started the build.
if [ -n "${WK_REMOTE_LOCAL:-}" ] && [ -n "${WK_REMOTE_ROOT:-}" ]; then
    WK_STORE="${WK_REMOTE_STORE:-$WK_REMOTE_ROOT}"
else
    WK_STORE="${WK_REMOTE_STORE:-$(wk_state_dir)/remote/${WK_TARGET:-remote}}"
fi

# Whether this process is running *on* the target. `wk remote setup` writes
# WK_REMOTE_LOCAL=1 into the conf it leaves on the machine itself, so the same
# driver drives the same workspaces from either end -- from the workstation
# over ssh, and from a shell on the box with no ssh at all. One code path, so
# the two can never answer differently about where a checkout is or how many
# jobs a build gets.
_remote_is_local() { [ -n "${WK_REMOTE_LOCAL:-}" ]; }

# Another workstation rather than a build box -- see "peers" at the top.
_remote_peer() { [ -n "${WK_REMOTE_PEER:-}" ]; }

_remote_require() {
    _remote_is_local && return 0
    [ -n "$WK_REMOTE_HOST" ] || die "target '${WK_TARGET:-remote}' has no host to reach.
    Set WK_REMOTE_HOST in $(target_conf "${WK_TARGET:-remote}"), or name the
    target after a machine your ~/.ssh/config already knows:
        wk new <name> --target devbox-arm64-2"
}

# ssh, multiplexed and never interactive. A remote target answers `t_info`,
# `t_src`, `t_tools` and the capacity probe through this, several times per
# command, and every one of those is a full handshake through a jump host
# without a shared connection -- seconds each, on a link where the work itself
# is a single build. The socket expires by itself, so nothing has to clean it
# up.
#
# ConnectTimeout because `wk status` walks every target it knows: a machine
# that is off, or off this network, must cost ten seconds and not a TCP
# timeout. BatchMode because none of this may ever stop to ask for a
# passphrase -- a prompt in the middle of `wk build` is a hang with a cursor.
_ssh_opts() {
    local d; d="$(wk_state_dir)/ssh"
    mkdir -p "$d" 2>/dev/null || true
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=${WK_SSH_TIMEOUT:-10} -o ControlMaster=auto -o ControlPath=$d/%h-%p-%r -o ControlPersist=60"
}

_rsh() {
    _remote_require
    # On the machine itself there is nothing to connect to, and trying would
    # need an sshd loop and a key to itself. Same shell, same script text.
    if _remote_is_local; then
        bash -c "$*"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts) "$WK_REMOTE_HOST" "$@"
}

# ssh for *questions* -- everything that asks the machine something rather than
# handing it work. `-n`, so it can never read this side's stdin.
#
# That is not a precaution, it is a fix. ssh reads stdin eagerly and forwards
# it whether or not the remote command wants it, and these run inside command
# substitutions that inherit whatever stdin their caller had. Two ways it bit:
# the build status arrived on the machine as an empty file, because resolving
# the destination path in the same pipeline drank the content; and a `du` in
# `wk remote setup`'s cleanup loop could swallow the answer being typed at the
# confirmation prompt right after it.
#
# t_exec, t_exec_tty, t_wk and t_state_put deliberately do *not* use it: a
# command run in the workspace may legitimately be fed something. A *build* is
# not one of those -- it reads nothing, and `run_watched` puts it in the
# background, where an ssh reaching for the terminal earns a SIGTTIN.
_rsh_q() {
    _remote_require
    if _remote_is_local; then
        bash -c "$*" </dev/null
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -n $(_ssh_opts) "$WK_REMOTE_HOST" "$@"
}

# One round trip, cached for the life of this process: the remote home (which
# is what $WK_REMOTE_ROOT defaults to), the core count, the load average and
# the memory actually free.
#
# Everything that sizes a build here comes from this and from nothing local.
# The machine driving the build may be a laptop with 8 cores and the build may
# be running on 80; lib/resources.sh measures the machine it runs on, which for
# every other target is the right one and for this one is never.
# The two halves are split because reporting and working want different
# answers to "the machine did not reply". A build cannot proceed without the
# numbers and says so with the ssh command to try; `wk status` must never die
# over an unreachable machine -- the core requirement is that it reports
# unreachable, with the timeout that decided it, and carries on with the rest
# of the fleet.
#
# The failure is remembered for the life of this process, exactly as the
# success is: `wk status` asks about every workspace on the machine, and one
# ConnectTimeout per question would turn a listing into a minute of waiting.
# The four questions, as one shell snippet, because two callers ask them: the
# probe below and the parallel prefetch further down. One copy, or the prefetch
# fills the cache with fields the reader parses differently.
_remote_probe_cmd() {
    printf '%s' 'echo "$HOME"; nproc; awk "{print int(\$1)}" /proc/loadavg;
                 awk "/^MemAvailable:/ {print int(\$2/1024)}" /proc/meminfo'
}

# Where a prefetched answer for *this* target would be, if a command asked for
# one (prefetch_targets, lib/target.sh). Empty when nothing did.
#
# A file rather than an inherited variable because the prefetch happens in a
# subshell per target -- that is what makes it parallel -- and a subshell cannot
# hand a value back to its parent. It lives for the length of one command and is
# removed with it: this is the same per-process memo _WK_REMOTE_PROBED already
# is, not a cache of a fact that outlives the asking (docs/HANDOFF-workspace-state.md).
_remote_probe_file() {
    [ -n "${WK_PREFETCH_DIR:-}" ] || return 0
    printf '%s/%s.probe' "$WK_PREFETCH_DIR" "${WK_TARGET:-remote}"
}

# Asks the machine, in parallel with every other target, and writes what it
# says. An *empty* file means asked-and-did-not-answer, which is a real answer
# and the one worth prefetching: it is the ten-second one.
t_prefetch() {
    local f out
    f=$(_remote_probe_file) || return 0
    [ -n "$f" ] || return 0
    _remote_is_local && return 0
    [ -n "${WK_REMOTE_HOST:-}" ] || return 0
    out=$(_rsh_q "$(_remote_probe_cmd)" 2>/dev/null) || out=""
    printf '%s' "$out" > "$f.tmp.$$" && mv "$f.tmp.$$" "$f"
}

_remote_probe_try() {
    [ -n "${_WK_REMOTE_PROBED:-}" ] && return 0
    [ -n "${_WK_REMOTE_DOWN:-}" ] && return 1
    _remote_require
    local out f
    # A prefetched answer if one was taken for this command, and the ssh
    # otherwise. Same question, same parsing; the only difference is who waited.
    f=$(_remote_probe_file) || f=""
    if [ -n "$f" ] && [ -f "$f" ]; then
        out=$(cat "$f")
        if [ -z "$out" ]; then
            _WK_REMOTE_DOWN=1
            return 1
        fi
    elif ! out=$(_rsh_q "$(_remote_probe_cmd)"); then
        _WK_REMOTE_DOWN=1
        return 1
    fi

    _WK_REMOTE_HOME=$(printf '%s\n' "$out" | sed -n 1p)
    _WK_REMOTE_CORES=$(printf '%s\n' "$out" | sed -n 2p)
    _WK_REMOTE_LOAD=$(printf '%s\n' "$out" | sed -n 3p)
    _WK_REMOTE_MEM=$(printf '%s\n' "$out" | sed -n 4p)

    [ -n "$WK_REMOTE_ROOT" ] || WK_REMOTE_ROOT="$_WK_REMOTE_HOME/wk"
    _WK_REMOTE_PROBED=1
}

_remote_probe() {
    _remote_probe_try || die "cannot reach '$WK_REMOTE_HOST' over ssh (${WK_SSH_TIMEOUT:-10}s).
    This target has no way in but ssh, and it is not interactive: the key,
    the ProxyJump and the host entry all have to work non-interactively.
    Try:  ssh -o BatchMode=yes $WK_REMOTE_HOST true"
}

# A shared WebKit repository on the machine, to clone workspaces from.
#
# Igalia's build boxes carry one and say so in the MOTD -- buildbox4's reads
# "instead of cloning the git repo from webkit.org, clone the one from
# /var/git/WebKit.git (is updated every 10 minutes)", and notes that a local
# clone hardlinks `.git/objects` and so costs almost nothing. Since the same
# MOTD asks everyone to keep disk use down, and our own mirror is 13 GB, using
# theirs is not a micro-optimisation: it is the difference between following
# the house rules on a shared machine and not.
#
# Advertised or configured, and nothing else. A path that merely exists is
# somebody's checkout, not an invitation; a path in the MOTD is the sysadmins
# telling every user to clone it. WK_REMOTE_REFERENCE overrides, for a machine
# that has one and does not announce it.
#
# Verified before use, because buildbox4 advertises a path that is not there --
# the MOTD outlived the repository. An unchecked hint would turn every `wk new`
# into a confusing clone failure.
_remote_reference() {
    [ -n "${_WK_REMOTE_REF_PROBED:-}" ] && { printf '%s' "$WK_REMOTE_REFERENCE"; return 0; }
    _WK_REMOTE_REF_PROBED=1

    if [ -n "${WK_REMOTE_REFERENCE:-}" ]; then
        printf '%s' "$WK_REMOTE_REFERENCE"
        return 0
    fi

    # Static MOTD files only: /etc/update-motd.d holds scripts, and running
    # somebody else's scripts to read a hint is not a trade worth making.
    WK_REMOTE_REFERENCE=$(_rsh_q '
        cat /etc/motd /etc/motd.d/* /run/motd.dynamic 2>/dev/null \
        | grep -oE "/[A-Za-z0-9._/-]*[Ww]eb[Kk]it(\.git)?" | sort -u \
        | while read -r p; do
              git -C "$p" rev-parse --verify -q refs/heads/main >/dev/null 2>&1 || continue
              echo "$p"; break
          done' 2>/dev/null) || WK_REMOTE_REFERENCE=""

    printf '%s' "$WK_REMOTE_REFERENCE"
}

# The mirror this driver keeps when the machine has no shared repository of its
# own, and the fetch into it.
#
# Both halves live here because both callers need both: `t_create` clones a new
# workspace from it, and `t_sync` refreshes it -- and a sync that assumed the
# mirror already existed would fail on the machine that has never made one,
# which is exactly the machine somebody is most likely to sync first.
#
# Only main is mirrored, matching wk_mirror_branches: WebKit has ~920 branches
# and the ones that are not main are not what a workspace starts from. Anything
# else is still one `git fetch` away inside the workspace.
#
# gc.auto is off for the same reason it is off in the local mirror: the
# workspaces borrow its objects through --shared, and a repack underneath a
# live clone is how that arrangement breaks.
_remote_mirror_update() {
    local root="$1"
    info "updating the WebKit mirror on $WK_REMOTE_HOST (first run clones it)"
    _rsh_q "set -e
        mkdir -p $(sh_quote "$root/ws") $(sh_quote "$root/cache/ccache")
        M=$(sh_quote "$root/mirror")
        if [ ! -d \"\$M\" ]; then
            git init --bare -q \"\$M\"
            git -C \"\$M\" config gc.auto 0
            git -C \"\$M\" remote add origin https://github.com/WebKit/WebKit.git
            git -C \"\$M\" config --add remote.origin.fetch '+refs/heads/main:refs/heads/main'
        fi
        git -C \"\$M\" fetch --prune -q origin" \
        || die "could not update the WebKit mirror on $WK_REMOTE_HOST"
}

# $WK_REMOTE_ROOT, resolved. Every path function goes through this rather than
# reading the variable, because the default is only known after the probe.
_remote_root() { _remote_probe; printf '%s' "$WK_REMOTE_ROOT"; }

_remote_ws()   { echo "$(_remote_root)/ws/$1"; }

# --- contract ----------------------------------------------------------------

t_src()   { echo "$(_remote_ws "$1")/WebKit"; }

# The remote's own ccache, under the remote root. Deliberately not a shared one
# somewhere on the box: a cache you do not administer is a cache you can poison
# for other people, and a good way to become unpopular.
t_ccache_dir() { echo "$(_remote_root)/cache/ccache"; }

# The remote $HOME, from the probe that already asked for it.
_remote_home() { _remote_probe; printf '%s' "$_WK_REMOTE_HOME"; }

# wk-tools is pushed to the remote root rather than per workspace: it is the
# same tree for all of them, and `wk build` re-rsyncs it on every run.
#
# WK_REMOTE_TOOLS overrides, which is how a peer is reached: its wk-tools is a
# git checkout it maintains itself, wherever that machine keeps it. A relative
# path is relative to the *remote* home -- the conf is sourced on this side, so
# writing $HOME in it would expand to the wrong machine's home, silently and
# plausibly.
t_tools() {
    case "${WK_REMOTE_TOOLS:-}" in
        "") echo "$(_remote_root)/tools" ;;
        /*) printf '%s' "$WK_REMOTE_TOOLS" ;;
        *)  printf '%s/%s' "$(_remote_home)" "$WK_REMOTE_TOOLS" ;;
    esac
}

# There are no base snapshots here. The overlay scheme is a local-store
# concept; the equivalent on the far end is the git mirror, which t_create
# maintains itself.
t_needs_base() { return 1; }

# The ssh destination is the one already configured -- not a generated wk-<name>
# alias. A generated alias could not carry the ProxyJump the real entry has,
# and a second entry pointing at the same machine is a second thing to keep
# right.
# Non-zero rather than fatal when there is none, which is the contract the vm
# driver already follows for a guest that is not running: `wk enter --zed`
# treats it as "no route", and `wk new` simply leaves the line out. On the
# machine itself there is no route and never will be -- it would be an ssh loop
# back to the host you are typing on.
t_ssh_host() {
    _remote_is_local && return 1
    _remote_require; echo "$WK_REMOTE_HOST"
}

t_store_init() {
    ensure_dir "$WK_STORE"
    ensure_dir "$WK_STORE/ws"
}

t_list() {
    # `|| true`: no ws directory yet is not an error, and under `pipefail` in
    # the caller a non-zero ls would fail the whole listing.
    { _rsh_q "ls -1 $(sh_quote "$(_remote_root)/ws") 2>/dev/null" 2>/dev/null || true; } \
        | while read -r n; do [ -n "$n" ] && printf '%s\tpresent\n' "$n"; done
}

# The whole lifecycle in one round trip, because every extra one is a
# handshake through a jump host and `wk status` asks per workspace:
#
#   no workspace directory          absent
#   directory, no `.wk-ready`       creating -- a clone that never finished,
#                                   or one whose ssh was cut mid-way
#   `.wk-ready`                     present
#   the machine did not answer      unreachable, never absent
#
# The marker is the point of the exercise. Before it, a workspace whose clone
# died half-way had a directory and a partial checkout, `t_info` called it
# present, `wk new` refused it as "already exists" and `wk build` built the
# rubble -- and none of that was visible from the machine itself, which is
# where the marker now is.
t_info() {
    local ws out
    _remote_probe_try || { echo unreachable; return 0; }
    ws=$(_remote_ws "$1")
    out=$(_rsh_q "if [ ! -d $(sh_quote "$ws") ]; then echo absent;
                  elif [ -f $(sh_quote "$ws/$WK_READY_MARKER") ]; then echo present;
                  else echo creating; fi" 2>/dev/null) || out=unreachable
    printf '%s\n' "${out:-unreachable}"
}

# The far side's marker, which is the only copy of this fact. Asked through
# t_info so there is one round trip and one place that knows where the marker
# lives.
t_created() { [ "$(t_info "$1")" = present ]; }

t_create() {
    local name="$1" root ws ref
    _remote_peer && die "'$WK_REMOTE_HOST' is a workstation, not a build machine for this one.
    Its workspaces are its own -- containers or guests, from its own store --
    and this driver would make a plain checkout under ~/wk instead. Create it
    there:  ssh $WK_REMOTE_HOST wk new $name"
    _remote_probe
    root=$(_remote_root)
    ws=$(_remote_ws "$name")

    # A finished workspace is somebody's work and is refused by name. A
    # half-made one is not: `wk new` has already destroyed it before getting
    # here (rule 3, wipe over repair), so reaching this with anything but
    # absent means the record and the machine disagree about something this
    # driver cannot resolve on its own.
    case "$(t_info "$name")" in
        absent) ;;
        creating) die "'$name' on $WK_REMOTE_HOST is a checkout that never finished being
    made, and destroying it did not take. Remove it by hand and try again:
        ssh $WK_REMOTE_HOST rm -rf $(sh_quote "$(_remote_ws "$name")")" ;;
        unreachable) die "cannot reach $WK_REMOTE_HOST to create '$name'" ;;
        *) die "workspace '$name' already exists on $WK_REMOTE_HOST" ;;
    esac

    ref=$(_remote_reference)

    if [ -n "$ref" ]; then
        # The machine's own shared repository, advertised in its MOTD. A plain
        # local clone, which git makes with hardlinks -- so this costs the
        # working tree and essentially no object storage, and unlike --shared
        # it does not leave the workspace depending on a repository the
        # sysadmins repack on a schedule. That is the whole point of using
        # theirs: no 13 GB mirror of our own on a machine whose MOTD asks
        # everyone to keep disk use down.
        #
        # The clone comes from it, and then origin is re-pointed at
        # WebKit/WebKit like everywhere else -- the machine's copy stays as
        # `shared`, which is what a fast, ten-minutes-fresh local mirror should
        # be called. It was origin until 2026-08-19, which made `git log
        # origin/main` in a remote workspace answer for that box's last fetch
        # rather than for upstream.
        info "cloning from $ref (this machine's shared WebKit, hardlinked)"
        _rsh_q "set -e
            mkdir -p $(sh_quote "$root/ws") $(sh_quote "$root/cache/ccache")
            git clone --quiet -b main $(sh_quote "$ref") $(sh_quote "$ws/WebKit")" \
            || die "could not clone $ref on $WK_REMOTE_HOST"
        _remote_wire "$ws/WebKit"
    else
        # No shared repository, so we keep one of our own and clone from it
        # with --shared: a checkout and no objects at all. Both halves of that
        # -- keeping it and fetching into it -- are _remote_mirror_update, which
        # `t_sync` is the other caller of.
        _remote_mirror_update "$root"
        _rsh_q "git clone --quiet --shared -b main $(sh_quote "$root/mirror") \
                          $(sh_quote "$ws/WebKit")" \
            || die "could not create the checkout on $WK_REMOTE_HOST"
        # Same wiring as every other target: origin is WebKit/WebKit, the forks
        # are here, and the local mirror keeps a name of its own -- the
        # workspace borrows its objects through --shared either way.
        _remote_wire "$ws/WebKit"
    fi

    # ccache's own default ceiling is 5 GB, which a couple of WebKit builds
    # blow through; recorded in the cache's config so `ccache -s` on the box
    # reports the real limit too.
    _rsh_q "printf 'max_size = %s\n' $(sh_quote "${WK_CCACHE_MAXSIZE:-40G}") \
          > $(sh_quote "$root/cache/ccache/ccache.conf")" || true

    ensure_dir "$(wk_ws_dir "$name")"

    # Last, on the far side, and that is the whole point: this file is what
    # says the clone, the wiring and the ccache config all happened. Written
    # over there rather than here so that it survives this end going away --
    # an ssh cut mid-clone leaves no marker and the workspace reads `creating`
    # from any machine that asks, including the box itself.
    _rsh_q "touch $(sh_quote "$ws/$WK_READY_MARKER")" \
        || die "could not mark '$name' ready on $WK_REMOTE_HOST -- treat it as half-made
    and re-run 'wk new $name --target ${WK_TARGET:-remote}'"
    info "remote workspace '$name' created on $WK_REMOTE_HOST ($ws)"
}

t_exec() {
    local name="$1"; shift
    _rsh "cd $(sh_quote "$(t_src "$name")") && $(sh_quote "$@")"
}

# One file out of the machine, byte for byte (lib/target.sh, t_pull).
t_pull() {
    local name="$1" src="$2" dest="$3"
    if _remote_is_local; then cp -f "$src" "$dest"; return; fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$WK_REMOTE_HOST:$src" "$dest"
}

t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    local ex=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --exclude) ex+=("--exclude" "${2:-}"); shift 2 ;;
            *) die "t_pull_dir: unknown option $1" ;;
        esac
    done
    mkdir -p "$dest"
    if _remote_is_local; then
        rsync -a --delete ${ex[@]+"${ex[@]}"} "$src/" "$dest/"; return
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    rsync -a --delete ${ex[@]+"${ex[@]}"} -e "ssh $(_ssh_opts)" \
        "$WK_REMOTE_HOST:$src/" "$dest/"
}

# The build, and only the build, is serialised: two of your own builds must not
# stack on a machine you are already sharing with other people, and putting the
# lock on t_exec instead would silently block every `wk run` and every one-line
# probe behind an hour-long build.
#
# The lock is taken *on the machine that builds*, by lib/lockrun.sh in the copy
# of wk-tools that t_sync_tools has just pushed there. It cannot be taken here:
# a lock dies with its holder, and the holder that matters is the build, not
# the ssh session -- which a detached build outlives by hours.
#
# It was a `flock` on a file under the remote root, and stopped being one for
# the reason lib/common.sh gives: the descriptor is inherited, so anything the
# build leaves running holds the machine's build lock for as long as it lives.
# The lock is now this repo's one mechanism on both ends, which also means one
# fewer thing a shared machine has to have installed.
#
# nice and ionice are here as well as in build-in-target.sh: this end knows the
# target is shared, and the lock has to be outside them either way.
t_exec_build() {
    local name="$1"; shift
    local log tee_to
    log="$(_remote_ws "$name")/build.log"

    # Nothing to tee into on the machine itself: $WK_STORE is the remote root
    # there, so run_watched is already writing this exact file. Two writers on
    # one log interleave, and the result reads like a corrupted build.
    tee_to=" 2>&1 | tee $(sh_quote "$log")"
    _remote_is_local && tee_to=""

    # tee, so the canonical log is written *on the machine that is building*
    # while the same bytes stream back for the watchdog and the terminal here.
    # Without it a build started from the workstation would leave nothing
    # behind on the box, and `wk logs` in a shell there -- the shell most
    # likely to be watching -- would have nothing to show.
    #
    # pipefail with it, or tee's exit status becomes the build's and every
    # failure reads as success.
    _rsh_q "set -o pipefail
          cd $(sh_quote "$(t_src "$name")") && \
          $(sh_quote "$(t_tools "$name")/lib/lockrun.sh") remote-build -w 3600 -- \
          nice -n 19 ionice -c3 $(sh_quote "$@")$tee_to"
}

# The status file, pushed to the machine as it changes.
#
# A no-op in local mode, where cmd/build has already written that exact file:
# $WK_STORE is the remote root there, so the local write and this one are the
# same path.
t_state_put() {
    local name="$1" ws
    _remote_is_local && { cat >/dev/null; return 0; }
    # Resolved with stdin closed and *before* the pipeline below: the lookup
    # can itself reach the machine, and an ssh in a command substitution reads
    # the stdin it inherits.
    ws=$(_remote_ws "$name" </dev/null)
    # The log= field is rewritten on the way: the file it names is this side's
    # transcript, and on the machine the canonical log sits beside the
    # checkout. A status file pointing at a path that does not exist over there
    # would cost `wk status` its liveness check -- the one part that answers
    # "is it still moving".
    sed "s|^log=.*|log=$ws/build.log|" \
        | _rsh "cat > $(sh_quote "$ws/build.status")" || true
}

# `wk`, run on the machine itself.
#
# It answers about its own workspaces with its own store, which is where the
# canonical build state lives -- so `wk status` and `wk logs` here ask it
# rather than reporting the half of the truth this side happens to hold.
#
# Refused unless the machine has been provisioned: `wk remote setup` is what
# puts wk-tools and the marker there, and without them the command would either
# not exist or would resolve a target it has never heard of.
t_has_wk() {
    _remote_is_local && return 1
    # Before resolving the remote root, which is what `t_tools` needs and which
    # only the capacity probe knows: on a machine that is off, resolving it dies
    # inside a command substitution and prints a connection error in the middle
    # of a listing that was about to say "unreachable" perfectly clearly.
    _remote_probe_try || return 1
    # A peer has no marker and must not be given one: the marker is what makes
    # a machine refuse the host-only commands on itself, and a workstation
    # needs those. Its own wk being there is the whole qualification.
    if _remote_peer; then
        _rsh_q "test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
        return $?
    fi
    _rsh_q "test -f \$HOME/.wk-remote && test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
}

# Two variables travel with a delegated command, as environment and not as
# arguments, and the reason is version skew: a *peer* runs its own checkout of
# this repository, kept by git, so the two sides are the same code only after
# both have pulled. An argument an old copy has never heard of is fatal there --
# measured 2026-08-19, when `--label` reached a workstation on an older tree and
# `require_name --label` killed the delegated status, dropping that machine's
# workspaces out of the listing while `wk ls`, which happened to ignore extra
# arguments, still showed them. An unknown *variable* is ignored by every
# version, so the old side answers as it always did.
t_wk() {
    _rsh "cd \$HOME && \
        ${WK_ROW_LABEL:+WK_ROW_LABEL=$(sh_quote "${WK_ROW_LABEL:-}") }\
        ${WK_NO_DELEGATE:+WK_NO_DELEGATE=1 }\
        $(sh_quote "$(t_tools '')/wk") $(sh_quote "$@")"
}

# Detached on the machine, so this end can go away.
#
# nohup and </dev/null, and the redirections matter as much as the nohup:
# ssh's own session ends when its channel closes, and a child still holding
# the tty or the pipe is killed with it. The log is not lost -- the far-side
# `wk build` writes build.log and build.status beside the checkout, which is
# where `wk logs` and `wk status` already look.
t_wk_detach() {
    _rsh_q "cd \$HOME && nohup $(sh_quote "$(t_tools '')/wk") $(sh_quote "$@") \
                >/dev/null 2>&1 </dev/null & echo \$!"
}

# The same, with a pty: `wk sudo require` over there prompts for a password,
# and sudo refuses to read one without a terminal.
t_wk_tty() {
    if _remote_is_local; then
        t_wk "$@"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" \
        "cd \$HOME && $(sh_quote "$(t_tools '')/wk") $(sh_quote "$@")"
}

# A pty, for anything with a full-screen UI. ssh gives a command no terminal
# unless it is asked to, and `wk run --lldb` without one is a debugger prompt
# that prints, accepts nothing, and dies on the first ctrl-c.
t_exec_tty() {
    local name="$1"; shift
    if _remote_is_local; then
        # Already on a terminal, if the caller had one.
        cd "$(t_src "$name")" && exec "$@"
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    exec ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" \
        "cd $(sh_quote "$(t_src "$name")") && $(sh_quote "$@")"
}

t_enter() {
    _remote_probe
    if _remote_is_local; then
        cd "$(t_src "$1")" && exec "${SHELL:-/bin/sh}" -l
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    exec ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" \
        "cd $(sh_quote "$(t_src "$1")") && exec \$SHELL -l"
}

# rsync rather than a mount or a git clone: the tooling is small, this is the
# same push the vm target does, and rsync makes it a no-op when nothing
# changed. --delete so a file removed here is removed there; excluding .git
# because the remote copy is a deployment, not a checkout to work in.
t_sync_tools() {
    local name="$1" dest
    dest=$(t_tools "$name")

    # A peer keeps its own copy, under git, and it is not ours to overwrite:
    # rsync --delete onto another workstation's checkout would throw away
    # whatever it had uncommitted -- which on a machine somebody works on is
    # the most valuable thing in the tree.
    if _remote_peer; then
        debug "not pushing wk-tools to $WK_REMOTE_HOST: it is a workstation with its own checkout"
        return 0
    fi

    # On the machine itself the tooling being run *is* the tooling: `wk` there
    # is $dest/wk, reached through the PATH entry the shell rc adds. Rsyncing a
    # tree onto itself mid-command would be, at best, pointless.
    if _remote_is_local; then
        [ "$WK_ROOT" = "$dest" ] || warn "running $WK_ROOT/wk, but this target's tooling is $dest"
        return 0
    fi

    debug "syncing wk-tools -> $WK_REMOTE_HOST"
    # rsync creates the last path element but not a missing parent, and on a
    # fresh machine `wk remote setup` gets here before anything has made the
    # remote root -- t_create's mkdir has never run.
    _rsh_q "mkdir -p $(sh_quote "$dest")"
    rsync -az --delete --exclude '.git/' \
        -e "ssh $(_ssh_opts)" \
        "$WK_ROOT/" "$WK_REMOTE_HOST:$dest/"
}

# See t_wiring_args in lib/target.sh. The machine's own shared WebKit when it
# advertises one (fetch-only, hardlinked, and the house rules ask us to use it),
# our own mirror otherwise -- and the ssh config under the wk root either way,
# which is what makes the fork push URLs resolve on a box whose ~/.ssh is not
# ours to edit.
# Wire a checkout on the machine, from the three lines above -- so creation and
# `wk remotes --fix` cannot drift apart. `origin` is upstream on every target,
# which is the point of doing this at all: `git clone --shared <mirror>` leaves
# origin pointing at the mirror, and a workspace whose origin is a local copy
# answers `git log origin/main` for whatever that copy last fetched.
_remote_wire() {
    local src="$1" n u c
    { read -r n; read -r u; read -r c; } <<EOF
$(t_wiring_args)
EOF
    _rsh_q "$(wk_wiring_script "$src" "$n" "$u" "$c")" \
        || warn "could not wire the remotes in $src"
}

t_wiring_args() {
    local ref root
    root=$(_remote_root)
    ref=$(_remote_reference)
    if [ -n "$ref" ]; then
        printf 'shared\n%s\n%s\n' "$ref" "$root/ssh/config"
    else
        printf 'mirror\n%s\n%s\n' "$root/mirror" "$root/ssh/config"
    fi
}

# `wk sync`, for a machine of its own.
#
# On a host, sync means the store: a bare mirror of every upstream plus the
# hardlinked base snapshots the overlay scheme is built on. None of that exists
# here -- a workspace is a plain checkout -- so a bare `wk sync` is refused on
# this machine (`wk` dispatch, is_host_only) and this is what takes its place:
# the two things that *do* go stale on the far end of a target.
#
# The tooling first, because it is the one that fails confusingly. Every
# delegated command runs the machine's own copy of wk-tools, so a stale copy
# answers a question this side did not ask -- `unknown option --quiet` from a
# command that works perfectly where it was typed, three times in one
# afternoon. `wk status` already names the drift; without this there was
# nothing to name as the fix but a full `wk remote setup`.
#
# Then the WebKit objects, and which ones depends on where the machine's
# workspaces come from: our own mirror is ours to fetch, a shared repository in
# the machine's MOTD is not -- it belongs to the sysadmins, is updated by them
# every ten minutes, and fetching into it would be writing to somebody else's
# repository. Said rather than silently skipped, because "wk sync did nothing
# and reported success" is indistinguishable from a bug.
t_sync() {
    local ref
    _remote_probe

    # A peer's store is its own, and syncing it means running `wk sync` over
    # there -- 13 GB of fetch and a new base snapshot, on a machine somebody
    # else may be working on. So it happens only when that machine was asked
    # for by name: WK_SYNC_NAMED is set by cmd/sync for `--target <it>` and
    # unset when `--target all` merely walked onto it. Doing it either way
    # would mean one absent-minded `wk sync --target all` publishing snapshots
    # on every machine in the fleet.
    if _remote_peer; then
        if [ -z "${WK_SYNC_NAMED:-}" ]; then
            info "$WK_REMOTE_HOST is a workstation with a store of its own -- skipped"
            log  "  sync it by name:  wk sync --target $WK_TARGET"
            return 0
        fi
        info "running 'wk sync' on $WK_REMOTE_HOST -- its store, its snapshot"
        t_wk sync
        return $?
    fi

    t_sync_tools ""
    ref=$(_remote_reference)
    if [ -n "$ref" ]; then
        info "workspaces here clone from $ref, which this machine's admins keep up to date"
        log  "  nothing of ours to fetch: no mirror is kept on $WK_REMOTE_HOST"
        return 0
    fi
    _remote_mirror_update "$(_remote_root)"
    changed "the WebKit mirror on $WK_REMOTE_HOST is up to date"
}

t_destroy() {
    local name="$1"
    _remote_peer && die "'$WK_REMOTE_HOST' is a workstation: its workspaces are removed there,
    by the machine that made them.  ssh $WK_REMOTE_HOST wk rm $name"
    _rsh_q "rm -rf $(sh_quote "$(_remote_ws "$name")")"
    rm -rf "$(wk_ws_dir "$name")"
    info "removed remote workspace '$name' from $WK_REMOTE_HOST"
}

# --- capacity ----------------------------------------------------------------
# All three answer for the remote machine, which is the whole point: the polite
# calculation in lib/resources.sh subtracts load from cores and divides memory
# by the per-job working set, and every one of those numbers has to be the far
# end's or the result is a job count for the wrong computer.

t_cores()  { _remote_probe; echo "${_WK_REMOTE_CORES:-1}"; }
t_load()   { _remote_probe; echo "${_WK_REMOTE_LOAD:-0}"; }
t_mem_mb() { _remote_probe; echo "${_WK_REMOTE_MEM:-1024}"; }
