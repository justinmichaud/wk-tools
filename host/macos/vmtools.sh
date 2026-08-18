# Copy wk-tools into the VM and install what depends on it.
#
# The VM has no view of the host filesystem by design, so the tooling has to be
# pushed rather than mounted. rsync over `podman machine ssh` keeps that a
# single fast step and, crucially, works against the local working tree -- so
# changes are testable without pushing to GitHub first.
#
# Runs after the machine stage. Safe to re-run; rsync only sends differences.

WK_MACHINE="${WK_MACHINE:-wk}"

podman machine inspect "$WK_MACHINE" >/dev/null 2>&1 || {
    debug "no machine yet; skipping VM tooling"
    return 0 2>/dev/null || true
}

if [ "$(podman machine inspect "$WK_MACHINE" --format '{{.State}}')" != running ]; then
    info "starting machine '$WK_MACHINE'"
    podman machine start "$WK_MACHINE" >/dev/null
fi

_ssh_port=$(podman machine inspect "$WK_MACHINE" --format '{{.SSHConfig.Port}}')
_ssh_key=$(podman machine inspect "$WK_MACHINE" --format '{{.SSHConfig.IdentityPath}}')
_ssh_user=$(podman machine inspect "$WK_MACHINE" --format '{{.SSHConfig.RemoteUsername}}')

_rsh() {
    ssh -q -p "$_ssh_port" -i "$_ssh_key" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$_ssh_user@localhost" "$@"
}

# --delete so a file removed here is removed there: a stale command left behind
# in the VM would shadow the current one and be very confusing to debug.
#
# --itemize-changes so this reports honestly: an unconditional "synced" message
# would make every setup run look like it changed something.
_synced=$(rsync -az --delete --itemize-changes \
    -e "ssh -q -p $_ssh_port -i $_ssh_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
    --exclude '.git/' \
    "$WK_ROOT/" "$_ssh_user@localhost:/opt/wk-tools/" | grep -c . || true)

if [ "${_synced:-0}" -gt 0 ]; then
    changed "synced $_synced file(s) to /opt/wk-tools"
else
    unchanged "wk-tools in sync"
fi

# --- shared mutable skills ---------------------------------------------------
# Seeded from the repo once, then left alone. Workspaces share this directory
# read-write and are expected to edit it, so re-syncing on every setup run would
# silently destroy their work. `wk skills pull` is how edits come back.
if _rsh 'test -d /var/lib/wk/skills && test -n "$(ls -A /var/lib/wk/skills 2>/dev/null)"'; then
    unchanged "shared skills present (not overwritten)"
    _rsh 'diff -rq /opt/wk-tools/claude/skills /var/lib/wk/skills >/dev/null 2>&1' \
        || log "note: shared skills differ from the repo -- 'wk skills status' inside the VM"
else
    info "seeding the shared skills directory"
    _rsh 'mkdir -p /var/lib/wk/skills && cp -a /opt/wk-tools/claude/skills/. /var/lib/wk/skills/'
    changed "seeded /var/lib/wk/skills"
fi

# --- build key ---------------------------------------------------------------
# One shared deploy key, used only for pushing to the fork. Generated here so a
# fresh machine is ready to go; it still has to be registered on GitHub once.
_rsh 'WK_IN_VM=1 /opt/wk-tools/cmd/key ensure' 2>&1 | sed 's/^/  /' || true
if _rsh 'test -f /var/lib/wk/secrets/build_key.pub'; then
    unchanged "build key present"
else
    warn "no build key; workspaces will not be able to push"
fi

# --- machine configuration is regenerated, never accumulated -----------------
# Everything below is derived wholly from this repo and reapplied on every run,
# so a change made by hand inside the VM does not survive `./setup`. That is the
# point: the VM is meant to be reproducible from the repo, and configuration
# drift there is invisible and very hard to debug.
#
# What is regenerated: /opt/wk-tools, the SDK checkout and its patches, the
# nftables policy, the podman network, installed packages.
#
# What is NOT touched, because it is data rather than configuration:
#   /var/lib/wk/git      the mirror        /var/lib/wk/ws       workspaces
#   /var/lib/wk/base     snapshots         /var/lib/wk/cache    ccache et al
#   /var/lib/wk/skills   mutable skills    /var/lib/wk/secrets  the build key

# Re-applied every run so nothing done by hand in the VM survives, but reported
# from ansible's own changed-count -- regenerating to an identical result is not
# a change, and saying it is destroys the signal value of "no changes".
debug "re-applying machine provisioning"
scp -q -P "$_ssh_port" -i "$_ssh_key" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$WK_ROOT/host/macos/playbook.yaml" "$_ssh_user@localhost:/home/core/playbook.yaml"
_pb=$(_rsh 'ansible-playbook /home/core/playbook.yaml 2>&1 | grep -oE "changed=[0-9]+" | head -1' || echo "changed=?")
case "$_pb" in
    changed=0) unchanged "machine provisioning" ;;
    changed=\?) warn "provisioning playbook reported errors; re-run with WK_DEBUG=1" ;;
    *)         changed "machine provisioning ($_pb)" ;;
esac

# --- the SDK -----------------------------------------------------------------
# Cloned inside the VM, then patched. The patches are what make a sandboxed
# workspace possible at all: without --additional-flags there is no way to
# attach the overlay mount, and without a selectable --network the container
# shares the host namespace and cannot be firewalled.
if _rsh 'test -d /opt/webkit-container-sdk/.git'; then
    unchanged "webkit-container-sdk present"
else
    info "cloning webkit-container-sdk into the machine"
    _rsh 'sudo mkdir -p /opt/webkit-container-sdk && sudo chown core:core /opt/webkit-container-sdk &&
          git clone -q https://github.com/Igalia/webkit-container-sdk.git /opt/webkit-container-sdk'
    changed "cloned webkit-container-sdk"
fi

# Hard reset before patching. Without this an edit made inside the VM would
# survive forever: the patcher is idempotent and would see its own markers
# already present, so it would leave the tampered file exactly as it found it.
#
# Because of that reset the patcher always has work to do and always says so.
# What matters is whether the *result* differs, so hash the patched files either
# side and report on that; the patcher's own chatter is debug-level.
debug "resetting and re-patching the SDK"
_sdk_hash() {
    _rsh 'cat /opt/webkit-container-sdk/scripts/host-only/wkdev-create \
              /opt/webkit-container-sdk/scripts/host-only/wkdev-enter \
              /opt/webkit-container-sdk/scripts/container-only/.wkdev-init \
              /opt/webkit-container-sdk/scripts/container-only/.wkdev-sync-runtime-state \
          2>/dev/null | sha256sum | cut -d" " -f1'
}
_sdk_before=$(_sdk_hash)
_rsh 'cd /opt/webkit-container-sdk && git reset --hard --quiet && git clean -qfd'
if [ -n "${WK_DEBUG:-}" ]; then
    _rsh 'bash /opt/wk-tools/container/sdk-patches/apply.sh /opt/webkit-container-sdk'
else
    _rsh 'bash /opt/wk-tools/container/sdk-patches/apply.sh /opt/webkit-container-sdk' >/dev/null 2>&1 \
        || die "SDK patching failed; re-run with WK_DEBUG=1"
fi
_sdk_after=$(_sdk_hash)
if [ "$_sdk_before" = "$_sdk_after" ]; then
    unchanged "SDK patches"
else
    changed "SDK patches re-applied (result differs from before)"
fi

# --- firewall ----------------------------------------------------------------
# The boundary itself, rebuilt from the repo each run so no rule can be added,
# removed or reordered out from under it.
#
# The Pi addresses are the one piece of runtime state in the policy, so they
# are kept outside it in /var/lib/wk/pi-hosts and replayed after the reload --
# otherwise regenerating the firewall would silently cut off the test devices.
# Same reasoning: the table is torn down and rebuilt every run so no rule can
# be added or reordered out from under it, but an identical result is not news.
debug "regenerating the workspace egress policy"
_nft_before=$(_rsh 'sudo nft list table inet wk_egress 2>/dev/null | sha256sum | cut -d" " -f1' || echo none)
_rsh 'sudo install -D -m 0644 /opt/wk-tools/container/nftables/wk-egress.nft /etc/nftables/wk-egress.nft'
_rsh 'sudo grep -qxF '"'"'include "/etc/nftables/wk-egress.nft"'"'"' /etc/sysconfig/nftables.conf 2>/dev/null ||
      echo '"'"'include "/etc/nftables/wk-egress.nft"'"'"' | sudo tee -a /etc/sysconfig/nftables.conf >/dev/null'

_rsh 'sudo nft delete table inet wk_egress 2>/dev/null || true
      sudo nft -f /etc/nftables/wk-egress.nft
      sudo systemctl enable --now nftables >/dev/null 2>&1 || true
      if [ -s /var/lib/wk/pi-hosts ]; then
          while read -r ip; do
              [ -n "$ip" ] && sudo nft add element inet wk_egress pi_hosts "{ $ip }" 2>/dev/null || true
          done < /var/lib/wk/pi-hosts
      fi' || warn "could not load the egress policy; 'wk claude' will refuse to run until it is"

# Seed the hostnames that have no published range, so a fresh machine can
# install the Claude CLI without a manual gc run.
_rsh 'for h in downloads.claude.ai; do
        for ip in $(getent ahostsv4 "$h" 2>/dev/null | awk "{print \$1}" | sort -u); do
          sudo nft add element inet wk_egress resolved_hosts "{ $ip }" 2>/dev/null || true
        done
      done' || true

if _rsh 'sudo nft list table inet wk_egress >/dev/null 2>&1'; then
    _pis=$(_rsh 'cat /var/lib/wk/pi-hosts 2>/dev/null | wc -l' | tr -d ' ')
    _nft_after=$(_rsh 'sudo nft list table inet wk_egress 2>/dev/null | sha256sum | cut -d" " -f1')
    if [ "$_nft_before" = "$_nft_after" ]; then
        unchanged "egress policy (${_pis} Pi address(es) allowed)"
    else
        changed "egress policy rebuilt (${_pis} Pi address(es) allowed)"
    fi
else
    warn "egress policy is NOT active"
fi

unset _ssh_port _ssh_key _ssh_user
