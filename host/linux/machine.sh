# The workstation itself: storage, identity, and the few things that need root.
# On Linux the workstation is the machine, so this does what the macOS playbook
# does to its podman VM. Root is needed once; after this, no wk command is.

. "$WK_ROOT/lib/store.sh"

_uid=$(id -u)
_gid=$(id -g)
_user=$(id -un)

# Under the user's own data directory (lib/store.sh), so it is unprivileged.
info "store: $WK_STORE"
store_init

# Read back by the egress proxy after `wk pi setup`. Empty is correct here.
if [ -f "$WK_STORE/pi-hosts" ]; then
    unchanged "pi allowlist"
else
    : > "$WK_STORE/pi-hosts"
    changed "created $WK_STORE/pi-hosts"
fi

# The headless marker selects a 2 GB reserve instead of 12 GB: wrong here.
_hm=$(headless_marker)
if [ -f "$_hm" ]; then
    warn "$_hm exists on a workstation -- removing it"
    warn "  it would cut the host reserve from ${WK_RESERVE_MB}MB to ${WK_HEADLESS_RESERVE_MB}MB"
    rm -f "$_hm"
    changed "removed the headless marker"
fi
unset _hm

# Without these, `--userns keep-id` fails at container creation.
if grep -q "^$_user:" /etc/subuid 2>/dev/null && grep -q "^$_user:" /etc/subgid 2>/dev/null; then
    unchanged "subuid/subgid ranges for $_user"
else
    info "adding subordinate id ranges for $_user (needs root once)"
    sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$_user" \
        || die "could not add subuid/subgid ranges"
    changed "subuid/subgid ranges for $_user"
fi

# Cgroup delegation is what makes --memory and --cpus work rootless.
_deleg=$(cat "/sys/fs/cgroup/user.slice/user-$_uid.slice/user@$_uid.service/cgroup.controllers" 2>/dev/null || echo "")
case " $_deleg " in
    *" memory "*) unchanged "cgroup delegation (memory, cpu)" ;;
    *) warn "no delegated memory controller for user@$_uid -- container memory caps will not apply"
       log  "  expected 'memory' in /sys/fs/cgroup/user.slice/user-$_uid.slice/user@$_uid.service/cgroup.controllers" ;;
esac

# /dev/dri/renderD128 is root:render 0660 and logind grants an ACL only to the
# active seat, so the same user over ssh cannot open it. Group membership can.
_want_groups=""
for g in render video; do
    getent group "$g" >/dev/null 2>&1 || continue
    case " $(id -nG "$_user") " in
        *" $g "*) ;;
        *) _want_groups="$_want_groups $g" ;;
    esac
done

if [ -z "$_want_groups" ]; then
    unchanged "render/video group membership"
else
    # Not fatal: what it buys is GPU access from an ssh session.
    if sudo -n true 2>/dev/null || [ -t 0 ]; then
        info "adding $_user to:$_want_groups (needs root once)"
        if sudo usermod -aG "$(echo $_want_groups | tr ' ' ',')" "$_user"; then
            changed "added $_user to:$_want_groups"
            warn "log out and back in for this to take effect in new sessions"
        else
            warn "could not add group membership; GPU access over ssh will fail"
        fi
    else
        warn "not in group(s):$_want_groups -- /dev/dri is unreachable from an ssh session"
        log  "  run once, from a terminal:  sudo usermod -aG $(echo $_want_groups | tr ' ' ',') $_user"
    fi
fi

# Without lingering the systemd --user egress proxy dies with the last login.
if [ "$(loginctl show-user "$_user" -p Linger --value 2>/dev/null)" = yes ]; then
    unchanged "systemd lingering for $_user"
else
    info "enabling systemd lingering for $_user"
    loginctl enable-linger "$_user" 2>/dev/null \
        || sudo loginctl enable-linger "$_user" \
        || warn "could not enable lingering; the egress proxy will stop at logout"
    [ "$(loginctl show-user "$_user" -p Linger --value 2>/dev/null)" = yes ] \
        && changed "enabled lingering for $_user"
fi

unset _uid _gid _user _deleg _want_groups g _missing_keys _remote _repo _alias

# Workspaces share this read-write, so re-seeding every run destroys their edits.
if [ -n "$(ls -A "$WK_STORE/skills" 2>/dev/null)" ]; then
    unchanged "shared skills present (not overwritten)"
    diff -rq "$WK_ROOT/claude/skills" "$WK_STORE/skills" >/dev/null 2>&1 \
        || log "note: shared skills differ from the repo -- 'wk skills status'"
else
    cp -a "$WK_ROOT/claude/skills/." "$WK_STORE/skills/"
    changed "seeded $WK_STORE/skills"
fi

# One deploy key per fork: GitHub refuses the same key on a second repository.
# Reported, never generated: `wk key register` needs a token and a decision.
_missing_keys=""
while read -r _remote _repo _alias; do
    [ -n "$_remote" ] || continue
    # The private half, in the directory nothing mounts (wk_push_held_dir).
    [ -f "$(wk_push_held_dir)/build_key_$_remote" ] || _missing_keys="$_missing_keys $_repo"
done <<EOF
$(wk_push_forks)
EOF

if [ -z "$_missing_keys" ]; then
    unchanged "push keys"
else
    warn "no push key for:$_missing_keys"
    log  "  workspaces can fetch but not push until:  wk key register"
fi
