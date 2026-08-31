# What a shared build machine needs installed, and the one command that
# installs it. Sourceable; defines functions and nothing else.
#
# One list, because three places ask the same question and must not disagree:
# remote/provision.sh (on the machine, while provisioning), `wk remote setup`
# (from this side, before it provisions) and `wk doctor --all` (afterwards,
# read-only).
#
# wk installs none of this. A build box belongs to everyone who logs into it and
# provisioning never takes root (remote/provision.sh's one rule), so what wk
# does instead is print the exact root command -- ready to run, or to send to the
# machine's administrators. Naming the command is the whole of the help there is
# to give: "install ccache" is a sentence, `sudo apt-get install -y ccache` is
# an action.

# <tool> <required|wanted> <what it is for>
#
# required: a build cannot start without it, and provisioning refuses.
# wanted:   the machine works without it and works badly. Never a refusal.
wk_remote_deps() {
    cat <<'EOF'
git required the checkout, and the lock that serialises builds
cmake required configures every CMake port
ninja required every CMake port builds with it
clang required the compiler every config here names (build/configs.sh)
python3 required webkitpy, and every structured-data step wk runs on the far side
ccache wanted without it every build on this machine starts cold, every time
zsh wanted the shell wk's rc moves an interactive session to; bash works too
EOF
}

# The package that carries <tool> on <family>. Only the ones whose package name
# is not the tool's name need saying; the rest fall through to the tool.
wk_remote_package() { # <tool> <family>
    case "$2:$1" in
        debian:ninja)  printf 'ninja-build' ;;
        fedora:ninja)  printf 'ninja-build' ;;
        arch:ninja)    printf 'ninja' ;;
        debian:clang)  printf 'clang' ;;
        *)             printf '%s' "$1" ;;
    esac
}

# Which family a machine's /etc/os-release puts it in -- from ID, then ID_LIKE,
# so a derivative (Raspberry Pi OS, Linux Mint, Rocky) resolves to its parent
# without being named here. `unknown` when it says nothing recognisable, which
# is reported as such rather than guessed at.
wk_remote_family() { # <ID> <ID_LIKE>
    local w
    for w in $1 $2; do
        case "$w" in
            debian|ubuntu|raspbian) printf debian; return 0 ;;
            fedora|rhel|centos)     printf fedora; return 0 ;;
            arch|archlinux)         printf arch;   return 0 ;;
            suse|opensuse*)         printf suse;   return 0 ;;
        esac
    done
    printf unknown
}

# The one command to run as root on that machine, or nothing when the family is
# not one this knows -- in which case the caller names the packages and leaves
# the command to the person, rather than printing a command that does not exist.
wk_remote_install_cmd() { # <family> <package>...
    local family="$1"; shift
    [ $# -gt 0 ] || return 1
    case "$family" in
        debian) printf 'sudo apt-get update && sudo apt-get install -y %s' "$*" ;;
        fedora) printf 'sudo dnf install -y %s' "$*" ;;
        arch)   printf 'sudo pacman -S --needed %s' "$*" ;;
        suse)   printf 'sudo zypper install -y %s' "$*" ;;
        *)      return 1 ;;
    esac
}

# The environment wk's build sets for itself, whatever the machine has in it
# (config_build_env, build/configs.sh). Reported when a login shell there
# already sets one, because a machine whose administrators export CC or
# CCACHE_DIR is a machine where a build's flags are not what its owner expects
# -- wk wins, silently, and this is where that gets said out loud.
wk_remote_build_env_vars() {
    printf '%s\n' CC CXX CFLAGS CXXFLAGS LDFLAGS MAKEFLAGS \
        CCACHE_DIR CCACHE_BASEDIR CCACHE_SLOPPINESS \
        NUMBER_OF_PROCESSORS CMAKE_BUILD_PARALLEL_LEVEL WEBKIT_OUTPUTDIR
}

# What is wrong with a machine, from remote/probe.sh's output: one finding per
# line, tab-separated,
#
#     <state>  <what>  <remedy>
#
# with state one of ok | required | wanted | note. Findings rather than printed
# prose, because the two callers render differently -- `wk doctor --all` counts
# them into its own ok/--/?? columns and `wk remote setup` narrates -- and
# neither should be re-deciding what counts as wrong.
#
# The remedy for a missing tool is one root command for the whole set, appended
# once at the end: three lines each naming apt-get is three chances to run two
# of them and stop.
wk_remote_findings() { # <probe output>
    local probe="$1" tool need why path family v pkgs=""
    _f() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }
    _v() { printf '%s\n' "$probe" | sed -n "s|^$1=||p" | tail -1; }

    family=$(_v family)

    while read -r tool need why; do
        [ -n "$tool" ] || continue
        path=$(_v "tool\.$tool")
        if [ -n "$path" ]; then
            _f ok "$tool ($path)"
            continue
        fi
        pkgs="$pkgs $(wk_remote_package "$tool" "$family")"
        case "$need" in
            required) _f required "$tool -- $why" ;;
            *)        _f wanted "$tool -- $why" ;;
        esac
    done <<DEPS
$(wk_remote_deps)
DEPS

    if [ -n "${pkgs# }" ]; then
        # shellcheck disable=SC2086 -- the package list is deliberately split.
        if v=$(wk_remote_install_cmd "$family" ${pkgs# }); then
            _f note "as root on $(_v host):" "$v"
        else
            _f note "$(_v host) runs $(_v os), whose package manager this does not know" \
                    "install by hand:${pkgs}"
        fi
    fi

    # The identity a commit made there would carry, against the identity this
    # repository declares (dotfiles/gitconfig) -- not merely "is it set". Both
    # ways of being wrong are silent until a commit is already made: unset, and
    # git refuses it after the work with "Please tell me who you are"; set to
    # something else, and it is authored by whatever that says. One machine here
    # had `user.name = no`, an answer to a prompt years ago.
    local want have
    for v in name email; do
        want=$(git config --file "$WK_ROOT/dotfiles/gitconfig" --get "user.$v" 2>/dev/null || true)
        have=$(_v "git\.$v")
        if [ -n "$want" ] && [ "$have" = "$want" ]; then
            _f ok "git user.$v = $have"
        elif [ -z "$have" ]; then
            _f wanted "git user.$v is not set there -- a commit would be refused" \
                      "wk remote setup <target>  (it writes the include)"
        else
            _f wanted "git user.$v there is '$have', not this repository's '$want' -- every commit made there carries it" \
                      "wk remote setup <target>  (it removes the shadowing value)"
        fi
    done
    case "$(_v git\.fsmonitor):$(_v git\.manyfiles)" in
        true:true) _f ok "git speed settings (fsmonitor, manyFiles)" ;;
        *) _f wanted "git there is unconfigured for a big checkout, so \`git status\` in WebKit walks the whole tree -- which an editor over ssh asks on every keystroke" \
                     "wk remote setup <target>  (dotfiles/gitconfig, through the include)" ;;
    esac

    # A machine whose administrators export CC or CCACHE_DIR: wk's build sets
    # its own (config_build_env) and does not read theirs, so the flags in force
    # are not the ones that machine's owner would expect. Said out loud rather
    # than silently won.
    printf '%s\n' "$probe" | sed -n 's/^env\.\([A-Z_]*\)=\(.*\)$/\1 \2/p' \
        | while read -r v rest; do
        _f note "$v is set to '$rest' in a login shell there" \
                "wk's build sets its own $v and ignores that one (build/configs.sh)"
    done

    unset -f _f _v
    return 0
}

# The probe, run on a machine: these two files concatenated and fed to a shell
# over there. One implementation, so `wk remote setup` and `wk doctor --all`
# send the same evidence-gatherer and cannot answer differently about the same
# machine.
#
# The far side needs no wk-tools of its own -- deps.sh carries the list, and
# this must answer about a machine that has never been provisioned. `_rsh`
# (targets/remote.sh, in scope once the caller has load_target'd the machine)
# and not `_rsh_q`: the script arrives on stdin, and `_rsh_q` is the form that
# closes stdin so a question cannot drink the terminal.
wk_remote_probe() {
    cat "$WK_ROOT/remote/deps.sh" "$WK_ROOT/remote/probe.sh" | _rsh 'bash -s'
}
