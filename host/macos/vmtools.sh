# Install what depends on wk-tools inside the VM.
#
# The tooling itself is not copied: the machine mounts this checkout read-only
# at /opt/wk-tools (host/macos/machine.sh), so the VM runs the working tree and
# an edit here is testable in a workspace with no step in between. What is left
# is everything that has to be *installed* around it -- the egress proxy, the
# SDK, the machine's own packages -- and the three mounts are verified first,
# since every one of those steps names a path in one of them.
#
# Runs after the machine stage. Safe to re-run.

# For $WK_STORE and wk_secrets_dir: the two ends of the secrets mount this
# verifies below, from the one file that answers where each of them is.
. "$WK_ROOT/lib/store.sh"

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

# _unpinned_host_key_opts (lib/reach.sh) fits here too: a podman machine is
# recreated by ./setup, not upgraded in place, so its host key is as
# disposable as a board's bench image. -p/-i/BatchMode/ConnectTimeout are
# discovered fresh from `podman machine inspect` every run.
command -v _unpinned_host_key_opts >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"

_rsh() {
    # shellcheck disable=SC2046
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" $(_unpinned_host_key_opts) \
        -p "$_ssh_port" -i "$_ssh_key" \
        "$_ssh_user@localhost" "$@"
}

# --- the three mounts, from inside -------------------------------------------
# Every step below names a path in one of them, and so does every container
# this machine will ever create. A machine missing one fails one step at a
# time, each with a different message; this fails once and names the remedy.
if _rsh 'test -x /opt/wk-tools/wk'; then
    unchanged "this checkout is mounted at /opt/wk-tools"
else
    die "/opt/wk-tools/wk is not executable inside '$WK_MACHINE', so nothing in
    there can run this tooling. The machine mounts this checkout there when it
    is created:  ./setup --stage machine"
fi

# The mount, not the directory: an empty directory of the VM's own at the same
# path would leave `wk key set` writing on this host and every container
# reading nothing.
if _rsh "findmnt -no TARGET $(sh_quote "$WK_STORE/secrets")" >/dev/null 2>&1; then
    unchanged "the secrets directory is mounted at $WK_STORE/secrets"
else
    die "$WK_STORE/secrets is not a mount inside '$WK_MACHINE', so the keys this
    host holds ($(wk_secrets_dir)) reach no workspace. The machine mounts them
    there when it is created:  ./setup --stage machine"
fi

# The one writable mount: the Claude CLI rewrites the login credential in place
# when it spends the refresh token, so a read-only mount here logs every
# workspace out the first time one refreshes. Writability is checked from
# inside, where the mode actually applies.
if ! _rsh "findmnt -no TARGET $(sh_quote "$WK_STORE/agent-rw")" >/dev/null 2>&1; then
    die "$WK_STORE/agent-rw is not a mount inside '$WK_MACHINE', so the claude.ai
    login this host holds ($(wk_agent_rw_dir)) reaches no workspace. The machine
    mounts it there when it is created:  ./setup --stage machine"
elif _rsh "test -w $(sh_quote "$WK_STORE/agent-rw")"; then
    unchanged "the agent-writable directory is mounted read-write at $WK_STORE/agent-rw"
else
    die "$WK_STORE/agent-rw is mounted read-only inside '$WK_MACHINE'. The Claude
    CLI rewrites the login credential in place, so a workspace would be logged
    out the first time it refreshes. The machine mounts it read-write when it
    is created:  ./setup --stage machine"
fi

# The proxy runs the policy file straight off the mount, so an edit here is
# live in the VM the moment it is saved -- but only for a proxy that restarts
# to read it. Restarted only when the policy changed: an unconditional restart
# drops every workspace's egress for a moment, and this file is meant to be
# runnable while a build is fetching something.
_proxy_policy_hash() { cksum < "$WK_ROOT/container/proxy/wk-proxy.py" | awk '{print $1}'; }

# Only when it is already running: starting it is the install step further
# down, and a never-set-up machine should not report a failure to start
# something nothing has installed yet.
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

# --- shared mutable skills ---------------------------------------------------
# Seeded from the repo once, then left alone: workspaces share this
# directory read-write, so re-syncing on every run would destroy their
# edits. `wk skills pull` is how edits come back.
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
# One deploy key per fork, generated here so a fresh machine is ready to go; it
# still has to be registered on GitHub once (`wk key register`). Run on this
# host, where the keys live: the VM only reads them.
"$WK_ROOT/cmd/key" ensure 2>&1 | sed 's/^/  /' || true
if [ -f "$(wk_secrets_dir)/build_key_fork.pub" ]; then
    unchanged "build key present"
else
    warn "no build key; workspaces will not be able to push"
fi

# --- machine configuration is regenerated, never accumulated -----------------
# Everything below is derived wholly from this repo and reapplied on every
# run, so a change made by hand inside the VM does not survive `./setup`:
# the VM is reproducible from the repo, and drift there is invisible and
# hard to debug.
#
# Regenerated: the SDK checkout and its patches, the egress proxy, the
# machine's own layered packages. NOT touched, since it is data rather than
# configuration:
#   /var/lib/wk/git      the mirror        /var/lib/wk/ws       workspaces
#   /var/lib/wk/base     snapshots         /var/lib/wk/cache    ccache et al
#   /var/lib/wk/skills   mutable skills
# /opt/wk-tools and /var/lib/wk/secrets are neither: they are this host's own
# directories, mounted read-only.

# Reported from ansible's own changed-count: regenerating to an identical
# result is not a change, and saying it is destroys the signal of "no changes".
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
# Cloned inside the VM, then patched. The patches make a sandboxed workspace
# possible at all: without --additional-flags there is no way to attach the
# overlay mount, and without a selectable --network the container shares
# the host namespace and cannot be firewalled.
if _rsh 'test -d /opt/webkit-container-sdk/.git'; then
    unchanged "webkit-container-sdk present"
else
    info "cloning webkit-container-sdk into the machine"
    _rsh 'sudo mkdir -p /opt/webkit-container-sdk && sudo chown core:core /opt/webkit-container-sdk &&
          git clone -q https://github.com/Igalia/webkit-container-sdk.git /opt/webkit-container-sdk'
    changed "cloned webkit-container-sdk"
fi

# Hard reset before patching: without it an edit made inside the VM would
# survive forever, since the idempotent patcher sees its own markers already
# present and leaves a tampered file as found. The reset means the patcher
# always has work to do, so what's reported is whether the *result* differs,
# hashed either side; the patcher's own chatter is debug-level.
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
# The boundary, the same one Linux uses: a systemd --user service owned by
# `core`, so nothing in the daily path needs a privilege and nothing inside
# a workspace can modify it. In place of nftables, which requires rootful
# podman -- and under rootful podman a container escape is a root escape --
# the proxy needs no privilege and expresses policy in hostnames rather
# than hand-refreshed CIDR lists. Lingering is already on for `core`, so
# the service survives with nobody logged in.
debug "installing the egress proxy in the machine"

_rsh 'mkdir -p ~/.config/systemd/user'

# %t expands to the user runtime directory: /run/user/501, since the
# machine's `core` is uid 501.
_unit=$(mktemp)
cat > "$_unit" <<'UNIT'
[Unit]
Description=wk workspace egress proxy
Documentation=file:///opt/wk-tools/container/proxy/wk-proxy.py

[Unit]
# The executable is on a mount, so systemd must wait for it: without this the
# service starts before virtiofs is up and fails as "no such file", which reads
# as a broken proxy rather than a race.
RequiresMountsFor=/opt/wk-tools

[Service]
Type=notify
NotifyAccess=all
ExecStart=/usr/bin/python3 /opt/wk-tools/container/proxy/wk-proxy.py
Environment=WK_STORE=/var/lib/wk
Restart=on-failure
RestartSec=2
# Containers bind-mount %t/wk, so systemd must not delete it on stop, or
# every running workspace is left holding a mount of a deleted directory.
RuntimeDirectory=wk
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=default.target
UNIT

# One installer for every unit this machine gets: copy beside the live one,
# compare, and reload only when the result differs -- so a re-run of ./setup
# reports no change and does not restart a service a build is depending on.
_install_unit() { # <unit name> <local file>
    local name="$1" file="$2"
    scp -q -P "$_ssh_port" -i "$_ssh_key" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$file" "$_ssh_user@localhost:/home/core/.config/systemd/user/$name.new"
    rm -f "$file"
    if _rsh "cmp -s ~/.config/systemd/user/$name.new ~/.config/systemd/user/$name"; then
        _rsh "rm -f ~/.config/systemd/user/$name.new"
        unchanged "$name"
        return 0
    fi
    _rsh "mv ~/.config/systemd/user/$name.new ~/.config/systemd/user/$name &&
          systemctl --user daemon-reload"
    changed "installed $name in the machine"
}

_install_unit wk-proxy.service "$_unit"

_proxy_policy_reload || true
if ! _rsh 'systemctl --user is-active --quiet wk-proxy.service'; then
    _rsh 'systemctl --user enable --now wk-proxy.service' >/dev/null 2>&1 \
        || warn "could not start wk-proxy.service -- workspaces will have no egress"
    if _rsh 'systemctl --user is-active --quiet wk-proxy.service'; then
        _rsh "echo $(_proxy_policy_hash) > /var/lib/wk/.proxy-policy"
        changed "started wk-proxy.service in the machine"
    fi
fi

# --- the deploy keys' ssh-agent ----------------------------------------------
# The one thing on this machine that holds a private deploy key. Its socket is
# in %t/wk, the directory every container bind-mounts at /run/wk, so a
# workspace can *use* a key it can never read -- an agent has no protocol for
# handing one back. `wk push on|off` is what loads and empties it
# (push_agent_*, lib/store.sh); this only makes sure it is there to be filled.
#
# -D keeps it in the foreground so systemd's pid is the agent's. It starts
# empty and stays empty across a restart, which is the safe direction: the
# switch has to be thrown again, and `wk push status` says so rather than
# claiming a key that is gone.
_unit=$(mktemp)
cat > "$_unit" <<'UNIT'
[Unit]
Description=wk deploy-key ssh-agent (outside every workspace)
Documentation=file:///opt/wk-tools/cmd/push

[Service]
Type=simple
ExecStart=/usr/bin/ssh-agent -D -a %t/wk/ssh-agent.sock
Restart=on-failure
RestartSec=2
# The same directory wk-proxy.service owns, and containers bind-mount it, so
# systemd must not delete it on stop.
RuntimeDirectory=wk
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
# ssh-agent refuses to bind a path that already exists, and one left by a
# previous run has nothing behind it.
ExecStartPre=/usr/bin/rm -f %t/wk/ssh-agent.sock
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=default.target
UNIT
_install_unit wk-ssh-agent.service "$_unit"

if ! _rsh 'systemctl --user is-active --quiet wk-ssh-agent.service'; then
    _rsh 'systemctl --user enable --now wk-ssh-agent.service' >/dev/null 2>&1 \
        || warn "could not start wk-ssh-agent.service -- no workspace here can push"
    _rsh 'systemctl --user is-active --quiet wk-ssh-agent.service' \
        && changed "started wk-ssh-agent.service in the machine"
fi

# --- the GitHub API credential injector --------------------------------------
# The other half of the same switch: it terminates TLS for api.github.com and
# puts the real token in the Authorization header, so `git-webkit pr` works in
# a workspace that never holds the token (container/proxy/github-inject.py).
# Its socket is under the store and *not* in %t/wk: a workspace must reach it
# through the egress policy, not around it.
_unit=$(mktemp)
cat > "$_unit" <<'UNIT'
[Unit]
Description=wk GitHub API credential injector
Documentation=file:///opt/wk-tools/container/proxy/github-inject.py

[Unit]
# The executable is on a mount, so systemd must wait for it, exactly as
# wk-proxy.service does.
RequiresMountsFor=/opt/wk-tools

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/wk-tools/container/proxy/github-inject.py
Environment=WK_STORE=/var/lib/wk
Restart=on-failure
RestartSec=2
# It publishes its CA certificate into %t/wk, which every container mounts.
RuntimeDirectory=wk
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/wk

[Install]
WantedBy=default.target
UNIT
_install_unit wk-github-inject.service "$_unit"

# Restarted on a policy change for the same reason wk-proxy is: the program
# runs straight off the mount, so an edit here is only live for a service that
# re-execs to read it.
_inject_hash() { cksum < "$WK_ROOT/container/proxy/github-inject.py" | awk '{print $1}'; }
if _rsh 'systemctl --user is-active --quiet wk-github-inject.service'; then
    if [ "$(_rsh 'cat /var/lib/wk/.inject-policy 2>/dev/null' || true)" = "$(_inject_hash)" ]; then
        unchanged "wk-github-inject running"
    else
        _rsh "systemctl --user restart wk-github-inject.service && echo $(_inject_hash) > /var/lib/wk/.inject-policy"
        changed "restarted wk-github-inject (program changed)"
    fi
else
    _rsh 'systemctl --user enable --now wk-github-inject.service' >/dev/null 2>&1 \
        || warn "could not start wk-github-inject.service -- 'git-webkit pr' in a workspace will fail"
    if _rsh 'systemctl --user is-active --quiet wk-github-inject.service'; then
        _rsh "echo $(_inject_hash) > /var/lib/wk/.inject-policy"
        changed "started wk-github-inject.service in the machine"
    fi
fi

unset _ssh_port _ssh_key _ssh_user _unit
