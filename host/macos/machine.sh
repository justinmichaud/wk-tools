# The podman machine that hosts every workspace.
#
# Three properties matter, and each is verified rather than assumed:
#
#   no volume mounts  podman machine mounts /Users, /private and /var/folders
#                     by default. Workspaces need none of them, and removing
#                     them is what makes "no host filesystem access" a
#                     structural property instead of a convention.
#
#   rootful           the workspace firewall is nftables in the VM's root
#                     network namespace. Rootless podman uses pasta, which
#                     re-emits container traffic as ordinary sockets from the
#                     init namespace -- it never traverses the forward chain,
#                     so there is nothing to filter on.
#
#   bounded resources the VM must not be able to starve the desktop.

WK_MACHINE="${WK_MACHINE:-wk}"

# --- retire other machines ---------------------------------------------------
# applehv permits exactly one running VM, so a leftover machine is not merely
# wasted disk: it blocks `wk` from starting with "only one VM can be active at
# a time". Machine storage is per-machine, so nothing here is shared with the
# wk machine and removing it loses only that machine's own images.
_others=$(podman machine list --format json 2>/dev/null \
    | python3 -c "
import json,sys
try: ms = json.load(sys.stdin)
except Exception: sys.exit(0)
for m in ms:
    n = (m.get('Name') or '').rstrip('*')
    if n and n != '$WK_MACHINE':
        print(n)
" 2>/dev/null || true)

for _m in $_others; do
    _img="$HOME/.local/share/containers/podman/machine/applehv/${_m}-arm64.raw"
    _sz=$(du -sh "$_img" 2>/dev/null | cut -f1 || echo "?")
    warn "obsolete podman machine '$_m' ($_sz) blocks the wk machine from starting"
    if confirm "  remove machine '$_m' and its images? this cannot be undone"; then
        podman machine stop "$_m" >/dev/null 2>&1 || true
        podman machine rm -f "$_m" >/dev/null
        changed "removed podman machine '$_m' (reclaimed $_sz)"
    else
        warn "keeping '$_m' -- 'wk' will fail to start while it is running"
    fi
done
unset _others _m _img _sz

_cores=$(envelope_cores)
_mem=$(envelope_mem_mb)
_disk="${WK_DISK_GB:-200}"

if podman machine inspect "$WK_MACHINE" >/dev/null 2>&1; then
    unchanged "podman machine '$WK_MACHINE' exists"
else
    info "creating podman machine '$WK_MACHINE' (${_cores} cpus, ${_mem} MiB, ${_disk} GiB)"
    log  "this downloads a Fedora CoreOS image and takes a few minutes"

    # An explicit empty --volume overrides the default mount list. Verified
    # below, because a silent fallback to the defaults would quietly hand every
    # workspace read-write access to the entire home directory.
    podman machine init "$WK_MACHINE" \
        --cpus "$_cores" \
        --memory "$_mem" \
        --disk-size "$_disk" \
        --rootful \
        --volume "" \
        --playbook "$WK_ROOT/host/macos/playbook.yaml"

    changed "created podman machine '$WK_MACHINE'"
fi

# --- verify isolation --------------------------------------------------------
# Refuse to continue if the VM can see the host. This is the single most
# important invariant in the design, so it is checked on every setup run rather
# than trusted from creation time.
#
# Read from the machine's config file, not `podman machine inspect`: inspect
# does not expose Mounts at all (podman 5.4), so a --format query silently
# yields nothing and the check would pass by accident -- which is worse than no
# check, because it reports a guarantee it never verified.
_cfg="$HOME/.config/containers/podman/machine/applehv/$WK_MACHINE.json"
_mounts=$(python3 -c "
import json,sys
try:
    print(len(json.load(open(sys.argv[1])).get('Mounts', [])))
except Exception:
    print('unknown')
" "$_cfg" 2>/dev/null || echo unknown)

case "$_mounts" in
    0)  unchanged "machine has no host mounts (verified)" ;;
    unknown)
        die "could not read mounts from $_cfg -- refusing to proceed.
    Workspace isolation depends on this machine having no view of the host
    filesystem, and that cannot be confirmed." ;;
    *)  die "podman machine '$WK_MACHINE' has $_mounts host mount(s).
    Workspaces must not be able to reach the host filesystem.
    Recreate it with:  podman machine rm $WK_MACHINE && ./setup" ;;
esac

# --- resources ---------------------------------------------------------------
# Re-apply the envelope if the host changed (or the machine predates this
# policy). `podman machine set` requires the machine to be stopped.
_cur_cpus=$(podman machine inspect "$WK_MACHINE" --format '{{.Resources.CPUs}}' 2>/dev/null || echo "")
_cur_mem=$(podman machine inspect "$WK_MACHINE" --format '{{.Resources.Memory}}' 2>/dev/null || echo "")

if [ "$_cur_cpus" = "$_cores" ] && [ "$_cur_mem" = "$_mem" ]; then
    unchanged "machine resources (${_cores} cpus, ${_mem} MiB)"
else
    _was_running=""
    [ "$(podman machine inspect "$WK_MACHINE" --format '{{.State}}')" = running ] && _was_running=1
    [ -n "$_was_running" ] && podman machine stop "$WK_MACHINE" >/dev/null
    podman machine set "$WK_MACHINE" --cpus "$_cores" --memory "$_mem"
    changed "machine resources -> ${_cores} cpus, ${_mem} MiB (host keeps ${WK_RESERVE_CORES} cores / ${WK_RESERVE_MB} MiB)"
    [ -n "$_was_running" ] && podman machine start "$WK_MACHINE" >/dev/null
fi

unset _cores _mem _disk _mounts _cur_cpus _cur_mem _was_running
