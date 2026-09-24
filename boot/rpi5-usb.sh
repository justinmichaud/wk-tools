# Boot driver rpi5-usb: lib/wk/boot/pi.py; these are the functions a caller asks for by `command -v`.
command -v boot_facts >/dev/null 2>&1 || . "$WK_ROOT/boot/machines.sh"
boot_facts rpi5-usb || return 1
b_medium_selects_by_partition() { _wk_boot rpi5-usb selects-by-partition; }
