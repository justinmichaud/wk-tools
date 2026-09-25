#!/usr/bin/env bash
# cmd/bench's arm not yet ported -- --list -- as cmd/bench runs it (lib/wk/shell.py bench_arms).

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/bench.sh"

case "${1:-}" in
    --list)  bench_plan_list || exit 1; exit 0 ;;
    *) die "BUG: lib/bench-arms.sh has no arm '${1:-}' (cmd/bench's BASH_ARMS names them)" ;;
esac
