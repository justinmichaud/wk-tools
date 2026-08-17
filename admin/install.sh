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

_owner=$(stat -f '%Su' "$_target" 2>/dev/null || stat -c '%U' "$_target" 2>/dev/null || echo "")
[ -f "$_target" ] && [ "$_owner" != root ] && _needs_install=1

_rule="$(id -un) ALL=(root) NOPASSWD: $_target"

_sudoers_ok=0
if [ -f "$_sudoers" ] && sudo -n test -r "$_sudoers" 2>/dev/null; then
    sudo -n grep -qxF "$_rule" "$_sudoers" 2>/dev/null && _sudoers_ok=1
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

# Verify the guarantee the sudoers rule depends on, every run. A world- or
# group-writable helper would mean anyone who can write it gets root.
if [ ! -f "$_target" ]; then
    unset _libexec _target _source _sudoers _needs_install _owner _rule _sudoers_ok _tmp
    return 0 2>/dev/null || true
fi
_perm=$(stat -f '%Lp' "$_target" 2>/dev/null || stat -c '%a' "$_target" 2>/dev/null || echo 000)
case "$_perm" in
    *[2367])  die "$_target is world-writable (mode $_perm) -- this is a root escalation; remove $_sudoers now" ;;
esac
case "$_perm" in
    ?[2367]?) die "$_target is group-writable (mode $_perm) -- this is a root escalation; remove $_sudoers now" ;;
esac
unchanged "quiesce helper permissions ($_perm)"

unset _libexec _target _source _sudoers _needs_install _owner _rule _sudoers_ok _tmp _perm
