_wk_disk() { # <verb> [args]
    ( export NODE_NAME ${!NODE_*} MODE_CHANNEL
      PYTHONPATH="$WK_ROOT/lib" exec python3 -m wk.sysimage.disk "$@" )
}
disk_part() { _wk_disk part "$1" "$2"; }
disk_of_part() { _wk_disk disk-of "$1"; }
disk_partno() { _wk_disk partno "$1"; }
disk_tran_of_name() { _wk_disk tran "$1"; }
disk_list() { _wk_disk list; }

_image_wants_wifi() { PYTHONPATH="$WK_ROOT/lib" python3 -m wk.sysimage.write wants-wifi "${1:-}"; }
