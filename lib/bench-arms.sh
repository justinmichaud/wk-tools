#!/usr/bin/env bash
# cmd/bench's arms not yet ported -- mac, mac-volume, mac-ab, ab-summary, --list -- as cmd/bench runs them (lib/wk/shell.py bench_arms).

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/bench.sh"

case "${1:-}" in
    --list)  bench_plan_list || exit 1; exit 0 ;;
    mac)     shift; exec "$WK_ROOT/bench/mac-lane.sh" ${WK_DRY_RUN:+--dry-run} "$@" ;;
    mac-volume) shift; exec "$WK_ROOT/bench/mac-bench-volume.sh" ${WK_DRY_RUN:+--dry-run} "$@" ;;
    mac-ab)  shift; exec "$WK_ROOT/bench/mac-ab.sh" "$@" ;;
    ab-summary) shift; exec "$WK_ROOT/bench/mac-ab-summary.sh" "$@" ;;
    *) die "BUG: lib/bench-arms.sh has no arm '${1:-}' (cmd/bench's BASH_ARMS names them)" ;;
esac
