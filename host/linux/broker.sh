# The fleet-request broker, as a systemd --user service.
#
# The Linux counterpart of host/macos/broker.sh, and much the smaller of the
# two for one reason: here the containers and the broker share a machine, so
# the socket it binds in %t/wk *is* the directory every workspace bind-mounts
# at /run/wk (targets/container.sh). There is nothing to publish anywhere.
#
# Installed the same way and in the same place as the egress proxy
# (host/linux/sdk.sh), because it is the same kind of thing: a policy engine
# outside the sandbox, owning the only socket the sandbox can see, needing no
# privilege at all. Lingering -- enabled by the machine stage -- is what keeps
# both alive for unattended runs with nobody logged in.
#
# It is a separate stage rather than a second half of `sdk` so that a machine
# with no container SDK can still hold the door open: a Linux workstation that
# only *drives* bench devices for someone else's workspaces is a real
# configuration, and re-running one stage should not re-clone an SDK.

. "$WK_ROOT/lib/store.sh"

_unit_dir="$HOME/.config/systemd/user"
ensure_dir "$_unit_dir"

_unit_new=$(mktemp)
cat > "$_unit_new" <<EOF
[Unit]
Description=wk workspace fleet-request broker
Documentation=file://$WK_ROOT/container/broker/wk-broker.py

[Service]
Type=notify
NotifyAccess=all
ExecStart=/usr/bin/python3 $WK_ROOT/container/broker/wk-broker.py
Environment=WK_ROOT=$WK_ROOT
Environment=WK_STORE=$WK_STORE
Restart=on-failure
RestartSec=2
# %t/wk holds the sockets a workspace can see -- the proxy's and this one --
# and containers bind-mount that directory. Preserved across restarts for the
# same reason the proxy preserves it: letting systemd delete it on stop leaves
# every running workspace holding a mount of a deleted directory, which no
# amount of restarting fixes.
RuntimeDirectory=wk
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes

# Deliberately NOT sandboxed the way wk-proxy is. The proxy only ever opens
# sockets, so ProtectSystem=strict costs it nothing; this one runs \`wk boot\`
# and \`wk pi\`, which read the image store, write request records, and ssh to
# a board -- it is the thing with the privilege, and pretending otherwise by
# copying the proxy's hardening would only produce a service that fails in
# ways that have nothing to do with what it was asked for.
#
# What bounds it instead is the vocabulary: it can run seven commands against
# machines the fleet declares as bench devices, and nothing else.
PrivateTmp=yes

[Install]
WantedBy=default.target
EOF

if cmp -s "$_unit_new" "$_unit_dir/wk-broker.service"; then
    unchanged "wk-broker.service"
    rm -f "$_unit_new"
else
    install -m 0644 "$_unit_new" "$_unit_dir/wk-broker.service"
    rm -f "$_unit_new"
    systemctl --user daemon-reload
    changed "installed wk-broker.service"
fi

# Restarted only when the policy itself changed -- the same rule and the same
# reason as the proxy's: `./setup` is meant to be runnable at any time, and a
# restart drops whatever request is in flight. The fleet's declarations
# (boot/machines/*.conf) are re-read per request and need no restart at all.
_policy_stamp="$WK_STORE/.broker-policy"
_policy_hash=$(cksum < "$WK_ROOT/container/broker/wk-broker.py" | awk '{print $1}')

if systemctl --user is-active --quiet wk-broker.service; then
    if [ "$(cat "$_policy_stamp" 2>/dev/null)" = "$_policy_hash" ]; then
        unchanged "wk-broker running"
    else
        systemctl --user restart wk-broker.service
        printf '%s\n' "$_policy_hash" > "$_policy_stamp"
        changed "restarted wk-broker (vocabulary changed)"
    fi
else
    systemctl --user enable --now wk-broker.service >/dev/null 2>&1 \
        || warn "could not start wk-broker.service -- workspaces will have no way to
    ask for a bench device (they will say so, naming this stage)"
    if systemctl --user is-active --quiet wk-broker.service; then
        printf '%s\n' "$_policy_hash" > "$_policy_stamp"
        changed "started wk-broker.service"
    fi
fi

unset _unit_dir _unit_new _policy_stamp _policy_hash
