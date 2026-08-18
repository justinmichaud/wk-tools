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

unset _libexec _target _source _sudoers _needs_install _owner _rule _sudoers_ok _tmp _perm _sessenv _sessline _tmp2
