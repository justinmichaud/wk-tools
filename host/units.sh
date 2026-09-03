# The systemd --user units a machine that runs workspaces carries, and the one
# way they are installed.
#
# Three services, one body each, in host/units/*.service: the egress proxy, the
# ssh-agent holding the deploy keys, and the GitHub API credential injector.
# Two machines install them -- this workstation (host/linux/sdk.sh) and the
# podman machine on macOS (host/macos/vmtools.sh) -- and they differ in exactly
# two things, which the bodies spell as @WK_ROOT@ and @WK_STORE@: where the
# checkout is (a virtiofs mount at /opt/wk-tools inside the podman machine,
# this checkout itself on a workstation) and where the store is.
#
# <run...> is the caller's own way of running one command string on the machine
# the unit is for, with the unit body on stdin -- `sh -c` locally, the driver's
# ssh wrapper for the podman machine -- passed as a command *prefix* so nothing
# here leaves a global function behind.

command -v warn >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"

unit_render() { # <unit name> <tools root over there> <store over there>
    local f="$WK_ROOT/host/units/$1"
    [ -f "$f" ] || die "no unit body for '$1' in $WK_ROOT/host/units"
    sed -e "s|@WK_ROOT@|$2|g" -e "s|@WK_STORE@|$3|g" "$f"
}

# Written beside the live one, compared, and moved into place only when it
# differs -- so a re-run of ./setup reports no change and does not
# daemon-reload under a service a build is depending on.
unit_install() { # <unit name> <tools root> <store> <run...>
    local name="$1" root="$2" store="$3"; shift 3
    local dir='~/.config/systemd/user' tmp
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
