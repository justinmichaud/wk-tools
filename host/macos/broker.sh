# The fleet-request broker, as a per-user LaunchAgent.
#
# The macOS counterpart of host/linux/broker.sh, and it has one more job than
# that file does, because on this platform the workspace and the workstation
# are not the same machine.
#
# Container workspaces live inside the podman machine, a Fedora CoreOS guest,
# and they bind-mount *its* runtime directory at /run/wk -- which is why the
# egress proxy is installed in there (host/macos/vmtools.sh) rather than out
# here. The broker cannot follow it in. Everything it does is on this Mac: the
# tailnet identity, the ssh config that reaches the rpi4 through its bridge,
# the image store, `wk boot`'s drivers. A copy in the guest would have none of
# it and no route to any of it.
#
# So the process runs here and the *socket* is published in there, by the
# broker itself, over ssh with a remote unix-socket forward (see
# publish_into_machine in container/broker/wk-broker.py for why that direction
# and not a TCP listener). This stage installs the agent; the agent starts the
# broker; the broker holds the door open and re-establishes it if the guest
# restarts.
#
# Survives a reboot the way the proxy does -- differently spelled, same
# property. The proxy is a systemd --user service with lingering inside a guest
# that podman brings back up; this is RunAtLoad plus KeepAlive in the user's
# own LaunchAgents directory, which is what launchd offers for the same thing.

. "$WK_ROOT/lib/store.sh"

_label=com.wk.broker
_agents="$HOME/Library/LaunchAgents"
_plist="$_agents/$_label.plist"
_log="$(wk_state_dir)/broker.log"
ensure_dir "$_agents"
ensure_dir "$(wk_state_dir)"

_plist_new=$(mktemp)
cat > "$_plist_new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$_label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$WK_ROOT/container/broker/wk-broker.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WK_ROOT</key><string>$WK_ROOT</string>
    <key>WK_STORE</key><string>$WK_STORE</string>
    <!-- Which podman machine to publish the socket into. Named rather than
         discovered, because a Mac may hold more than one and only the one wk
         uses has workspaces in it. -->
    <key>WK_BROKER_PUBLISH_MACHINE</key><string>${WK_MACHINE:-wk}</string>
    <!-- launchd hands an agent /usr/bin:/bin:/usr/sbin:/sbin and nothing
         else, and everything this runs is a command: podman for the forward
         into the machine, ssh and tailscale for the fleet. /opt/podman/bin is
         where the official macOS pkg puts podman -- which is the install
         README.md calls for -- and it is on no default PATH, so leaving it out
         cost the forward silently on the first run here. -->
    <key>PATH</key><string>/opt/podman/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$_log</string>
  <key>StandardErrorPath</key><string>$_log</string>
</dict>
</plist>
EOF

_reload=""
if cmp -s "$_plist_new" "$_plist"; then
    unchanged "$_label.plist"
    rm -f "$_plist_new"
else
    install -m 0644 "$_plist_new" "$_plist"
    rm -f "$_plist_new"
    _reload=1
    changed "installed $_label.plist"
fi

# Restarted when the plist changed or the vocabulary did, and otherwise left
# alone -- `./setup` must be runnable while a request is in flight, and a
# bootout takes the request with it.
_policy_stamp="$(wk_state_dir)/.broker-policy"
_policy_hash=$(cksum < "$WK_ROOT/container/broker/wk-broker.py" | awk '{print $1}')
[ "$(cat "$_policy_stamp" 2>/dev/null)" = "$_policy_hash" ] || _reload=1

_svc="gui/$(id -u)/$_label"
if [ -n "$_reload" ]; then
    # bootout then bootstrap, rather than kickstart -k: the plist may have
    # changed, and launchd only re-reads it on bootstrap. A bootout of a label
    # that is not loaded exits non-zero and that is not a failure.
    launchctl bootout "$_svc" >/dev/null 2>&1 || true
    if launchctl bootstrap "gui/$(id -u)" "$_plist" >/dev/null 2>&1; then
        printf '%s\n' "$_policy_hash" > "$_policy_stamp"
        changed "started the fleet-request broker ($_label)"
    else
        warn "could not start $_label -- workspaces will have no way to ask for a
    bench device. launchctl print $_svc says why; the log is $_log"
    fi
elif launchctl print "$_svc" >/dev/null 2>&1; then
    unchanged "wk-broker running"
else
    launchctl bootstrap "gui/$(id -u)" "$_plist" >/dev/null 2>&1 \
        || warn "could not start $_label (see $_log)"
    launchctl print "$_svc" >/dev/null 2>&1 && changed "started $_label"
fi

unset _label _agents _plist _plist_new _log _reload _policy_stamp _policy_hash _svc
