# Boot driver mac-guest: lib/wk/boot/mac.py, whose facts a bash caller reads through boot/machines.sh.
command -v boot_facts >/dev/null 2>&1 || . "$WK_ROOT/boot/machines.sh"
boot_facts mac-guest || return 1
