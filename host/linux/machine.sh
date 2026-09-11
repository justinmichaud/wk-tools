. "$WK_ROOT/lib/store.sh"

_uid=$(id -u)
_gid=$(id -g)
_user=$(id -un)

info "store: $WK_STORE"
store_init

if [ -f "$WK_STORE/pi-hosts" ]; then
    unchanged "pi allowlist"
elif [ -n "${WK_DRY_RUN:-}" ]; then
    changed "would create $WK_STORE/pi-hosts"
else
    : > "$WK_STORE/pi-hosts"
    changed "created $WK_STORE/pi-hosts"
fi

_hm=$(headless_marker)
if [ -f "$_hm" ]; then
    warn "$_hm exists on a workstation"
    warn "  it would cut the host reserve from ${WK_RESERVE_MB}MB to ${WK_HEADLESS_RESERVE_MB}MB"
    if [ -n "${WK_DRY_RUN:-}" ]; then
        changed "would remove the headless marker (rm -f $_hm)"
    else
        rm -f "$_hm"
        changed "removed the headless marker"
    fi
fi
unset _hm

# Without these, `--userns keep-id` fails at container creation.
if grep -q "^$_user:" /etc/subuid 2>/dev/null && grep -q "^$_user:" /etc/subgid 2>/dev/null; then
    unchanged "subuid/subgid ranges for $_user"
elif [ -n "${WK_DRY_RUN:-}" ]; then
    changed "would add subordinate id ranges (sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $_user)"
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
    if [ -n "${WK_DRY_RUN:-}" ]; then
        changed "would add $_user to:$_want_groups (sudo usermod -aG $(echo $_want_groups | tr ' ' ',') $_user)"
    elif sudo -n true 2>/dev/null || [ -t 0 ]; then
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
elif [ -n "${WK_DRY_RUN:-}" ]; then
    changed "would enable systemd lingering (sudo loginctl enable-linger $_user)"
else
    info "enabling systemd lingering for $_user (needs root once)"
    sudo loginctl enable-linger "$_user" \
        || die "could not enable lingering for $_user, so the egress proxy stops at
    logout.  Run once, from a terminal:  sudo loginctl enable-linger $_user"
    [ "$(loginctl show-user "$_user" -p Linger --value 2>/dev/null)" = yes ] \
        || die "'sudo loginctl enable-linger $_user' reported success and this user's
    Linger is still off, so the egress proxy stops at logout.  Ask systemd why:
    loginctl show-user $_user"
    changed "enabled lingering for $_user"
fi

unset _uid _gid _user _deleg _want_groups g _missing_keys _remote _repo _alias

if [ -n "$(ls -A "$WK_STORE/skills" 2>/dev/null)" ]; then
    unchanged "shared skills present (not overwritten)"
    diff -rq "$WK_ROOT/claude/skills" "$WK_STORE/skills" >/dev/null 2>&1 \
        || log "note: shared skills differ from the repo -- 'wk skills status'"
elif [ -n "${WK_DRY_RUN:-}" ]; then
    changed "would seed $WK_STORE/skills from $WK_ROOT/claude/skills"
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
    log  "  workspaces can fetch but not push until:  wk key deploy"
fi

# A hardware fact, not a case naming a hostname: any Raspberry Pi 5 gets this.
if grep -aqs '^Raspberry Pi 5' /proc/device-tree/model 2>/dev/null; then
    _rpi5_log="$WK_STORE/log/rpi5-setup.log"   # run in the foreground, since it asks sudo for a password: captured, the prompt stands there with none of the steps that explain it. Its ✓ says a step is in the state it wants, not that this run put it there, so a change is the two lines it prints only when it writes something new
    if [ -n "${WK_DRY_RUN:-}" ]; then
        changed "would run this board's tuning tree (bash $WK_ROOT/host/linux/rpi5/rpi5-setup.sh)"
    else
        info "Raspberry Pi 5 detected -- running its tuning tree"
        ensure_dir "$(dirname "$_rpi5_log")" >/dev/null
        if bash "$WK_ROOT/host/linux/rpi5/rpi5-setup.sh" 2>&1 | tee "$_rpi5_log" >&2; then
            if grep -qE '(added:|appended tuning block)' "$_rpi5_log"; then
                changed "rpi5-setup.sh wrote this board's tuning ($_rpi5_log)"
            else
                unchanged "rpi5 tuning tree, already applied ($_rpi5_log)"
            fi
        else
            warn "rpi5-setup.sh exited non-zero -- $_rpi5_log says at which step"
        fi
    fi
    unset _rpi5_log
fi
