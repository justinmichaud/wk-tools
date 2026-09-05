#!/usr/bin/env bash
# `ProxyCommand <this> <ws>`: sshd inetd mode over `podman exec` (`--network none` leaves no address); stdout carries protocol only.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"

NAME="${1:-}"
[ -n "$NAME" ] || die "usage: ssh-transport.sh <workspace>   (as an ssh ProxyCommand)"
require_name "$NAME"

load_target container

t_ssh_exec "$NAME"
