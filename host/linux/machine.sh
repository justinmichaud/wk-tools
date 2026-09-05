. "$WK_ROOT/lib/store.sh"

_uid=$(id -u)
_gid=$(id -g)
_user=$(id -un)

info "store: $WK_STORE"
store_init

if [ -f "$WK_STORE/pi-hosts" ]; then
    unchanged "pi allowlist"
else
    : > "$WK_STORE/pi-hosts"
    changed "created $WK_STORE/pi-hosts"
fi

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

# renderD128 is root:render 0660 and logind's ACL covers only the active seat.
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

if [ -n "$(ls -A "$WK_STORE/skills" 2>/dev/null)" ]; then
    unchanged "shared skills present (not overwritten)"
    diff -rq "$WK_ROOT/claude/skills" "$WK_STORE/skills" >/dev/null 2>&1 \
        || log "note: shared skills differ from the repo -- 'wk skills status'"
else
    cp -a "$WK_ROOT/claude/skills/." "$WK_STORE/skills/"
    changed "seeded $WK_STORE/skills"
fi

# One deploy key per fork: GitHub refuses the same key on a second repository.
_missing_keys=""
while read -r _remote _repo _alias; do
    [ -n "$_remote" ] || continue
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
