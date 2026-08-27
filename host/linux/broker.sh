# The fleet-request broker, as a systemd --user service.
#
# The Linux counterpart of host/macos/broker.sh, much the smaller of the two:
# here the containers and the broker share a machine, so the socket it binds
# in %t/wk *is* the directory every workspace bind-mounts at /run/wk
# (targets/container.sh). There is nothing to publish anywhere.
#
# Installed the same way as the egress proxy (host/linux/sdk.sh): a policy
# engine outside the sandbox, owning the only socket the sandbox can see,
# needing no privilege. Lingering, enabled by the machine stage, keeps both
# alive for unattended runs with nobody logged in.
#
# A separate stage, not a second half of `sdk`, so a machine with no
# container SDK can still hold the door open: driving bench devices for
# someone else's workspaces is a real configuration.

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
# %t/wk holds the sockets a workspace can see, and containers bind-mount
# that directory: preserved across restarts, or systemd deleting it on stop
# leaves every running workspace holding a mount of a deleted directory.
RuntimeDirectory=wk
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes

# Not sandboxed the way wk-proxy is: the proxy only opens sockets, but this
# one runs \`wk boot\`/\`wk pi\`, reading what system a device's own boot
# partition holds and ssh-ing to a board -- it is the thing with the
# privilege. What bounds it instead is the vocabulary: seven commands against
# machines the fleet declares as bench devices.
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
