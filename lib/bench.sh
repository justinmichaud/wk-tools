wkdata() { python3 "$WK_ROOT/lib/wkdata.py" "$@"; }
wkslot() { python3 "$WK_ROOT/lib/wkslot.py" "$@"; }

BENCH_DIR="$WK_STORE/bench"
SEED_DIR="$WK_STORE/cache/bench"

# A task is one benchmarking command's output: $BENCH_DIR/<task>/ holds task.json, runs/<run>/, logs and reports. A live lock bench-task-<name> is what "running" means; no progress is stored.
bench_task_dir() { printf '%s/%s' "$BENCH_DIR" "$1"; }
bench_task_stamp() { date -u +%Y%m%dT%H%M%SZ; }

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

bench_running_tasks() {
    local d out=""
    for d in "$BENCH_DIR"/*/task.json; do
        [ -f "$d" ] || continue
        d=$(basename "$(dirname "$d")")
        lock_alive "bench-task-$d" && out="${out:+$out,}$d"
    done
    printf '%s' "$out"
}
RUNNER_DIR="$WK_STORE/cache/bench-runner"  # exported Tools/Scripts trees keyed by the WebKit commit: artifacts, never edited

plan_json() { # <plan>; the caller defines `bench_plan_read <path under Tools/Scripts>` -- a workspace's checkout for `wk bench`, the runner tree for `wk pi bench`
    local plan="$1" body seen=0
    while [ "$seen" -lt 5 ]; do
        body=$(bench_plan_read "webkitpy/benchmark_runner/data/plans/${plan}.plan" 2>/dev/null) \
            || die "no such plan: $plan (plans live in Tools/Scripts/webkitpy/benchmark_runner/data/plans)"
        # A plan file may contain nothing but the name of another plan.
        case "$(printf '%s' "$body" | head -c 1)" in
            '{') printf '%s' "$body"; return 0 ;;
            *)   plan=$(printf '%s' "$body" | tr -d ' \n' | sed 's/\.plan$//'); seen=$((seen + 1)) ;;
        esac
    done
    die "plan $plan indirects too many times"
}

# `wk bench --list` runs before any workspace exists to read a plan from, so it asks the mirror -- one git ls-tree, no export -- and returns 1 when there is no mirror.
bench_plan_list() {
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

seed_payload() { # <plan>: keyed by the exact commit, so a moved upstream branch makes a new directory; prints nothing when the plan has no fetchable source
    local plan="$1" json
    json=$(plan_json "$plan")

    local spec
    spec=$(printf '%s' "$json" | wkdata plan-spec 2>/dev/null) \
        || { warn "cannot pre-seed $plan; run-benchmark will fetch it itself"; echo ""; return 0; }

    # shellcheck disable=SC2086
    set -- $spec
    local kind="$1" url="$2" ref="$3" subdir="$4"
    [ "$kind" = git ] || { echo ""; return 0; }

    local sha
    sha=$(git ls-remote "$url" "$ref" 2>/dev/null | awk '{print $1; exit}')
    [ -n "$sha" ] || sha="$ref"     # already a commit

    ensure_dir "$SEED_DIR"
    local dest="$SEED_DIR/$plan-${sha:0:12}"
    if [ -d "$dest/.wk-seeded" ]; then
        debug "payload cached: $dest"
        [ -d "$dest/.git" ] && rm -rf "$dest/.git"  # repairs a cache still carrying one
        echo "$dest"; return 0
    fi

    info "seeding $plan payload from $url@${sha:0:12}"
    local tmp; tmp=$(mktemp -d "$SEED_DIR/.tmp-XXXXXX")
    if ! git clone -q "$url" "$tmp/repo" 2>/dev/null; then
        rm -rf "$tmp"
        warn "could not clone $url; run-benchmark will fetch the payload itself"
        echo ""; return 0
    fi
    git -C "$tmp/repo" checkout -q "$sha" 2>/dev/null || git -C "$tmp/repo" checkout -q "$ref"

    rm -rf "$dest"
    if [ "$subdir" = "." ]; then
        mv "$tmp/repo" "$dest"
    else
        mv "$tmp/repo/$subdir" "$dest"
    fi
    rm -rf "$tmp"
    # No .git in a pinned payload: a clone carries fsmonitor's Unix domain socket, which no copy tool can reproduce.
    rm -rf "$dest/.git"
    mkdir -p "$dest/.wk-seeded"
    printf 'url=%s\nref=%s\nsha=%s\nsubdir=%s\n' "$url" "$ref" "$sha" "$subdir" > "$dest/.wk-seeded/origin"
    info "seeded $dest"
    echo "$dest"
}

# The knobs are recorded into `configuration`, which `wk bench report` groups by; path_len is the pad *requested*, since what matters for grouping is the axis varied.
bench_configuration_args() {
    BENCH_CFG_ARGS=()
    [ "${WK_BENCH_ASLR:-}" = off ] && BENCH_CFG_ARGS+=("configuration.aslr=off")
    local pad="${WK_BENCH_ENV_PAD:-0}"
    case "$pad" in ''|*[!0-9]*) pad=0 ;; esac
    [ "$pad" -gt 0 ] && BENCH_CFG_ARGS+=("configuration.env_pad_bytes=$pad")
    local pl="${WK_BENCH_PATH_PAD:-0}"
    case "$pl" in ''|*[!0-9]*) pl=0 ;; esac
    [ "$pl" -gt 0 ] && BENCH_CFG_ARGS+=("configuration.path_len=$pl")
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
