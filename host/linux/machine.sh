# The workstation itself: storage, identity, and the few things that need root.
#
# There is no VM here. The macOS side has a podman machine as the isolation
# boundary and an ansible playbook to provision it; on Linux the workstation is
# the machine, so this file does what the playbook does -- and nothing more.
#
# Everything below is written so that root is needed exactly once, on a fresh
# machine, from an interactive `./setup`. After this stage completes, no wk
# command needs a privilege: workspaces are rootless podman, the egress boundary
# is a user-owned proxy, and the only privileged operations left (quiesce, the
# benchmark session) go through the fixed-allowlist helper in admin/.
#
# That was a deliberate reversal of the old macOS design, where rootful podman
# was mandatory because the egress firewall lived in the forward chain --
# rootless podman has no filterable forward path at all (its network helper
# sits in a randomly named cgroup scope), so the boundary moved to
# `--network none` plus a proxy, and root moved out of the daily path with it.
# macOS has since adopted the same model (targets/container.sh, WK_SANDBOX).
# See docs/HANDOFF-linux.md.

# The storage model, for WK_STORE and store_init. setup itself only loads
# common.sh and resources.sh, since the macOS stages need nothing else.
. "$WK_ROOT/lib/store.sh"

_uid=$(id -u)
_gid=$(id -g)
_user=$(id -un)

# --- the store ---------------------------------------------------------------
# Under the user's own data directory (see lib/store.sh), so creating it,
# repairing it and deleting it are all unprivileged. Nothing in it is
# system state: it is a git mirror, some snapshots, and caches.
info "store: $WK_STORE"
store_init

# Read back by the egress proxy after `wk pi setup`. Empty is correct here.
if [ -f "$WK_STORE/pi-hosts" ]; then
    unchanged "pi allowlist"
else
    : > "$WK_STORE/pi-hosts"
    changed "created $WK_STORE/pi-hosts"
fi

# The headless marker selects a 2 GB reserve instead of 12 GB. That is right for
# a VM with no desktop and wrong for a workstation with a monitor attached --
# the whole point of the reserve is that the GUI stays interactive under a full
# build.
if [ -f "$WK_STORE/.headless" ]; then
    warn "$WK_STORE/.headless exists on a workstation -- removing it"
    warn "  it would cut the host reserve from ${WK_RESERVE_MB}MB to ${WK_HEADLESS_RESERVE_MB}MB"
    rm -f "$WK_STORE/.headless"
    changed "removed the headless marker"
fi

# --- rootless podman prerequisites -------------------------------------------
# Subordinate id ranges. Without these rootless podman cannot map any user but
# the invoking one, and `--userns keep-id` fails at container creation.
if grep -q "^$_user:" /etc/subuid 2>/dev/null && grep -q "^$_user:" /etc/subgid 2>/dev/null; then
    unchanged "subuid/subgid ranges for $_user"
else
    info "adding subordinate id ranges for $_user (needs root once)"
    sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$_user" \
        || die "could not add subuid/subgid ranges"
    changed "subuid/subgid ranges for $_user"
fi

# Cgroup delegation, which is what makes --memory and --cpus work rootless. Not
# fatal if absent: the build still runs, it just loses the cap that keeps it
# from taking the machine down with it, so say so loudly rather than proceed
# silently.
_deleg=$(cat "/sys/fs/cgroup/user.slice/user-$_uid.slice/user@$_uid.service/cgroup.controllers" 2>/dev/null || echo "")
case " $_deleg " in
    *" memory "*) unchanged "cgroup delegation (memory, cpu)" ;;
    *) warn "no delegated memory controller for user@$_uid -- container memory caps will not apply"
       log  "  expected 'memory' in /sys/fs/cgroup/user.slice/user-$_uid.slice/user@$_uid.service/cgroup.controllers" ;;
esac

# --- GPU device access -------------------------------------------------------
# /dev/dri/renderD128 is root:render 0660. logind grants an ACL to whoever holds
# the active seat, so a user sitting at the machine can open it and the same
# user over ssh cannot -- which is exactly the case that matters here, because
# benchmark runs are driven remotely. Group membership makes access independent
# of who is logged in at the console.
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
    # The only privileged step in the whole setup, and it is not fatal: logind
    # also grants an ACL on the render node to whoever holds the active seat,
    # so a benchmark session started at the machine works without it. What it
    # buys is GPU access from an ssh session, which is how unattended runs
    # actually happen.
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

# --- lingering ---------------------------------------------------------------
# The egress proxy is a systemd --user service. Without lingering it dies with
# the last login session, which would break exactly the unattended runs this is
# meant to support.
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

# --- shared mutable skills ---------------------------------------------------
# Seeded from the repo once, then left alone. Workspaces share this directory
# read-write and are expected to edit it, so re-seeding on every setup run would
# silently destroy their work. `wk skills pull` is how edits come back.
if [ -n "$(ls -A "$WK_STORE/skills" 2>/dev/null)" ]; then
    unchanged "shared skills present (not overwritten)"
    diff -rq "$WK_ROOT/claude/skills" "$WK_STORE/skills" >/dev/null 2>&1 \
        || log "note: shared skills differ from the repo -- 'wk skills status'"
else
    cp -a "$WK_ROOT/claude/skills/." "$WK_STORE/skills/"
    changed "seeded $WK_STORE/skills"
fi

# --- build keys --------------------------------------------------------------
# One deploy key per fork, because GitHub refuses the same key on a second
# repository. Only reported here, never generated: `wk key register` creates
# and registers them through the GitHub API in one step, and that needs a token
# and a decision, neither of which belongs in an unattended setup stage.
_missing_keys=""
while read -r _remote _repo _alias; do
    [ -n "$_remote" ] || continue
    [ -f "$WK_STORE/secrets/build_key_$_remote" ] || _missing_keys="$_missing_keys $_repo"
done <<EOF
$(wk_push_forks)
EOF

if [ -z "$_missing_keys" ]; then
    unchanged "push keys"
else
    warn "no push key for:$_missing_keys"
    log  "  workspaces can fetch but not push until:  wk key register"
fi
