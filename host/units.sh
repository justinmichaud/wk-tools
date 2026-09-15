command -v warn >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"

unit_exists() { # <unit name>
    [ -f "$WK_ROOT/host/units/$1" ] || die "no unit body for '$1' in $WK_ROOT/host/units"
}

unit_render() { # <unit name> <tools root over there> <store over there>
    unit_exists "$1"
    sed -e "s|@WK_ROOT@|$2|g" -e "s|@WK_STORE@|$3|g" "$WK_ROOT/host/units/$1"
}

unit_program() { # <unit name>
    unit_exists "$1"
    awk -v p='@WK_ROOT@/' 'index($0, "ExecStart=") == 1 {
        for (i = 1; i <= NF; i++)
            if (index($i, p) == 1) { print substr($i, length(p) + 1); exit }
    }' "$WK_ROOT/host/units/$1"
}

# A service goes on running the program it exec'd: a tools sync moves the file under it, and until it is restarted the machine is running code nobody can read in the tree. /proc/<pid> is stamped with when the process started, so this compares two facts and stores neither.
unit_stale() { # <unit name> -- 0 when this tree's program is newer than the running service
    local name="$1" prog pid
    prog=$(unit_program "$name")
    [ -n "$prog" ] || return 1
    pid=$(systemctl --user show -p MainPID --value "$name" 2>/dev/null) || return 1
    { [ -n "$pid" ] && [ "$pid" != 0 ] && [ -d "/proc/$pid" ]; } || return 1
    [ "$WK_ROOT/$prog" -nt "/proc/$pid" ]
}

unit_install() { # <unit name> <tools root> <store> <run...>
    local name="$1" root="$2" store="$3"; shift 3
    local dir='~/.config/systemd/user' tmp
    if [ -n "${WK_DRY_RUN:-}" ]; then
        if unit_render "$name" "$root" "$store" | "$@" "cmp -s - $dir/$name"; then
            unchanged "$name"
        else
            changed "would install $name into $dir and reload systemd"
        fi
        return 0
    fi
    tmp=$(mktemp)
    unit_render "$name" "$root" "$store" > "$tmp"
    "$@" "mkdir -p $dir && cat > $dir/$name.new && chmod 0644 $dir/$name.new" < "$tmp"
    rm -f "$tmp"
    if "$@" "cmp -s $dir/$name.new $dir/$name"; then
        "$@" "rm -f $dir/$name.new"
        unchanged "$name"
        return 0
    fi
    "$@" "mv $dir/$name.new $dir/$name && systemctl --user daemon-reload"
    changed "installed $name"
}

unit_unready() { # <unit name> <consequence> <journal prefix>
    warn "$1 did not reach readiness -- $2
  why: ${3}journalctl --user -u $1 -e"
}

# Every body is Type=notify or forking: Type=simple answers is-active yes at t=0.
unit_start() { # <unit name> <root> <store> <consequence> <journal prefix> <run...>
    local name="$1" root="$2" store="$3" why="$4" jrn="$5"; shift 5
    local prog stamp="" want="" have="" active=yes

    unit_install "$name" "$root" "$store" "$@"

    # `enable --now` is a no-op on a running service, so ask before touching it.
    "$@" "systemctl --user is-active --quiet $name" || active=no

    prog=$(unit_program "$name")
    if [ -n "$prog" ]; then
        stamp="$store/.${name%.service}.program"
        want=$(cksum < "$WK_ROOT/$prog" | awk '{print $1}')
        have=$("$@" "cat $stamp 2>/dev/null" || true)
    fi

    if [ -n "${WK_DRY_RUN:-}" ]; then
        if [ "$active" = no ]; then
            changed "would start $name"
        elif [ "$have" = "$want" ]; then
            unchanged "$name ready"
        else
            changed "would restart $name (its program changed)"
        fi
        return 0
    fi

    # A unit at its start limit refuses to start until the counter is cleared.
    "$@" "systemctl --user reset-failed $name" >/dev/null 2>&1 || true

    if ! "$@" "systemctl --user enable --now $name" >/dev/null 2>&1; then
        unit_unready "$name" "$why" "$jrn"
        return 0
    fi

    if [ "$active" = no ]; then
        if [ -n "$prog" ]; then "$@" "echo $want > $stamp"; fi
        changed "started $name"
        return 0
    fi

    if [ "$have" = "$want" ]; then
        unchanged "$name ready"
        return 0
    fi
    if ! "$@" "systemctl --user restart $name" >/dev/null 2>&1; then
        unit_unready "$name" "$why" "$jrn"
        return 0
    fi
    "$@" "echo $want > $stamp"
    changed "restarted $name (program changed)"
}
