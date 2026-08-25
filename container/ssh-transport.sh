#!/usr/bin/env bash
#
# The ssh transport for a container workspace: the ssh protocol over
# `podman exec`, for use as a ProxyCommand.
#
#   ProxyCommand <this> <workspace>
#
# A container workspace has no network interface at all -- `--network none` is
# the sandbox boundary, not a setting -- so there is no address for ssh to
# connect to and there never will be. What there is, on both hosts, is
# `podman exec`, and sshd's inetd mode is exactly a protocol conversation on
# stdin/stdout. One exec is therefore a complete transport, with nothing
# listening anywhere and no hole in the boundary: it is the privilege `wk enter`
# already has, spelled as something ssh can speak through.
#
# This is a *transport*, so two rules bind it:
#
#   Nothing but the protocol on stdout. Every message here goes to stderr,
#   which ssh shows to the person; a stray line on stdout is a corrupt packet
#   and reads as "connection closed by remote host".
#
#   It must not start anything or wait for anything. `wk zed` is what installs
#   the sshd and authorises the key (t_ssh_prepare); by the time ssh runs this,
#   all of that is either true or the connection should fail saying so.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"

NAME="${1:-}"
[ -n "$NAME" ] || die "usage: ssh-transport.sh <workspace>   (as an ssh ProxyCommand)"
require_name "$NAME"

# The driver by name rather than by ws_target: this is only ever the container
# transport, and a workspace whose record has moved to another target should
# fail here rather than be quietly reached some other way.
load_target container

t_ssh_exec "$NAME"
