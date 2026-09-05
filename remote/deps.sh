# What a shared build machine needs installed, and the one command that installs it. Sourceable, defining functions and nothing else. One list, because remote/provision.sh, `wk remote setup` and `wk doctor --all` all ask it and must not disagree. wk installs none of it -- provisioning never takes root on someone else's machine -- so it prints the root command for the machine's administrators.

wk_remote_deps() {   # <tool> <required|wanted> <what it is for>; a build cannot start without a `required` one and provisioning refuses, where a `wanted` one missing makes the machine work badly and is never a refusal
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

wk_remote_package() { # <tool> <family> -- only a tool whose package name is not the tool's name needs an entry
    case "$2:$1" in
        debian:ninja)  printf 'ninja-build' ;;
        fedora:ninja)  printf 'ninja-build' ;;
        arch:ninja)    printf 'ninja' ;;
        debian:clang)  printf 'clang' ;;
        *)             printf '%s' "$1" ;;
    esac
}

wk_remote_family() { # <ID> <ID_LIKE> -- ID then ID_LIKE, so a derivative (Raspberry Pi OS, Mint, Rocky) resolves to its parent without being named here; `unknown` is reported, never guessed
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

wk_remote_install_cmd() { # <family> <package>... -- nothing for a family it does not know, the caller then naming the packages and leaving the command to the person
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

wk_remote_build_env_vars() {   # the variables wk's build sets for itself (config_build_env, build/configs.sh)
    printf '%s\n' CC CXX CFLAGS CXXFLAGS LDFLAGS MAKEFLAGS \
        CCACHE_DIR CCACHE_BASEDIR CCACHE_SLOPPINESS \
        NUMBER_OF_PROCESSORS CMAKE_BUILD_PARALLEL_LEVEL WEBKIT_OUTPUTDIR
}

wk_remote_findings() { # <probe output> -- what is wrong with a machine, one finding per line, tab-separated <state>\t<what>\t<remedy> with state one of ok | required | wanted | note, because `wk doctor --all` counts them into its columns while `wk remote setup` narrates. Missing tools share one root command at the end.
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

    # The identity a commit made there would carry, against the identity this repository declares (dotfiles/gitconfig), not merely "is it set": one machine here had `user.name = no`, an answer to a prompt years ago.
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

    printf '%s\n' "$probe" | sed -n 's/^env\.\([A-Z_]*\)=\(.*\)$/\1 \2/p' \
        | while read -r v rest; do
        _f note "$v is set to '$rest' in a login shell there" \
                "wk's build sets its own $v and ignores that one (build/configs.sh)"
    done

    unset -f _f _v
    return 0
}

wk_remote_probe() {   # these two files concatenated and fed to a shell over there, so the far side needs no wk-tools of its own; `_rsh` and not `_rsh_q`, the script arriving on stdin, which `_rsh_q` closes
    cat "$WK_ROOT/remote/deps.sh" "$WK_ROOT/remote/probe.sh" | _rsh 'bash -s'
}
