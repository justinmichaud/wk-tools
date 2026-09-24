wkdata() { python3 "$WK_ROOT/lib/wkdata.py" "$@"; }
wkslot() { python3 "$WK_ROOT/lib/wkslot.py" "$@"; }

BENCH_DIR="$(wk_bench_dir)"
SEED_DIR="$(wk_artifact_dir)/bench"

# What a driver's b_bench_put never carries onto a benchmark install: a repository's history, and byte-compiled python whose source travels beside it.
BENCH_PUT_SKIP=".git __pycache__"
bench_put_excludes() {  # -> `--exclude X` per name, for tar and rsync alike. Unquoted, because the caller word-splits this into an argv and word splitting does not remove quotes: `--exclude '.git'` excludes a name no file has, and carries the thing it names. Safe only because the list above is fixed and holds no metacharacter.
    local x out=""
    for x in $BENCH_PUT_SKIP; do out="$out --exclude $x"; done
    printf '%s' "$out"
}

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

bench_task_attach() {
    local dir; dir=$(bench_task_dir "$1")
    [ -f "$dir/task.json" ] || die "no such task '$1' ($dir has no task.json); 'wk bench ls' lists the tasks"
}

RUNNER_DIR="$(wk_artifact_dir)/bench-runner"  # exported Tools/Scripts trees keyed by the WebKit commit: artifacts, never edited

plan_json() { # <plan>; the caller defines `bench_plan_read <path under Tools/Scripts>` -- a workspace's checkout for `wk bench`, the runner tree for `wk pi bench`
    local plan="$1" body seen=0
    while [ "$seen" -lt 5 ]; do
        body=$(bench_plan_read "webkitpy/benchmark_runner/data/plans/${plan}.plan" 2>/dev/null) \
            || die "no such plan: $plan (plans live in Tools/Scripts/webkitpy/benchmark_runner/data/plans)"
        case "$(printf '%s' "$body" | head -c 1)" in
            '{') printf '%s' "$body"; return 0 ;;
            *)   plan=$(printf '%s' "$body" | tr -d ' \n' | sed 's/\.plan$//'); seen=$((seen + 1)) ;;
        esac
    done
    die "plan $plan indirects too many times"
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

seed_payload() { # <plan>: the pinned payload's directory, or nothing when the plan has no fetchable source (lib/wk/bench/seed.py)
    local json
    json=$(plan_json "$1")
    printf '%s' "$json" | PYTHONPATH="$WK_ROOT/lib" python3 -m wk.bench.seed "$SEED_DIR" "$1"
}

bench_configuration_args() {  # BENCH_CFG_ARGS: the knobs' configuration.* fields (lib/wk/bench/pipeline.py)
    BENCH_CFG_ARGS=()
    local f
    while IFS= read -r f; do
        [ -z "$f" ] || BENCH_CFG_ARGS+=("$f")
    done < <(PYTHONPATH="$WK_ROOT/lib" python3 -m wk.bench.pipeline configuration)
    return 0
}

# A board runs a browser and nothing else -- no python, no checkout -- so run-benchmark runs here, from Tools/Scripts exported at one mirror commit, kept as runner_sha so both arms of an A/B share a runner.
bench_runner_tree() {  # sets BENCH_RUNNER and BENCH_RUNNER_SHA
    local mirror ref sha tree drivers
    mirror=$(wk_mirror)
    [ -d "$mirror" ] || die "no mirror at $mirror; 'wk sync' makes one. The runner tree is exported from it."
    ref="${WK_BENCH_RUNNER_REF:-refs/heads/main}"
    sha=$(git -C "$mirror" rev-parse --verify --quiet "$ref^{commit}") \
        || die "the mirror has no '$ref' to export a runner from ('wk sync' fetches main)"
    BENCH_RUNNER_SHA="$sha"
    tree="$RUNNER_DIR/${sha:0:12}"
    drivers="$tree/Tools/Scripts/webkitpy/benchmark_runner/browser_driver"

    if [ ! -x "$tree/Tools/Scripts/run-benchmark" ]; then
        ensure_dir "$RUNNER_DIR"
        local tmp; tmp=$(mktemp -d "$RUNNER_DIR/.tmp-XXXXXX")  # renamed into place, so a kill mid-export leaves a .tmp- nothing reads
        info "exporting run-benchmark from the mirror at ${sha:0:12} (Tools/Scripts only)"
        ( git -C "$mirror" archive "$sha" Tools/Scripts | tar -x -C "$tmp" ) \
            || { rm -rf "$tmp"; die "could not export Tools/Scripts at $sha from $mirror"; }
        [ -x "$tmp/Tools/Scripts/run-benchmark" ] || { rm -rf "$tmp"; die "the export has no Tools/Scripts/run-benchmark"; }
        rm -rf "$tree"; mv "$tmp" "$tree"
    fi
    cp "$WK_ROOT/bench/wk_board_driver.py" "$drivers/wk_board_driver.py" \
        || die "could not install the board driver into $drivers"
    BENCH_RUNNER="$tree"
}
