# NOPASSWD on one absolute path each, so safety reduces to who can write that path:
# root-owned installs, never a symlink into this repo, and an unverifiable mode refuses.
# zzz-: sudo takes the LAST match in /etc/sudoers.d and zz-<user>-passwd re-imposes
# `PASSWD: ALL` over every command, so an earlier name is a dead grant; a dot is skipped.
_libexec=/usr/local/libexec
_target="$_libexec/wk-quiesce-priv"
_source="$WK_ROOT/admin/wk-quiesce-priv"
_sudoers=/etc/sudoers.d/zzz-wk-quiesce
# Tombstone: an out-ranked second grant of the same path still reads as in force.
_sudoers_old=/etc/sudoers.d/wk-quiesce

if [ ! -f "$_source" ]; then
    warn "quiesce helper missing at $_source; skipping"
    return 0 2>/dev/null || true
fi

_needs_install=0
if [ ! -f "$_target" ] || ! cmp -s "$_source" "$_target"; then
    _needs_install=1
fi

# GNU form first: Linux's `stat -f` succeeds as "filesystem status", never as an owner.
_owner=$(stat -c '%U' "$_target" 2>/dev/null || stat -f '%Su' "$_target" 2>/dev/null || echo "")
[ -f "$_target" ] && [ "$_owner" != root ] && _needs_install=1

_rule="$(id -un) ALL=(root) NOPASSWD: $_target"

_sudoers_ok=0
if [ -x "$_target" ] && sudo -n "$_target" status >/dev/null 2>&1; then
    _sudoers_ok=1
fi

if [ "$_needs_install" -eq 0 ] && [ "$_sudoers_ok" -eq 1 ]; then
    unchanged "quiesce helper and sudoers rule"
elif ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
    if [ "$_needs_install" -eq 0 ]; then
        warn "the quiesce helper is installed, but its sudoers rule is not in force"
        log  "  something later in the include order grants PASSWD over it."
        log  "  'sudo -l' shows the order; $_sudoers has to be the last match."
        log  "  from an interactive shell:  ./setup --stage quiesce"
    else
        warn "quiesce helper not installed: sudo needs a terminal"
        log  "  run this from an interactive shell:  ./setup --stage quiesce"
    fi
else
    info "installing the quiesce helper (requires sudo once)"

    sudo install -d -o root -g wheel -m 0755 "$_libexec" 2>/dev/null \
        || sudo install -d -o root -g root -m 0755 "$_libexec"

    sudo install -o root -m 0755 "$_source" "$_target"
    changed "installed $_target"

    # An invalid sudoers file locks the account out of sudo entirely.
    _tmp="$(mktemp)"
    printf '%s\n' "$_rule" > "$_tmp"

    if sudo visudo -cqf "$_tmp"; then
        sudo install -o root -m 0440 "$_tmp" "$_sudoers"
        changed "installed $_sudoers"
        if [ -f "$_sudoers_old" ]; then
            sudo rm -f "$_sudoers_old"
            changed "removed $_sudoers_old (it sorted before zz-<user>-passwd and was dead)"
        fi
    else
        rm -f "$_tmp"
        die "generated sudoers rule failed validation; nothing was installed"
    fi
    rm -f "$_tmp"
fi

# Card gate, narrower than the capability: only a usb or mmc whole disk the machine is
# not running from -- the boot check, not the transport one, is what makes it safe.
_card_target="$_libexec/wk-card-priv"
_card_source="$WK_ROOT/admin/wk-card-priv"
# Beside the helper under the name it knows: boot-check never runs a caller-named path.
_check_target="$_libexec/wk-check-boot-files.py"
_check_source="$WK_ROOT/boot/check-boot-files.py"
_card_sudoers=/etc/sudoers.d/zzz-wk-card
_card_sudoers_old=/etc/sudoers.d/wk-card

if [ ! -f "$_card_source" ]; then
    warn "card helper missing at $_card_source; skipping"
elif ! is_linux; then
    unchanged "card helper (linux only)"
else
    _card_needs=0
    if [ ! -f "$_card_target" ] || ! cmp -s "$_card_source" "$_card_target"; then _card_needs=1; fi
    if [ ! -f "$_check_target" ] || ! cmp -s "$_check_source" "$_check_target"; then _card_needs=1; fi
    _card_owner=$(stat -c '%U' "$_card_target" 2>/dev/null || echo "")
    [ -f "$_card_target" ] && [ "$_card_owner" != root ] && _card_needs=1

    _card_ok=0
    if [ -x "$_card_target" ] && sudo -n "$_card_target" status >/dev/null 2>&1; then _card_ok=1; fi

    if [ "$_card_needs" -eq 0 ] && [ "$_card_ok" -eq 1 ]; then
        unchanged "card helper and sudoers rule"
    elif ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
        if [ "$_card_needs" -eq 0 ]; then
            warn "the card helper is installed, but its sudoers rule is not in force"
            log  "  'sudo -l' shows the order; $_card_sudoers has to be the last match."
            log  "  from an interactive shell:  ./setup --stage quiesce"
        else
            warn "card helper not installed: sudo needs a terminal"
            log  "  run this from an interactive shell:  ./setup --stage quiesce"
        fi
    else
        info "installing the card helper (requires sudo once)"
        sudo install -d -o root -g root -m 0755 "$_libexec"
        sudo install -o root -m 0755 "$_card_source" "$_card_target"
        sudo install -o root -m 0644 "$_check_source" "$_check_target"
        changed "installed $_card_target and $_check_target"

        _card_tmp="$(mktemp)"
        printf '%s\n' "$(id -un) ALL=(root) NOPASSWD: $_card_target" > "$_card_tmp"
        if sudo visudo -cqf "$_card_tmp"; then
            sudo install -o root -m 0440 "$_card_tmp" "$_card_sudoers"
            changed "installed $_card_sudoers"
            if [ -f "$_card_sudoers_old" ]; then
                sudo rm -f "$_card_sudoers_old"
                changed "removed $_card_sudoers_old (it sorted before zz-<user>-passwd and was dead)"
            fi
        else
            rm -f "$_card_tmp"
            die "generated card sudoers rule failed validation; nothing was installed"
        fi
        rm -f "$_card_tmp"
    fi

    # Checked every run: writable by anyone but root is a root escalation.
    if [ -f "$_card_target" ]; then
        _card_perm=$(stat -c '%a' "$_card_target" 2>/dev/null || echo "")
        case "$_card_perm" in
            ''|*[!0-7]*) die "could not read the mode of $_card_target -- refusing to vouch for $_card_sudoers" ;;
            *[2367])     die "$_card_target is world-writable (mode $_card_perm) -- remove $_card_sudoers now" ;;
            ?[2367]?)    die "$_card_target is group-writable (mode $_card_perm) -- remove $_card_sudoers now" ;;
        esac
        unchanged "card helper permissions ($_card_perm)"
    fi
fi
unset _card_target _card_source _card_sudoers _card_sudoers_old _card_needs _card_owner _card_ok _card_tmp _card_perm

# Same shape: a fixed verb list, no passthrough, one validated boot order, a fixed tag.
_boot_target="$_libexec/wk-boot-priv"
_boot_source="$WK_ROOT/admin/wk-boot-priv"
_boot_sudoers=/etc/sudoers.d/zzz-wk-boot

if [ ! -f "$_boot_source" ]; then
    warn "boot helper missing at $_boot_source; skipping"
elif ! is_linux; then
    unchanged "boot helper (linux only)"
else
    _boot_needs=0
    if [ ! -f "$_boot_target" ] || ! cmp -s "$_boot_source" "$_boot_target"; then _boot_needs=1; fi
    _boot_owner=$(stat -c '%U' "$_boot_target" 2>/dev/null || echo "")
    [ -f "$_boot_target" ] && [ "$_boot_owner" != root ] && _boot_needs=1

    _boot_ok=0
    if [ -x "$_boot_target" ] && sudo -n "$_boot_target" status >/dev/null 2>&1; then _boot_ok=1; fi

    if [ "$_boot_needs" -eq 0 ] && [ "$_boot_ok" -eq 1 ]; then
        unchanged "boot helper and sudoers rule"
    elif ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
        if [ "$_boot_needs" -eq 0 ]; then
            warn "the boot helper is installed, but its sudoers rule is not in force"
            log  "  'sudo -l' shows the order; $_boot_sudoers has to be the last match."
            log  "  from an interactive shell:  ./setup --stage quiesce"
        else
            warn "boot helper not installed: sudo needs a terminal"
            log  "  run this from an interactive shell:  ./setup --stage quiesce"
        fi
    else
        info "installing the boot helper (requires sudo once)"
        sudo install -d -o root -g root -m 0755 "$_libexec"
        sudo install -o root -m 0755 "$_boot_source" "$_boot_target"
        changed "installed $_boot_target"

        _boot_tmp="$(mktemp)"
        printf '%s\n' "$(id -un) ALL=(root) NOPASSWD: $_boot_target" > "$_boot_tmp"
        if sudo visudo -cqf "$_boot_tmp"; then
            sudo install -o root -m 0440 "$_boot_tmp" "$_boot_sudoers"
            changed "installed $_boot_sudoers"
        else
            rm -f "$_boot_tmp"
            die "generated boot sudoers rule failed validation; nothing was installed"
        fi
        rm -f "$_boot_tmp"
    fi

    if [ -f "$_boot_target" ]; then
        _boot_perm=$(stat -c '%a' "$_boot_target" 2>/dev/null || echo "")
        case "$_boot_perm" in
            ''|*[!0-7]*) die "could not read the mode of $_boot_target -- refusing to vouch for $_boot_sudoers" ;;
            *[2367])     die "$_boot_target is world-writable (mode $_boot_perm) -- remove $_boot_sudoers now" ;;
            ?[2367]?)    die "$_boot_target is group-writable (mode $_boot_perm) -- remove $_boot_sudoers now" ;;
        esac
        unchanged "boot helper permissions ($_boot_perm)"
    fi
fi
unset _boot_target _boot_source _boot_sudoers _boot_needs _boot_owner _boot_ok _boot_tmp _boot_perm

# Tombstones: without these an older revision's root-owned file and dead grant stay.
_retired="${_libexec:-/usr/local/libexec}/wk-tftpd /etc/sudoers.d/wk-netboot"
_stale=""
for _f in $_retired; do [ -e "$_f" ] && _stale="$_stale $_f"; done
if [ -n "$_stale" ]; then
    if ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
        warn "retired privileged file(s) present:$_stale"
        log  "  removing them needs sudo; run:  ./setup --stage quiesce"
    else
        info "removing retired privileged file(s) (requires sudo once)"
        # shellcheck disable=SC2086
        sudo rm -f $_stale
        changed "removed$_stale"
    fi
fi
unset _retired _stale _f

# Root-owned and argument-free: an argument would widen the allowlist to "as anybody".
_sessenv="$_libexec/wk-session.env"
_sessline="WK_SESSION_USER=$(id -un)"

if [ -f "$_target" ]; then
    if sudo -n grep -qxF "$_sessline" "$_sessenv" 2>/dev/null \
       || grep -qxF "$_sessline" "$_sessenv" 2>/dev/null; then
        unchanged "session user"
    else
        _tmp2="$(mktemp)"
        printf '%s\n' "$_sessline" > "$_tmp2"
        if sudo install -o root -m 0644 "$_tmp2" "$_sessenv" 2>/dev/null; then
            changed "recorded the session user in $_sessenv"
        else
            warn "could not write $_sessenv; 'wk session' will refuse to start"
        fi
        rm -f "$_tmp2"
    fi
fi

if [ ! -f "$_target" ]; then
    unset _libexec _target _source _sudoers _sudoers_old _needs_install _owner _rule _sudoers_ok _tmp
    return 0 2>/dev/null || true
fi
_perm=$(stat -c '%a' "$_target" 2>/dev/null || stat -f '%Lp' "$_target" 2>/dev/null || echo "")

case "$_perm" in
    ''|*[!0-7]*) die "could not read the mode of $_target -- refusing to vouch for the sudoers rule" ;;
esac

case "$_perm" in
    *[2367])  die "$_target is world-writable (mode $_perm) -- this is a root escalation; remove $_sudoers now" ;;
esac
case "$_perm" in
    ?[2367]?) die "$_target is group-writable (mode $_perm) -- this is a root escalation; remove $_sudoers now" ;;
esac
unchanged "quiesce helper permissions ($_perm)"

# logind auto-spawns a getty on any unused low VT and any console write unblanks it, and
# it allocates through autovt@ -- a separate unit name to systemd's mask bookkeeping.
# The symlink is read directly: `systemctl is-enabled` reports the template's mask state.
if is_linux; then
for _u in getty@tty2.service autovt@tty2.service; do
    _link="/etc/systemd/system/$_u"
    if [ -L "$_link" ] && [ "$(readlink "$_link")" = /dev/null ]; then
        unchanged "$_u already masked"
    else
        sudo systemctl mask --now "$_u"
        changed "masked $_u -- tty2 is wk session's VT, not a login prompt's"
    fi
done
fi
unset _u _link

# Only this subdirectory: /var/lib/wk stays root-owned because the card helper keeps the
# board's tailnet node key beside it (TAILNET_KEEP_DIR, 0700 root). Granted here, where a
# password prompt can be answered: `wk boot` records over a BatchMode ssh, no terminal.
_bootdir=/var/lib/wk/boot
_bootowner=$(stat -c '%U' "$_bootdir" 2>/dev/null || stat -f '%Su' "$_bootdir" 2>/dev/null || echo "")
if [ "$_bootowner" = "$(id -un)" ]; then
    unchanged "$_bootdir is writable by $(id -un)"
elif ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
    warn "$_bootdir is not writable by $(id -un): sudo needs a terminal"
    log  "  'wk boot <machine>' cannot record an arming until it is."
    log  "  run this from an interactive shell:  ./setup --stage quiesce"
else
    sudo install -d -o "$(id -un)" -m 0755 "$_bootdir"
    changed "made $_bootdir writable by $(id -un) -- where wk boot records an arming"
fi
unset _bootdir _bootowner

unset _libexec _target _source _sudoers _sudoers_old _needs_install _owner _rule _sudoers_ok _tmp _perm _sessenv _sessline _tmp2
