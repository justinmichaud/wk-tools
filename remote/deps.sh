# What a shared build machine needs installed: one table, read by lib/wk/machine_cmd.py here and by remote/probe.sh on the machine, which it is sent ahead of. Sourceable, defining functions and nothing else.

wk_remote_deps() {   # <tool> <required|wanted> <what it is for>; a build cannot start without a `required` one and provisioning refuses, where a `wanted` one missing makes the machine work badly and is never a refusal
    cat <<'EOF'
git required the checkout, and the lock that serialises builds
cmake required configures every CMake port
ninja required every CMake port builds with it
clang required the compiler every config here names (lib/wk/buildconf.py)
python3 required webkitpy, and every structured-data step wk runs on the far side
ccache wanted without it every build on this machine starts cold, every time
zsh wanted the shell wk's rc moves an interactive session to; bash works too
EOF
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

wk_remote_build_env_vars() {   # the variables wk's build sets for itself (build_env, lib/wk/buildconf.py)
    printf '%s\n' CC CXX CFLAGS CXXFLAGS LDFLAGS MAKEFLAGS \
        CCACHE_DIR CCACHE_BASEDIR CCACHE_SLOPPINESS \
        NUMBER_OF_PROCESSORS CMAKE_BUILD_PARALLEL_LEVEL WEBKIT_OUTPUTDIR
}
