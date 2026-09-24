# Boot driver mac-volume: lib/wk/boot/mac.py. Here: the verbs a bash caller asks for past boot/machines.sh's, and the bench channel's destination for the r_ssh bench/mac-ab.sh reads through -- the ssh config alias, `bench`'s own pinned key and not a Pi image's root.
command -v boot_facts >/dev/null 2>&1 || . "$WK_ROOT/boot/machines.sh"
boot_facts mac-volume || return 1
_wk_mac() { ( export NODE_NAME ${!NODE_*} MODE MODE_CHANNEL; PYTHONPATH="$WK_ROOT/lib" exec python3 -m wk.boot.mac mac-volume "$@" ); }
b_disarm() { _wk_boot mac-volume disarm || exit $?; }
b_disarm_note() { _wk_boot mac-volume disarm-note >&2; }
b_bench_root() { _wk_mac bench-root; }
b_bench_home() { _wk_mac bench-home; }
b_bench_put() { _wk_mac bench-put "$1" "$2" ${BENCH_PUT_SKIP:?names what a put never carries, and lib/bench.sh sets it}; }
b_bench_put_file() { _wk_mac bench-put-file "$@"; }
b_manage() { _wk_mac manage "$@"; }
b_manage_name() { _wk_mac manage-name; }
b_manage_tools() {
    machine_tools_present "$NODE_SSH" || return 1
    machine_tools_dir
}
b_manage_prepare() { machine_prepare "$NODE_SSH"; }
b_restart_ready() { _wk_mac restart-ready; }
b_restart_detail() { _wk_mac restart-detail; }
mv_reboot_ready() { _wk_mac restart-ready; }
mac_volume_present() { _wk_mac volume-present; }
mac_firmware_default() { _wk_mac firmware-default; }
image_addr() { printf '%s' "${WK_MAC_BENCH_SSH:-${NODE_BENCH_SSH:-}}"; }
i_ssh_opts() { :; }
