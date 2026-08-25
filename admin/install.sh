# Install the privileged quiesce helper and its sudoers rule.
#
# The rule grants NOPASSWD for one absolute path, so the security of the whole
# arrangement reduces to: can anyone but root modify that path? The install
# below is written to make the answer no, and to refuse rather than proceed if
# it cannot guarantee that.
#
# The helper is *copied* to a root-owned location, never symlinked into the
# repo. A sudoers rule pointing at a file inside a user-writable git checkout
# would be a trivially exploitable root escalation -- editing the file, or
# checking out a branch, would change what runs as root.

_libexec=/usr/local/libexec
_target="$_libexec/wk-quiesce-priv"
_source="$WK_ROOT/admin/wk-quiesce-priv"
_sudoers=/etc/sudoers.d/wk-quiesce

if [ ! -f "$_source" ]; then
    warn "quiesce helper missing at $_source; skipping"
    return 0 2>/dev/null || true
fi

_needs_install=0
if [ ! -f "$_target" ] || ! cmp -s "$_source" "$_target"; then
    _needs_install=1
fi

# GNU form first, BSD second. The other order is subtly broken on Linux:
# `stat -f '%Su' FILE` does not fail there, it interprets -f as "file system
# status" and prints a block of filesystem information instead -- so the owner
# came back as a multi-line blob, never equalled "root", and the helper was
# reinstalled on every single run.
_owner=$(stat -c '%U' "$_target" 2>/dev/null || stat -f '%Su' "$_target" 2>/dev/null || echo "")
[ -f "$_target" ] && [ "$_owner" != root ] && _needs_install=1

_rule="$(id -un) ALL=(root) NOPASSWD: $_target"

# Whether the rule works, not whether it reads a particular way. The rule
# grants exactly one path, so `sudo -n test -r /etc/sudoers.d/wk-quiesce` is
# itself denied -- and reading the failure as "not installed" made every
# non-interactive run report the helper missing while it sat there working.
_sudoers_ok=0
if [ -x "$_target" ] && sudo -n "$_target" status >/dev/null 2>&1; then
    _sudoers_ok=1
fi

if [ "$_needs_install" -eq 0 ] && [ "$_sudoers_ok" -eq 1 ]; then
    unchanged "quiesce helper and sudoers rule"
elif ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
    # Installing needs a real sudo prompt. Skip rather than abort: this stage
    # is independent of everything else, and failing the whole setup over a
    # benchmarking convenience would be the wrong trade.
    warn "quiesce helper not installed: sudo needs a terminal"
    log  "  run this from an interactive shell:  ./setup --stage quiesce"
else
    info "installing the quiesce helper (requires sudo once)"

    sudo install -d -o root -g wheel -m 0755 "$_libexec" 2>/dev/null \
        || sudo install -d -o root -g root -m 0755 "$_libexec"

    # 0755 root-owned: readable and executable by all, writable only by root.
    sudo install -o root -m 0755 "$_source" "$_target"
    changed "installed $_target"

    # Validate before installing. An invalid sudoers file can lock the account
    # out of sudo entirely, so this is written to a temp path, checked with
    # visudo, and only then moved into place.
    _tmp="$(mktemp)"
    printf '%s\n' "$_rule" > "$_tmp"

    if sudo visudo -cqf "$_tmp"; then
        sudo install -o root -m 0440 "$_tmp" "$_sudoers"
        changed "installed $_sudoers"
    else
        rm -f "$_tmp"
        die "generated sudoers rule failed validation; nothing was installed"
    fi
    rm -f "$_tmp"
fi

# --- the card helper ----------------------------------------------------------
#
# The second privileged helper, and the second NOPASSWD grant in this repo. The
# rule against adding these is in CLAUDE.md and it stands; what earns the
# exception is the same thing that earned the first one -- a fixed verb list, no
# passthrough, and a gate that is narrower than the thing it protects. This one
# may only touch a **usb or mmc whole disk that the machine is not running
# from**, which is a smaller grant than "write a card" sounds like: the fleet's
# boards boot from SD cards and USB sticks, so the transport check alone would
# happily overwrite a running system, and the boot check is what makes the grant
# safe rather than merely plausible.
#
# Installed on the machines that hold card readers. Everywhere else it is absent
# and `boot/disk.sh` falls back to plain sudo, which is what works on a board
# where the account is already root.
_card_target="$_libexec/wk-card-priv"
_card_source="$WK_ROOT/admin/wk-card-priv"
_card_sudoers=/etc/sudoers.d/wk-card

if [ ! -f "$_card_source" ]; then
    warn "card helper missing at $_card_source; skipping"
elif ! is_linux; then
    # macOS holds no card readers in this fleet, and its `dd`/`lsblk` are not
    # the ones this helper is written against. Absent rather than half-working.
    unchanged "card helper (linux only)"
else
    _card_needs=0
    if [ ! -f "$_card_target" ] || ! cmp -s "$_card_source" "$_card_target"; then _card_needs=1; fi
    _card_owner=$(stat -c '%U' "$_card_target" 2>/dev/null || echo "")
    [ -f "$_card_target" ] && [ "$_card_owner" != root ] && _card_needs=1

    _card_ok=0
    if [ -x "$_card_target" ] && sudo -n "$_card_target" status >/dev/null 2>&1; then _card_ok=1; fi

    if [ "$_card_needs" -eq 0 ] && [ "$_card_ok" -eq 1 ]; then
        unchanged "card helper and sudoers rule"
    elif ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
        warn "card helper not installed: sudo needs a terminal"
        log  "  run this from an interactive shell:  ./setup --stage quiesce"
    else
        info "installing the card helper (requires sudo once)"
        sudo install -d -o root -g root -m 0755 "$_libexec"
        sudo install -o root -m 0755 "$_card_source" "$_card_target"
        changed "installed $_card_target"

        _card_tmp="$(mktemp)"
        printf '%s\n' "$(id -un) ALL=(root) NOPASSWD: $_card_target" > "$_card_tmp"
        if sudo visudo -cqf "$_card_tmp"; then
            sudo install -o root -m 0440 "$_card_tmp" "$_card_sudoers"
            changed "installed $_card_sudoers"
        else
            rm -f "$_card_tmp"
            die "generated card sudoers rule failed validation; nothing was installed"
        fi
        rm -f "$_card_tmp"
    fi

    # The same guarantee the rule depends on, checked every run: a helper anyone
    # but root can write is a root escalation, not a helper.
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
unset _card_target _card_source _card_sudoers _card_needs _card_owner _card_ok _card_tmp _card_perm

# --- privileged files this repo has retired -----------------------------------
#
# Anything this stage installed in the past and no longer installs. A machine
# provisioned by an older revision keeps it otherwise -- a root-owned file and a
# sudoers grant that nothing uses, which is the worst kind of leftover: it is
# privilege with no owner. Named literally, because removing a file means naming
# it. Entries come out of this list once every machine in the fleet is clean.
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
# The helper's session verbs run a compositor as a specific user. That user is
# recorded here, root-owned, rather than passed as an argument: an argument
# would widen a fixed allowlist into "start a graphical session as anybody",
# which is a materially larger grant than the rest of this file.
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

# Verify the guarantee the sudoers rule depends on, every run. A world- or
# group-writable helper would mean anyone who can write it gets root.
if [ ! -f "$_target" ]; then
    unset _libexec _target _source _sudoers _needs_install _owner _rule _sudoers_ok _tmp
    return 0 2>/dev/null || true
fi
_perm=$(stat -c '%a' "$_target" 2>/dev/null || stat -f '%Lp' "$_target" 2>/dev/null || echo "")

# The same argument-order bug made this check inert on Linux, which matters far
# more here than the reinstall did: these two cases are the entire argument that
# the NOPASSWD rule is safe, and they were being run against a block of
# filesystem statistics that could never match either pattern. An unverifiable
# mode is a refusal, not a pass.
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
# WK_SESSION_TTY (tty2, hardcoded in wk-quiesce-priv's own default -- sudo's
# env_reset means an override can never actually reach it) is meant to be
# cage's alone while a session is running, and nobody's the rest of the time.
# systemd doesn't know that: logind auto-spawns a getty on any of its first
# few VTs the moment nothing else is using them, and the moment cage lets go
# of tty2, a getty and its login prompt showed up there instead. That's a
# console write, and any console write auto-unblanks the VT -- which is why
# `wk session off` blanking the screen and a getty immediately re-lighting it
# with a login prompt looked exactly like blanking not working at all.
#
# Both unit names, not just one: /usr/lib/systemd/system/autovt@.service is a
# symlink to the very same getty@.service template, but it is a *different
# unit name* to systemd's mask bookkeeping -- and logind's on-demand VT
# allocation instantiates gettys through the autovt@ name specifically, not
# getty@, so masking only getty@tty2.service leaves logind free to start
# autovt@tty2.service right past it. Confirmed live: masking just getty@
# still left a login prompt lighting tty2 back up every time.
#
# `systemctl is-enabled` is not the idempotency check here, and using it was
# its own bug: for an aliased template instance it reports the *resolved*
# template file's mask state, not this specific instance's own -- so it
# printed "masked" for autovt@tty2.service, and the mask this loop exists to
# create was silently skipped, even though the unit was demonstrably still
# starting. /etc/systemd/system/<unit> -> /dev/null is what masking actually
# is, so that symlink is the fact this checks instead.
#
# Linux only, and it had no guard: on macOS this ran `sudo systemctl mask` and
# failed the whole quiesce stage with `sudo: systemctl: command not found` --
# after the helper and its sudoers rule had already installed correctly. So the
# stage reported failure for a machine it had finished provisioning, which is
# the kind of error that gets a working setup re-run and re-debugged. tty2 and
# getty units do not exist on macOS; there is nothing here to do there.
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

unset _libexec _target _source _sudoers _needs_install _owner _rule _sudoers_ok _tmp _perm _sessenv _sessline _tmp2
