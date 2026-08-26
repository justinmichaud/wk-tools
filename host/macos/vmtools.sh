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

# _unpinned_host_key_opts (lib/reach.sh) is right for this machine too: a
# podman machine is recreated by ./setup, not upgraded in place (CLAUDE.md,
# "no in-place upgrades"), so its host key is exactly as disposable as a
# board's bench image. -p/-i/BatchMode/ConnectTimeout are what remain
# genuinely this machine's own -- a random port and a generated key,
# discovered fresh from `podman machine inspect` every run.
command -v _unpinned_host_key_opts >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"

_rsh() {
    # shellcheck disable=SC2046
    ssh -o BatchMode=yes -o ConnectTimeout="${WK_SSH_TIMEOUT:-10}" $(_unpinned_host_key_opts) \
        -p "$_ssh_port" -i "$_ssh_key" \
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

# WK_VMTOOLS_ONLY=tools: the push above and nothing else.
#
# `wk sync --target container` is the caller, and it wants exactly this one
# thing -- the copy of wk-tools every container bind-mounts read-only. Until it
# existed, that copy was refreshed only by `./setup --stage vmtools`, so a
# command added here was "unknown command" inside every container until somebody
# remembered to re-run setup. An environment variable rather than an argument
# because this file is *sourced* by ./setup, and $1 there is setup's own.

# The egress policy is *part of* the tooling that was just pushed, so it is
# applied here rather than only by a full `./setup`.
#
# Restarted only when the policy itself changed. An unconditional restart would
# fit the "regenerate, never accumulate" rule, but it also drops every
# workspace's egress for a moment, and this file is meant to be runnable while a
# build is fetching something. The per-device part (pi-hosts) is re-read per
# request and needs no restart at all.
_proxy_policy_hash() { cksum < "$WK_ROOT/container/proxy/wk-proxy.py" | awk '{print $1}'; }

# Only when it is already running: starting it is the full setup's job (the unit
# is installed further down), and a `wk sync --target container` on a machine
# that has never been set up should not report a failure to start something it
# was not asked to install. Returns non-zero when there was nothing running to
# reload, which is what the full path branches on.
_proxy_policy_reload() {
    local want; want=$(_proxy_policy_hash)
    _rsh 'systemctl --user is-active --quiet wk-proxy.service' || return 1
    if [ "$(_rsh 'cat /var/lib/wk/.proxy-policy 2>/dev/null' || true)" = "$want" ]; then
        unchanged "wk-proxy running"
        return 0
    fi
    _rsh "systemctl --user restart wk-proxy.service && echo $want > /var/lib/wk/.proxy-policy"
    changed "restarted wk-proxy (policy changed)"
}

if [ "${WK_VMTOOLS_ONLY:-}" = tools ]; then
    # A pushed policy that is not the running one is the same bug as a pushed
    # `wk` that no container can see: it is silent, and it shows up as a fetch
    # being refused for a name the allowlist plainly contains.
    _proxy_policy_reload || true
    return 0 2>/dev/null || exit 0
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
# egress proxy, the podman network, installed packages.
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

# --- the egress proxy --------------------------------------------------------
# The boundary, and the same one Linux uses. A systemd --user service owned by
# `core`, so nothing in the daily path needs a privilege and nothing inside a
# workspace can modify it: the workspace sees one unix socket and nothing else.
#
# This is the egress policy, in place of an nftables one. The short version:
# nftables requires
# rootful podman -- and under rootful podman a container escape is a root
# escape -- while the proxy needs no privilege at all and expresses the policy
# in hostnames rather than hand-refreshed CIDR lists.
#
# Lingering is already on for `core` in the podman machine, so the service
# survives with nobody logged in.
debug "installing the egress proxy in the machine"

_rsh 'mkdir -p ~/.config/systemd/user'

# %t expands to the user runtime directory, which is /run/user/501 here -- the
# machine's `core` is uid 501, not the 1000 the old comments assumed.
_unit=$(mktemp)
cat > "$_unit" <<'UNIT'
[Unit]
Description=wk workspace egress proxy
Documentation=file:///opt/wk-tools/container/proxy/wk-proxy.py

[Service]
Type=notify
NotifyAccess=all
ExecStart=/usr/bin/python3 /opt/wk-tools/container/proxy/wk-proxy.py
Environment=WK_STORE=/var/lib/wk
Restart=on-failure
RestartSec=2
# Containers bind-mount %t/wk, so systemd must not delete it on stop: every
# running workspace would be left holding a mount of a deleted directory, and
# restarting the proxy would not fix it.
RuntimeDirectory=wk
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=default.target
UNIT

scp -q -P "$_ssh_port" -i "$_ssh_key" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$_unit" "$_ssh_user@localhost:/home/core/.config/systemd/user/wk-proxy.service.new"
rm -f "$_unit"

if _rsh 'cmp -s ~/.config/systemd/user/wk-proxy.service.new ~/.config/systemd/user/wk-proxy.service'; then
    _rsh 'rm -f ~/.config/systemd/user/wk-proxy.service.new'
    unchanged "wk-proxy.service"
else
    _rsh 'mv ~/.config/systemd/user/wk-proxy.service.new ~/.config/systemd/user/wk-proxy.service &&
          systemctl --user daemon-reload'
    changed "installed wk-proxy.service in the machine"
fi

_proxy_policy_reload || true
if ! _rsh 'systemctl --user is-active --quiet wk-proxy.service'; then
    _rsh 'systemctl --user enable --now wk-proxy.service' >/dev/null 2>&1 \
        || warn "could not start wk-proxy.service -- workspaces will have no egress"
    if _rsh 'systemctl --user is-active --quiet wk-proxy.service'; then
        _rsh "echo $(_proxy_policy_hash) > /var/lib/wk/.proxy-policy"
        changed "started wk-proxy.service in the machine"
    fi
fi

# --- retire the nftables policy ----------------------------------------------
# Removed rather than left dormant. A workspace with an interface *and* a proxy
# socket has the union of two policies, and the failure is silent: the packet
# filter allows a connection the proxy would have refused, and nothing logs a
# decision that was never asked for.
if _rsh 'sudo nft list table inet wk_egress >/dev/null 2>&1'; then
    _rsh 'sudo nft delete table inet wk_egress 2>/dev/null || true
          sudo rm -f /etc/nftables/wk-egress.nft
          sudo sed -i "/wk-egress.nft/d" /etc/sysconfig/nftables.conf 2>/dev/null || true'
    changed "removed the old nftables egress policy (the proxy is the boundary now)"
else
    unchanged "no nftables egress policy (the proxy is the boundary)"
fi

unset _ssh_port _ssh_key _ssh_user _unit _policy_hash
