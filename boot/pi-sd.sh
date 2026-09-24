# Boot driver pi-sd: lib/wk/boot/pi.py; these are the functions a caller asks for by `command -v`.
command -v boot_facts >/dev/null 2>&1 || . "$WK_ROOT/boot/machines.sh"
boot_facts pi-sd || return 1
b_disarm() { _wk_boot pi-sd disarm || exit $?; }
b_disarm_note() { _wk_boot pi-sd disarm-note >&2; }
b_self_disarm_sh() { _wk_boot pi-sd self-disarm; }
