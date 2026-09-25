_WK_TS_PEERS=""
_WK_TS_READ=""
_reach_py() { PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" python3 -m wk.reach "$@"; }
wk_tailscale_peers() { [ -n "$_WK_TS_READ" ] || { _WK_TS_PEERS=$(_reach_py peers) || _WK_TS_PEERS=""; _WK_TS_READ=1; }; printf '%s' "$_WK_TS_PEERS"; }   # name<TAB>ip<TAB>up|down
reach_tailnet() { wk_tailscale_peers | _reach_py tailnet "$1"; }
REACH_WHY=""
reach_offline() { wk_tailscale_peers >/dev/null; REACH_WHY=$(wk_tailscale_peers | _reach_py offline "$1"); [ -n "$REACH_WHY" ]; }
reach_without_tailnet() { wk_tailscale_peers | _reach_py without-tailnet "$1"; }
# A fresh host key is generated on every image write, so pinning warns of a man-in-the-middle; with known-hosts at /dev/null ssh announces a new key every connection, hence LogLevel=ERROR.
_unpinned_host_key_opts() { printf '%s' "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"; }
