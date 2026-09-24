# The card helper as `sudo -n` on the disk's machine (BatchMode ssh has no terminal), stdin passed through; lib/wk/sysimage shimmed.
CARD_PRIV=/usr/local/libexec/wk-card-priv
CARD_CHECKER=/usr/local/libexec/wk-check-boot-files.py

card_priv() { # <verb> [args...]
    r_sudo "$CARD_PRIV $(sh_quote "$@")"
}

_wk_disk() { # <verb> [args]
    ( export NODE_NAME ${!NODE_*} MODE_CHANNEL
      PYTHONPATH="$WK_ROOT/lib" exec python3 -m wk.sysimage.disk "$@" )
}
disk_part() { _wk_disk part "$1" "$2"; }
disk_of_part() { _wk_disk disk-of "$1"; }
disk_partno() { _wk_disk partno "$1"; }
disk_candidates() { _wk_disk candidates; }
disk_tran_of_name() { _wk_disk tran "$1"; }
disk_own_or_declared() { _wk_disk own-or-declared; }
disk_list() { _wk_disk list; }

disk_unmount() {
    local dev="$1"
    card_priv unmount "$dev" >/dev/null \
        || die "could not unmount what is on $dev on $NODE_NAME.
    Something is using it:
$(m_ssh "lsblk -lno NAME,MOUNTPOINT $(sh_quote "$dev")" 2>/dev/null | awk 'NF > 1 { print "    /dev/" $1 " at " $2 }')"
}

_image_wants_wifi() { PYTHONPATH="$WK_ROOT/lib" python3 -m wk.sysimage.write wants-wifi "${1:-}"; }
