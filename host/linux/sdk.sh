# The container SDK and the egress proxy.
#
# The Linux counterpart of host/macos/vmtools.sh, minus everything that existed
# only because of the VM. Two jobs:
#
#   1. Clone webkit-container-sdk and re-apply the patches that make a sandboxed
#      workspace possible. Reset first: the patcher is idempotent and would see
#      its own markers on a hand-edited checkout and leave the tampering in
#      place, so configuration is regenerated rather than accumulated.
#
#   2. Install the egress proxy as a systemd --user service. This is the
#      workspace network boundary in its entirety: workspaces run with
#      --network none and reach the outside only through this proxy's unix
#      socket. See container/proxy/wk-proxy.py for the policy itself.
#
# Neither job needs root, except once to create /opt/webkit-container-sdk.

. "$WK_ROOT/lib/store.sh"

# Not /opt: an unprivileged checkout is one the user can reset, inspect and
# re-clone without asking anyone. The path only has to agree with targets/
# container.sh, which reads WK_SDK the same way.
SDK="${WK_SDK:-${XDG_DATA_HOME:-$HOME/.local/share}/webkit-container-sdk}"

# --- the SDK checkout --------------------------------------------------------
if [ -d "$SDK/.git" ]; then
    unchanged "webkit-container-sdk present"
else
    info "cloning webkit-container-sdk into $SDK"
    ensure_dir "$(dirname "$SDK")"
    git clone -q https://github.com/Igalia/webkit-container-sdk.git "$SDK" \
        || die "could not clone the container SDK"
    changed "cloned webkit-container-sdk"
fi

# Hard reset before patching, for the reason in the header.
_before=$(cd "$SDK" && git rev-parse HEAD)$(cd "$SDK" && git status --porcelain | wc -l)
git -C "$SDK" reset --hard --quiet
git -C "$SDK" clean -qfd

if bash "$WK_ROOT/container/sdk-patches/apply.sh" "$SDK" >/dev/null 2>&1; then
    unchanged "SDK patched"
else
    bash "$WK_ROOT/container/sdk-patches/apply.sh" "$SDK" || die "SDK patching failed"
fi

# --- the egress proxy --------------------------------------------------------
# A systemd --user service, so it needs no privilege and cannot be modified by
# anything inside a workspace: the workspace sees one unix socket and nothing
# else. Lingering (enabled by the machine stage) keeps it alive for unattended
# runs with nobody logged in.
_unit_dir="$HOME/.config/systemd/user"
ensure_dir "$_unit_dir"

_unit_new=$(mktemp)
cat > "$_unit_new" <<EOF
[Unit]
Description=wk workspace egress proxy
Documentation=file://$WK_ROOT/container/proxy/wk-proxy.py

[Service]
Type=notify
NotifyAccess=all
ExecStart=/usr/bin/python3 $WK_ROOT/container/proxy/wk-proxy.py
Environment=WK_STORE=$WK_STORE
Restart=on-failure
RestartSec=2
# %t/wk holds the one socket a workspace can see. Preserved across restarts
# because containers bind-mount that directory: letting systemd delete it on
# stop would leave every running workspace holding a mount of a deleted
# directory, and no amount of restarting the proxy would fix it.
RuntimeDirectory=wk
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
# The proxy is the boundary; it must not be able to write anywhere interesting.
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=default.target
EOF

if cmp -s "$_unit_new" "$_unit_dir/wk-proxy.service"; then
    unchanged "wk-proxy.service"
    rm -f "$_unit_new"
else
    install -m 0644 "$_unit_new" "$_unit_dir/wk-proxy.service"
    rm -f "$_unit_new"
    systemctl --user daemon-reload
    changed "installed wk-proxy.service"
fi

# Restart only when the policy itself changed. Restarting unconditionally would
# match the "configuration is regenerated, never accumulated" rule -- but it also
# drops every workspace's egress for a moment, and `./setup` is meant to be
# runnable at any time, including while a build is fetching something. The
# allowlist is in the source file; the per-device part (pi-hosts) is re-read on
# every request and needs no restart at all.
_policy_stamp="$WK_STORE/.proxy-policy"
_policy_hash=$(cat "$WK_ROOT/container/proxy/wk-proxy.py" | cksum | awk '{print $1}')

if systemctl --user is-active --quiet wk-proxy.service; then
    if [ "$(cat "$_policy_stamp" 2>/dev/null)" = "$_policy_hash" ]; then
        unchanged "wk-proxy running"
    else
        systemctl --user restart wk-proxy.service
        printf '%s\n' "$_policy_hash" > "$_policy_stamp"
        changed "restarted wk-proxy (policy changed)"
    fi
else
    systemctl --user enable --now wk-proxy.service >/dev/null 2>&1 \
        || warn "could not start wk-proxy.service -- workspaces will have no egress"
    if systemctl --user is-active --quiet wk-proxy.service; then
        printf '%s\n' "$_policy_hash" > "$_policy_stamp"
        changed "started wk-proxy.service"
    fi
fi

# --- the deploy keys' ssh-agent ----------------------------------------------
# The one process on this machine that holds a private deploy key. Its socket
# is in %t/wk, the directory every container bind-mounts at /run/wk, so a
# workspace can *use* a key it can never read -- an agent has no protocol for
# handing one back. `wk push on|off` loads and empties it (push_agent_*,
# lib/store.sh); this only makes sure it is there to be filled. It starts empty
# and stays empty across a restart, which is the safe direction: the switch has
# to be thrown again and `wk push status` says so.
_unit_new=$(mktemp)
cat > "$_unit_new" <<EOF
[Unit]
Description=wk deploy-key ssh-agent (outside every workspace)
Documentation=file://$WK_ROOT/cmd/push

[Service]
Type=simple
ExecStart=/usr/bin/ssh-agent -D -a %t/wk/ssh-agent.sock
Restart=on-failure
RestartSec=2
# The same directory wk-proxy.service owns, preserved for the same reason:
# containers bind-mount it.
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
EOF

if cmp -s "$_unit_new" "$_unit_dir/wk-ssh-agent.service"; then
    unchanged "wk-ssh-agent.service"
    rm -f "$_unit_new"
else
    install -m 0644 "$_unit_new" "$_unit_dir/wk-ssh-agent.service"
    rm -f "$_unit_new"
    systemctl --user daemon-reload
    changed "installed wk-ssh-agent.service"
fi

if systemctl --user is-active --quiet wk-ssh-agent.service; then
    unchanged "wk-ssh-agent running"
else
    systemctl --user enable --now wk-ssh-agent.service >/dev/null 2>&1 \
        || warn "could not start wk-ssh-agent.service -- no workspace here can push"
    systemctl --user is-active --quiet wk-ssh-agent.service \
        && changed "started wk-ssh-agent.service"
fi

# --- the GitHub API credential injector --------------------------------------
# The other half of the same switch: it terminates TLS for api.github.com and
# replaces the Authorization header with the real token, so `git-webkit pr`
# works in a workspace that never holds it (container/proxy/github-inject.py).
# Its socket is under the store and *not* in %t/wk: a workspace must reach it
# through the egress policy, not around it.
_unit_new=$(mktemp)
cat > "$_unit_new" <<EOF
[Unit]
Description=wk GitHub API credential injector
Documentation=file://$WK_ROOT/container/proxy/github-inject.py

[Service]
Type=simple
ExecStart=/usr/bin/python3 $WK_ROOT/container/proxy/github-inject.py
Environment=WK_STORE=$WK_STORE
Restart=on-failure
RestartSec=2
# It publishes its CA certificate into %t/wk, which every container mounts.
RuntimeDirectory=wk
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$WK_STORE

[Install]
WantedBy=default.target
EOF

if cmp -s "$_unit_new" "$_unit_dir/wk-github-inject.service"; then
    unchanged "wk-github-inject.service"
    rm -f "$_unit_new"
else
    install -m 0644 "$_unit_new" "$_unit_dir/wk-github-inject.service"
    rm -f "$_unit_new"
    systemctl --user daemon-reload
    changed "installed wk-github-inject.service"
fi

# Restarted only when the program changed, for the reason the proxy's own stamp
# gives: it runs straight off this checkout.
_inject_stamp="$WK_STORE/.inject-policy"
_inject_hash=$(cksum < "$WK_ROOT/container/proxy/github-inject.py" | awk '{print $1}')
if systemctl --user is-active --quiet wk-github-inject.service; then
    if [ "$(cat "$_inject_stamp" 2>/dev/null)" = "$_inject_hash" ]; then
        unchanged "wk-github-inject running"
    else
        systemctl --user restart wk-github-inject.service
        printf '%s\n' "$_inject_hash" > "$_inject_stamp"
        changed "restarted wk-github-inject (program changed)"
    fi
else
    systemctl --user enable --now wk-github-inject.service >/dev/null 2>&1 \
        || warn "could not start wk-github-inject.service -- 'git-webkit pr' in a workspace will fail"
    if systemctl --user is-active --quiet wk-github-inject.service; then
        printf '%s\n' "$_inject_hash" > "$_inject_stamp"
        changed "started wk-github-inject.service"
    fi
fi

unset SDK _before _unit_dir _unit_new _policy_stamp _policy_hash _inject_stamp _inject_hash
