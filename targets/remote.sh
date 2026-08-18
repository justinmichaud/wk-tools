# Target driver: a shared, multi-user build machine.
#
# No containers here. These boxes are other people's build machines too, and
# they typically have no rootless podman, no overlay support and no appetite
# for a stranger's storage tree. So a workspace is a plain checkout under your
# own home directory.
#
# The design consequence is that isolation is gone -- there is no firewall, no
# read-only base and no disposable layer. Anything running here is trusted, and
# `wk claude` deliberately refuses to run against a remote target.
#
# What matters instead is being a good guest. Every build here is capped by
# live load, niced to the floor, and serialised against other wk builds by the
# same user.
#
# Per-host configuration lives in ~/.config/wk/targets/<name>.conf:
#
#   WK_REMOTE_HOST=devbox-arm64-2
#   WK_REMOTE_ROOT=/home/igalia/jmichaud/wk
#   WK_REMOTE_MAX_JOBS=16          # hard ceiling regardless of what is free
#
# Bespoke setup and teardown hooks are intentionally left as extension points;
# these machines differ enough that a generic implementation would be wrong.

WK_REMOTE_HOST="${WK_REMOTE_HOST:-}"
WK_REMOTE_ROOT="${WK_REMOTE_ROOT:-\$HOME/wk}"
WK_REMOTE_MAX_JOBS="${WK_REMOTE_MAX_JOBS:-16}"

_remote_require() {
    [ -n "$WK_REMOTE_HOST" ] || die "remote target needs WK_REMOTE_HOST (see ~/.config/wk/targets/)"
}

_rsh() {
    _remote_require
    ssh -o BatchMode=yes "$WK_REMOTE_HOST" "$@"
}

t_list() {
    _rsh "ls -1 $WK_REMOTE_ROOT/ws 2>/dev/null" 2>/dev/null || true
}

t_info() {
    _rsh "test -d $WK_REMOTE_ROOT/ws/$1 && echo present || echo absent" 2>/dev/null || echo absent
}

t_create() {
    local name="$1"
    _remote_require

    # A shared clone: git hardlinks the object store from a reference checkout
    # when one exists on the same filesystem, so this costs little even though
    # there is no overlay available.
    _rsh "set -e
        mkdir -p $WK_REMOTE_ROOT/ws
        if [ -d $WK_REMOTE_ROOT/mirror ]; then
            git clone --quiet --shared $WK_REMOTE_ROOT/mirror $WK_REMOTE_ROOT/ws/$name/WebKit
        else
            git clone --quiet https://github.com/WebKit/WebKit.git $WK_REMOTE_ROOT/ws/$name/WebKit
        fi"
    info "remote workspace '$name' created on $WK_REMOTE_HOST"
}

t_exec() {
    local name="$1"; shift
    # A flock keeps two of your own builds from stacking on a machine you are
    # already sharing with other people.
    #
    # sh_quote, not $*: ssh joins its arguments with spaces and hands the
    # result to the remote shell, so an unquoted argument with a space or a
    # quote would be re-split there.
    _rsh "cd $WK_REMOTE_ROOT/ws/$name/WebKit && \
          flock -w 3600 $WK_REMOTE_ROOT/.build.lock \
          nice -n 19 ionice -c3 $(sh_quote "$@")"
}

t_enter() {
    _remote_require
    exec ssh -t "$WK_REMOTE_HOST" "cd $WK_REMOTE_ROOT/ws/$1/WebKit && exec \$SHELL -l"
}

t_destroy() {
    local name="$1"
    _rsh "rm -rf $WK_REMOTE_ROOT/ws/$name"
    info "removed remote workspace '$name'"
}

# Jobs available right now, after subtracting what other users are using.
t_capacity() {
    _rsh 'nproc; awk "{print int(\$1)}" /proc/loadavg; awk "/MemAvailable/ {print int(\$2/1024)}" /proc/meminfo'
}
