wkdata() { python3 "$WK_ROOT/lib/wkdata.py" "$@"; }
wkslot() { python3 "$WK_ROOT/lib/wkslot.py" "$@"; }

BENCH_DIR="$(wk_bench_dir)"

# A task is one benchmarking command's output: $BENCH_DIR/<task>/ holds task.json, runs/<run>/, logs and reports. A live lock bench-task-<name> is what "running" means; no progress is stored.
bench_task_dir() { printf '%s/%s' "$BENCH_DIR" "$1"; }

bench_task_new() {
    local name="$1"; shift
    valid_name "$name" || die "'$name' is not a task name (letters, digits, '.', '_' and '-')"
    local dir; dir=$(bench_task_dir "$name")
    [ ! -e "$dir" ] || die "task $name already exists ($dir); a task is one request, made once"
    hold_lock "bench-task-$name" -w 5 || die "task $name is being created by another command"
    ensure_dir "$dir/runs" >/dev/null
    wkdata task-write "$dir" task="$name" requested="$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$@" \
        || die "could not write $dir/task.json"
}


bench_plan_list() {  # `wk bench --list` runs before any workspace exists to read a plan from, so it asks the mirror -- one git ls-tree, no export -- and returns 1 when there is no mirror
    local mirror ref
    mirror=$(wk_mirror)
    ref="${WK_BENCH_RUNNER_REF:-refs/heads/main}"  # another ref, for a runner an older lane needs
    if [ ! -d "$mirror" ] || ! git -C "$mirror" rev-parse --verify --quiet "$ref^{commit}" >/dev/null; then
        printf "no mirror at %s to read plans from; 'wk sync' fetches one, or read\n" "$mirror"
        printf "them from a workspace's own checkout: Tools/Scripts/run-benchmark --list-plans\n"
        return 1
    fi
    git -C "$mirror" ls-tree --name-only "$ref" \
            Tools/Scripts/webkitpy/benchmark_runner/data/plans/ 2>/dev/null \
        | sed -n 's#.*/\([^/]*\)\.plan$#\1#p' | sort
}
