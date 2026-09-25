# Boot driver mac-volume: lib/wk/boot/mac.py. Here: the verbs a bash caller asks for past boot/machines.sh's.
command -v boot_facts >/dev/null 2>&1 || . "$WK_ROOT/boot/machines.sh"
boot_facts mac-volume || return 1
b_disarm() { _wk_boot mac-volume disarm || exit $?; }
b_disarm_note() { _wk_boot mac-volume disarm-note >&2; }
