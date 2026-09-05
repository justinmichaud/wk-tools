#!/usr/bin/env bash
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

# Not exec'd: the EXIT trap that drops the lock has to outlive the command.
"$@"
