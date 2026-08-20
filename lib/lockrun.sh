#!/usr/bin/env bash
#
# lockrun.sh <resource> [-w seconds] -- cmd...
#
# Run a command while holding one of this machine's wk locks, and exit with the
# command's own status. `with_lock` for a caller that is not a shell function
# in this process -- which today means one caller: a build on a shared machine,
# started over ssh by a workstation that cannot hold a lock on the far end
# because the lock has to die with the *build*, not with the connection.
#
# It replaced a `flock -w 3600 <file> <cmd>` in targets/remote.sh, for the
# reason every other flock in this tree was replaced (lib/common.sh, "locks"):
# a flock is held by the open file descriptor, so every process the command
# leaves behind holds the machine's build lock for as long as it lives. The
# same file also explains why this cannot be that command's own `hold_lock`:
# nothing over here is a process over there.
set -euo pipefail

WK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"

[ $# -ge 1 ] || die "usage: lockrun.sh <resource> [-w seconds] -- cmd..."
res="$1"; shift
wait_for=600
[ "${1:-}" = -w ] && { wait_for="${2:-600}"; shift 2; }
[ "${1:-}" = -- ] && shift
[ $# -ge 1 ] || die "lockrun.sh: nothing to run"

hold_lock "$res" -w "$wait_for"

# Not exec'd: the EXIT handler that drops the lock has to still be here when
# the command ends. The lock would be broken by the next taker either way --
# it dies with its holder -- but "either way" is a repair, and this is the
# ordinary path.
"$@"
