# NOPASSWD on one absolute path each, so safety reduces to who can write that path:
# root-owned installs, never a symlink into this repo, and an unverifiable mode refuses.
# zzz-: sudo takes the LAST match in /etc/sudoers.d and zz-<user>-passwd re-imposes
# `PASSWD: ALL` over every command, so an earlier name is a dead grant; a dot is skipped.
_libexec=/usr/local/libexec
# macOS has no `root` group; root's is `wheel`. Asked of the platform, not tried and retried.
if is_macos; then _rootgrp=wheel; else _rootgrp=root; fi

# Beside the helper under the name it knows: boot-check never runs a caller-named path.
_check_source="$WK_ROOT/boot/check-boot-files.py"
_check_target="$_libexec/wk-check-boot-files.py"

# The rule is written to a fixed path, overwritten, that only this user and root can write:
# a kill between writing it and installing it leaves one predictable file the next run
# truncates, rather than an unpredictable mktemp name nothing will ever remove, and no
# other account can change the bytes between `visudo -cqf` and the install that trusts it.
_rules_dir="$(wk_state_dir)/priv"

# GNU form first: Linux's `stat -f` succeeds as "filesystem status", never as an owner.
_priv_owner() {   # <path>
    stat -c '%U' "$1" 2>/dev/null || stat -f '%Su' "$1" 2>/dev/null || echo ""
}

_priv_mode() {   # <path>
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null || echo ""
}

# What a helper runs beside itself, and so is part of its state rather than a step of its own.
_priv_companions() {   # <name> -- "<source> <installed path>" per line
    case "$1" in
        wk-card-priv) printf '%s %s\n' "$_check_source" "$_check_target" ;;
    esac
    return 0
}

# The declared final state of one helper, both halves in one predicate: this tree's binary
# installed root-owned and writable by nobody else, and a grant that answers. A rule is
# never compared as text -- it can parse and still be out-ranked by a later include, or
# name a user nobody logs in as -- so what is asked of it is whether `sudo -n` runs the
# helper with no password, which is the only property anything unattended depends on.
_priv_state() {   # <name> -- "<binary> <grant>"
    local name="$1" tgt src bin grant csrc cdst
    tgt="$(wk_priv_path "$name")"
    src="$WK_ROOT/admin/$name"
    if [ ! -f "$tgt" ]; then bin=absent
    elif [ "$(_priv_owner "$tgt")" != root ]; then bin=foreign
    else
        case "$(_priv_mode "$tgt")" in
            ''|*[!0-7]*)      bin=nomode ;;
            *[2367]|?[2367]?) bin=writable ;;
            *) if cmp -s "$src" "$tgt"; then bin=ok; else bin=stale; fi ;;
        esac
    fi
    if [ "$bin" = ok ]; then
        while read -r csrc cdst; do
            [ -n "$cdst" ] || continue
            if [ "$(_priv_owner "$cdst")" != root ] || ! cmp -s "$csrc" "$cdst"; then
                bin=stale
            fi
        done <<COMPANIONS
$(_priv_companions "$name")
COMPANIONS
    fi
    if wk_priv_answers "$tgt"; then grant=ok; else grant=silent; fi
    printf '%s %s\n' "$bin" "$grant"
}

_priv_explain() {   # <name> <binary> <grant>
    local name="$1" bin="$2" grant="$3" tgt sudoers
    tgt="$(wk_priv_path "$name")"
    sudoers="$(wk_priv_sudoers "$name")"
    case "$bin" in
        absent)  log "  $tgt is not installed" ;;
        foreign) log "  $tgt is owned by $(_priv_owner "$tgt"), not root" ;;
        stale)   log "  $tgt is not this tree's copy of admin/$name" ;;
    esac
    if [ "$grant" != ok ]; then
        log "  'sudo -n $tgt status' asks for a password, so nothing unattended can use it."
        log "  $sudoers has to be the last match 'sudo -l' shows, and name that path"
        log "  character for character."
    fi
    return 0
}

# One repair, from any starting point -- absent, stale, wrong owner, no rule, a rule naming
# another user, a rule a later include out-ranks -- and safe to run when the state is
# already right: it installs rather than deciding a second time what is missing, so a kill
# anywhere in it leaves a state the next run converges from.
_priv_repair() {   # <name> <binary verdict before>
    local name="$1" bin="$2" src tgt sudoers old cand csrc cdst
    src="$WK_ROOT/admin/$name"
    tgt="$(wk_priv_path "$name")"
    sudoers="$(wk_priv_sudoers "$name")"
    old="${sudoers%/*}/${sudoers##*/zzz-}"
    cand="$_rules_dir/$name.rule"

    sudo install -d -o root -g "$_rootgrp" -m 0755 "$_libexec"
    sudo install -o root -m 0755 "$src" "$tgt"
    while read -r csrc cdst; do
        [ -n "$cdst" ] || continue
        sudo install -o root -m 0644 "$csrc" "$cdst"
    done <<COMPANIONS
$(_priv_companions "$name")
COMPANIONS
    [ "$bin" = ok ] || changed "installed $tgt"

    install -d -m 0700 "$_rules_dir"
    printf '%s\n' "$(id -un) ALL=(root) NOPASSWD: $tgt" > "$cand"
    # An invalid sudoers file locks the account out of sudo entirely.
    if ! sudo visudo -cqf "$cand"; then
        rm -f "$cand"
        die "the sudoers rule generated for $name failed validation; $sudoers is unchanged"
    fi
    if sudo cmp -s "$cand" "$sudoers"; then
        unchanged "$sudoers already carries this rule"
    else
        sudo install -o root -m 0440 "$cand" "$sudoers"
        changed "installed $sudoers"
    fi
    rm -f "$cand"
    # Tombstone: an out-ranked second grant of the same path still reads as in force.
    if [ -f "$old" ]; then
        sudo rm -f "$old"
        changed "removed $old (it sorted before zz-<user>-passwd and was dead)"
    fi
    return 0
}

_priv_converge() {   # <name> <platform> <what it is for>
    local name="$1" where="$2" what="$3" src tgt sudoers state bin grant
    if [ "$where" = linux ] && ! is_linux; then
        unchanged "$name ($where only)"
        return 0
    fi
    src="$WK_ROOT/admin/$name"
    if [ ! -f "$src" ]; then
        warn "$name is missing at $src; skipping"
        return 0
    fi
    tgt="$(wk_priv_path "$name")"
    sudoers="$(wk_priv_sudoers "$name")"

    state="$(_priv_state "$name")"
    bin="${state% *}"
    grant="${state#* }"
    # Writable by anyone but root is a root escalation, and the remedy is to take the grant
    # away now rather than to install this tree's copy over whatever is there.
    case "$bin" in
        nomode)   die "could not read the mode of $tgt -- refusing to vouch for $sudoers" ;;
        writable) die "$tgt is writable by more than root (mode $(_priv_mode "$tgt")) -- this is a root escalation; remove $sudoers now" ;;
    esac
    if [ "$bin" = ok ] && [ "$grant" = ok ]; then
        unchanged "$name and $sudoers"
        return 0
    fi
    if ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
        warn "$name ($what) is not in force, and repairing it needs sudo on a terminal"
        _priv_explain "$name" "$bin" "$grant"
        log  "  run this from an interactive shell:  ./setup --stage quiesce"
        return 0
    fi
    info "installing $name -- $what (requires sudo once)"
    _priv_repair "$name" "$bin"

    # Asked again, of the machine, now: a rule that copied without error can still be
    # out-ranked or name a path character-for-character different from the one being run.
    state="$(_priv_state "$name")"
    bin="${state% *}"
    grant="${state#* }"
    if [ "$bin" = ok ] && [ "$grant" = ok ]; then
        unchanged "$name answers"
        return 0
    fi
    warn "$name is installed and its grant does not answer:"
    _priv_explain "$name" "$bin" "$grant"
    return 0
}

while read -r _pname _pwhere _pwhat; do
    [ -n "$_pname" ] || continue
    _priv_converge "$_pname" "$_pwhere" "$_pwhat"
done <<ROWS
$(wk_priv_helpers)
ROWS
unset _pname _pwhere _pwhat

# Apple Silicon signs the startup-disk choice with a volume owner's credential, and `bless
# --help` lists --user/--stdinpass under Snapshot options rather than Mount Mode, so whether
# root alone suffices is the platform's answer: the helper blesses with a credential where
# the machine holds one and without one where it does not, and says which it used. Keeping a
# login password on disk stays the owner's call; nothing here creates that file.
if is_macos && [ ! -f /usr/local/share/wk-bench/owner-password ]; then
    log "  no volume-owner credential here, which may not be needed: 'wk boot mbp'"
    log "  blesses with root alone and reports what bless answered."
fi

# Tombstones: without these an older revision's root-owned file and dead grant stay.
_retired="$_libexec/wk-tftpd /etc/sudoers.d/wk-netboot"
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
_sesscand="$_rules_dir/wk-session.env"

if [ -f "$(wk_priv_path wk-quiesce-priv)" ]; then
    if sudo -n grep -qxF "$_sessline" "$_sessenv" 2>/dev/null \
       || grep -qxF "$_sessline" "$_sessenv" 2>/dev/null; then
        unchanged "session user"
    else
        install -d -m 0700 "$_rules_dir"
        printf '%s\n' "$_sessline" > "$_sesscand"
        if sudo install -o root -m 0644 "$_sesscand" "$_sessenv" 2>/dev/null; then
            changed "recorded the session user in $_sessenv"
        else
            warn "could not write $_sessenv; 'wk session' will refuse to start"
        fi
        rm -f "$_sesscand"
    fi
fi
unset _sessenv _sessline _sesscand

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

unset _libexec _rootgrp _check_source _check_target _rules_dir
