# Boot driver mac-guest: lib/wk/boot/mac.py. Here: the verbs a bash caller asks for past boot/machines.sh's, and m_ssh, since a guest's address is in no ssh config and changes with every boot.
command -v boot_facts >/dev/null 2>&1 || . "$WK_ROOT/boot/machines.sh"
boot_facts mac-guest || return 1
_wk_mac() { ( export NODE_NAME ${!NODE_*} MODE MODE_CHANNEL; PYTHONPATH="$WK_ROOT/lib" exec python3 -m wk.boot.mac mac-guest "$@" ); }
b_bench_root() { _wk_mac bench-root; }
b_bench_home() { _wk_mac bench-home; }
b_bench_put() { _wk_mac bench-put "$1" "$2" ${BENCH_PUT_SKIP:?names what a put never carries, and lib/bench.sh sets it}; }
b_bench_put_file() { _wk_mac bench-put-file "$@"; }
b_manage() { _wk_mac manage "$@"; }
b_manage_name() { _wk_mac manage-name; }
b_manage_tools() { printf '%s' "$WK_ROOT"; }
b_manage_prepare() { return 0; }
b_restart_ready() { _wk_mac restart-ready; }
b_restart_detail() { _wk_mac restart-detail; }
m_ssh() { _wk_mac exec "$@"; }
