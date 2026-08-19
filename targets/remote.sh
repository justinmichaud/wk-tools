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
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=auto -o ControlPath=$d/%h-%p-%r -o ControlPersist=60"
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
_remote_probe() {
    [ -n "${_WK_REMOTE_PROBED:-}" ] && return 0
    _remote_require
    local out
    out=$(_rsh_q 'echo "$HOME"; nproc; awk "{print int(\$1)}" /proc/loadavg;
                awk "/^MemAvailable:/ {print int(\$2/1024)}" /proc/meminfo') \
        || die "cannot reach '$WK_REMOTE_HOST' over ssh.
    This target has no way in but ssh, and it is not interactive: the key,
    the ProxyJump and the host entry all have to work non-interactively.
    Try:  ssh -o BatchMode=yes $WK_REMOTE_HOST true"

    _WK_REMOTE_HOME=$(printf '%s\n' "$out" | sed -n 1p)
    _WK_REMOTE_CORES=$(printf '%s\n' "$out" | sed -n 2p)
    _WK_REMOTE_LOAD=$(printf '%s\n' "$out" | sed -n 3p)
    _WK_REMOTE_MEM=$(printf '%s\n' "$out" | sed -n 4p)

    [ -n "$WK_REMOTE_ROOT" ] || WK_REMOTE_ROOT="$_WK_REMOTE_HOME/wk"
    _WK_REMOTE_PROBED=1
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

# wk-tools is pushed to the remote root rather than per workspace: it is the
# same tree for all of them, and `wk build` re-rsyncs it on every run.
t_tools() { echo "$(_remote_root)/tools"; }

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

t_info() {
    _rsh_q "test -d $(sh_quote "$(_remote_ws "$1")/WebKit") && echo present || echo absent" \
        2>/dev/null || echo absent
}

t_create() {
    local name="$1" root ws ref
    _remote_probe
    root=$(_remote_root)
    ws=$(_remote_ws "$name")

    [ "$(t_info "$name")" = absent ] || die "workspace '$name' already exists on $WK_REMOTE_HOST"

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
        _rsh_q "$(wk_wiring_script "$ws/WebKit" shared "$ref" "$root/ssh/config")" \
            || warn "could not wire the remotes in $ws/WebKit"
    else
        # No shared repository, so we keep one -- and this is also the answer
        # to "what does `wk sync` mean remotely": it means this, and it happens
        # here.
        #
        # `wk sync` is a host-store command -- a bare mirror plus the
        # hardlinked snapshots the overlay scheme is built on -- and none of
        # that exists on a machine where a workspace is a plain clone. What
        # does carry over is the reason the mirror exists: cloning WebKit from
        # GitHub once per workspace is minutes of somebody else's bandwidth
        # every time. So the driver keeps one mirror per remote root and clones
        # every workspace from it with --shared, which costs a checkout and no
        # objects at all.
        #
        # Only main is mirrored, matching wk_mirror_branches: WebKit has ~920
        # branches and the ones that are not main are not what a workspace
        # starts from. Anything else is still one `git fetch` away inside the
        # workspace.
        #
        # gc.auto is off for the same reason it is off in the local mirror: the
        # workspaces borrow its objects through --shared, and a repack
        # underneath a live clone is how that arrangement breaks.
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
            git -C \"\$M\" fetch --prune -q origin
            git clone --quiet --shared -b main \"\$M\" $(sh_quote "$ws/WebKit")" \
            || die "could not create the checkout on $WK_REMOTE_HOST"
        # Same wiring as every other target: origin is WebKit/WebKit, the forks
        # are here, and the local mirror keeps a name of its own -- the
        # workspace borrows its objects through --shared either way.
        _rsh_q "$(wk_wiring_script "$ws/WebKit" mirror "$root/mirror" "$root/ssh/config")" \
            || warn "could not wire the remotes in $ws/WebKit"
    fi

    # ccache's own default ceiling is 5 GB, which a couple of WebKit builds
    # blow through; recorded in the cache's config so `ccache -s` on the box
    # reports the real limit too.
    _rsh_q "printf 'max_size = %s\n' $(sh_quote "${WK_CCACHE_MAXSIZE:-40G}") \
          > $(sh_quote "$root/cache/ccache/ccache.conf")" || true

    ensure_dir "$(wk_ws_dir "$name")"
    info "remote workspace '$name' created on $WK_REMOTE_HOST ($ws)"
}

t_exec() {
    local name="$1"; shift
    _rsh "cd $(sh_quote "$(t_src "$name")") && $(sh_quote "$@")"
}

# The build, and only the build, is serialised. A flock keeps two of your own
# builds from stacking on a machine you are already sharing with other people
# -- and putting it on t_exec instead would silently block every `wk run` and
# every one-line probe behind an hour-long build.
#
# nice and ionice are here as well as in build-in-target.sh: this end knows the
# target is shared, and the flock has to be outside them either way.
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
          flock -w 3600 $(sh_quote "$(_remote_root)/.build.lock") \
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
    _rsh_q "test -f \$HOME/.wk-remote && test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
}

t_wk() {
    _rsh "cd \$HOME && $(sh_quote "$(t_tools '')/wk") $(sh_quote "$@")"
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

t_destroy() {
    local name="$1"
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
