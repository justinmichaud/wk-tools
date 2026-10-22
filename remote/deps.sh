# The distro family of a shared build machine, resolved on the machine by remote/probe.sh, which is sent after this file. Sourceable, defining a function and nothing else.

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
