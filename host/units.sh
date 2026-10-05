command -v warn >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"

unit_exists() { # <unit name>
    [ -f "$WK_ROOT/host/units/$1" ] || die "no unit body for '$1' in $WK_ROOT/host/units"
}

unit_render() { # <unit name> <tools root over there> <store over there>; WK_UNIT_CLAUDE_LOGIN, the login the injector holds
    unit_exists "$1"
    sed -e "s|@WK_ROOT@|$2|g" -e "s|@WK_STORE@|$3|g" -e "s|@WK_CLAUDE_LOGIN@|${WK_UNIT_CLAUDE_LOGIN:-}|g" "$WK_ROOT/host/units/$1"
}

# The one stamp of a daemon setup restarts when it changes: its program and every module of this tree that imports.
program_stamp() { # <program, relative to WK_ROOT>
    python3 - "$WK_ROOT" "$1" <<'PY'
import modulefinder, os, sys, zlib

root, prog = sys.argv[1], sys.argv[2]
tree = [os.path.dirname(os.path.join(root, prog)), os.path.join(root, "lib")]   # a program's own directory, as python3 puts it first


class InTree(modulefinder.ModuleFinder):
    def find_module(self, name, path, parent=None):
        return super().find_module(name, tree if path is None else path, parent)


files = {os.path.join(root, prog)}
if prog.endswith(".py"):
    f = InTree(tree)
    f.run_script(os.path.join(root, prog))
    files |= {m.__file__ for m in f.modules.values() if m.__file__}
crc = 0
for p in sorted(files):
    with open(p, "rb") as fh:
        crc = zlib.crc32(fh.read(), crc)
print(crc)
PY
}

unit_program() { # <unit name>
    unit_exists "$1"
    awk -v p='@WK_ROOT@/' 'index($0, "ExecStart=") == 1 {
        for (i = 1; i <= NF; i++)
            if (index($i, p) == 1) { print substr($i, length(p) + 1); exit }
    }' "$WK_ROOT/host/units/$1"
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
        want="$(program_stamp "$prog")-$(unit_render "$name" "$root" "$store" | cksum | awk '{print $1}')"
        have=$("$@" "cat $stamp 2>/dev/null" || true)
    fi

    if [ -n "${WK_DRY_RUN:-}" ]; then
        if [ "$active" = no ]; then
            changed "would start $name"
        elif [ "$have" = "$want" ]; then
            unchanged "$name ready"
        else
            changed "would restart $name (its program or unit changed)"
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
    changed "restarted $name (program or unit changed)"
}
