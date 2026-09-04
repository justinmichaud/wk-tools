# Install the privileged quiesce helper and its sudoers rule.
#
# The rule grants NOPASSWD for one absolute path, so security reduces to: can
# anyone but root modify that path? This refuses rather than proceeds if it
# cannot guarantee no. The helper is copied to a root-owned location, never
# symlinked into the repo, which would be a root escalation.

# --- why the file names begin zzz- -------------------------------------------
# sudo reads /etc/sudoers.d in lexical order and takes the LAST matching rule.
# `wk sudo setup` installs `zz-<user>-passwd`, re-imposing `PASSWD: ALL` over a
# site's blanket `NOPASSWD: ALL`, and `ALL` matches every command -- so a name
# sorting before it has a dead grant. `zzz-` sorts after it under LC_ALL=C. No
# dot in either name, since sudo skips those.
_libexec=/usr/local/libexec
_target="$_libexec/wk-quiesce-priv"
_source="$WK_ROOT/admin/wk-quiesce-priv"
_sudoers=/etc/sudoers.d/zzz-wk-quiesce
# Tombstone: the name these rules carried on a machine provisioned earlier. A
# second grant of the same path reads as in force even out-ranked.
_sudoers_old=/etc/sudoers.d/wk-quiesce

if [ ! -f "$_source" ]; then
    warn "quiesce helper missing at $_source; skipping"
    return 0 2>/dev/null || true
fi

_needs_install=0
if [ ! -f "$_target" ] || ! cmp -s "$_source" "$_target"; then
    _needs_install=1
fi

# GNU form first, BSD second: `stat -f '%Su' FILE` on Linux does not fail, it
# reads -f as "file system status", so the wrong order never equals "root".
_owner=$(stat -c '%U' "$_target" 2>/dev/null || stat -f '%Su' "$_target" 2>/dev/null || echo "")
[ -f "$_target" ] && [ "$_owner" != root ] && _needs_install=1

_rule="$(id -un) ALL=(root) NOPASSWD: $_target"

# Whether the rule works, not how it reads: it grants exactly one path, so
# `sudo -n test -r "$_sudoers"` is itself denied.
_sudoers_ok=0
if [ -x "$_target" ] && sudo -n "$_target" status >/dev/null 2>&1; then
    _sudoers_ok=1
fi

if [ "$_needs_install" -eq 0 ] && [ "$_sudoers_ok" -eq 1 ]; then
    unchanged "quiesce helper and sudoers rule"
elif ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
    # Installing needs a real sudo prompt, so this skips rather than aborts.
    # The two faults are reported apart because they have different fixes.
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
        # Here, not earlier: this branch already has sudo, and a
        # `sudo -n true` guard is false on exactly the affected machines.
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

# --- the card helper ----------------------------------------------------------
# The second privileged helper: a fixed verb list, no passthrough, and a gate
# narrower than the thing it protects -- only a usb or mmc whole disk the
# machine is not running from. The transport check alone would happily overwrite
# a running system; the boot check is what makes the grant safe. Installed only
# on machines with card readers; elsewhere `boot/disk.sh` refuses.
_card_target="$_libexec/wk-card-priv"
_card_source="$WK_ROOT/admin/wk-card-priv"
# The boot-file checker the helper's `boot-check` runs as root, beside it under
# the name the helper knows (CHECK_BOOT_FILES), never at a path a caller names.
_check_target="$_libexec/wk-check-boot-files.py"
_check_source="$WK_ROOT/boot/check-boot-files.py"
_card_sudoers=/etc/sudoers.d/zzz-wk-card
_card_sudoers_old=/etc/sudoers.d/wk-card

if [ ! -f "$_card_source" ]; then
    warn "card helper missing at $_card_source; skipping"
elif ! is_linux; then
    # macOS holds no card readers in this fleet.
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
        # Installed but out-ranked has a different fix than absent.
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

# --- privileged files this repo has retired -----------------------------------
# Tombstones. Without them a machine provisioned by an older revision keeps a
# root-owned file and a sudoers grant nothing uses. Entries come out of this
# list once every machine is clean.
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

# --- the session user --------------------------------------------------------
# The helper's session verbs run a compositor as a specific user, recorded here
# root-owned: an argument would widen the allowlist into "as anybody".
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

# A world/group-writable helper means anyone who can write it gets root.
if [ ! -f "$_target" ]; then
    unset _libexec _target _source _sudoers _sudoers_old _needs_install _owner _rule _sudoers_ok _tmp
    return 0 2>/dev/null || true
fi
_perm=$(stat -c '%a' "$_target" 2>/dev/null || stat -f '%Lp' "$_target" 2>/dev/null || echo "")

# The GNU/BSD order above applies here too. An unverifiable mode is a refusal.
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

# --- keep the benchmark session's VT to itself -------------------------------
# WK_SESSION_TTY (tty2) is cage's alone while a session runs, but logind
# auto-spawns a getty on any unused low VT, and any console write -- a getty's
# login prompt included -- auto-unblanks it.
#
# Both unit names: autovt@.service symlinks to the same getty@.service template
# but is a different unit name to systemd's mask bookkeeping, and logind's
# on-demand VT allocation goes through autovt@ specifically.
#
# The symlink /etc/systemd/system/<unit> -> /dev/null is checked directly rather
# than with `systemctl is-enabled`, which reports an aliased template instance's
# resolved template mask state, not the instance's own.
#
# Linux only: tty2/getty units do not exist on macOS.
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

unset _libexec _target _source _sudoers _sudoers_old _needs_install _owner _rule _sudoers_ok _tmp _perm _sessenv _sessline _tmp2
