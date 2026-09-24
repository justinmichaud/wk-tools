# Boot driver pi-tryboot: lib/wk/boot/pi.py; these are the functions a caller asks for by `command -v`.
command -v boot_facts >/dev/null 2>&1 || . "$WK_ROOT/boot/machines.sh"
boot_facts pi-tryboot || return 1
b_disarm() { _wk_boot pi-tryboot disarm || exit $?; }
b_disarm_note() { _wk_boot pi-tryboot disarm-note >&2; }
b_self_disarm_sh() { _wk_boot pi-tryboot self-disarm; }
