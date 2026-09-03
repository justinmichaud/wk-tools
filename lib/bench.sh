# What every benchmark lane shares: the result store, seeded payloads, the
# variance knobs' record, and the host-side runner tree a board lane drives
# a browser from. Sourced by cmd/bench and cmd/pi; neither carries a copy.

wkdata() { python3 "$WK_ROOT/lib/wkdata.py" "$@"; }
wkslot() { python3 "$WK_ROOT/lib/wkslot.py" "$@"; }

BENCH_DIR="$WK_STORE/bench"
SEED_DIR="$WK_STORE/cache/bench"

# --- tasks ------------------------------------------------------------------------
# A task is what one benchmarking command produced, and the unit `wk bench
# ls`, `wk bench report` and `wk status` speak in: $BENCH_DIR/<task>/ holds
# task.json (the request), runs/<run>/ (one directory per run), the command's
# logs and its reports. Its name is the moment it was requested and what it
# measures: <stamp>-wpe-pr1725, <stamp>-<sha12>, <stamp>-rpi3-base-vs-pr1725.
# The command that creates a task holds its lock (bench-task-<name>) for as
# long as it works on it; a live lock is what "running" means everywhere a
# task is reported, and nothing else is stored about its progress
# (lib/wkdata.py, task_state).
bench_task_dir() { printf '%s/%s' "$BENCH_DIR" "$1"; }
bench_task_stamp() { date -u +%Y%m%dT%H%M%SZ; }

# bench_task_new <name> <task-write fields...> -- creates the task and takes
# its lock; the caller keeps the lock by staying alive.
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

# bench_task_attach <name> -- an existing task another command created and
# holds the lock of (`wk ab` handing its task to `wk pi bench`); refused
# when there is no such task, so a typo cannot file runs into a fresh
# directory nothing describes.
bench_task_attach() {
    local dir; dir=$(bench_task_dir "$1")
    [ -f "$dir/task.json" ] || die "no such task '$1' ($dir has no task.json); 'wk bench ls' lists the tasks"
}

# The tasks whose lock is held right now, comma-separated: what --running
# hands to lib/wkdata.py so state is decided from the lock and nothing else.
bench_running_tasks() {
    local d out=""
    for d in "$BENCH_DIR"/*/task.json; do
        [ -f "$d" ] || continue
        d=$(basename "$(dirname "$d")")
        lock_alive "bench-task-$d" && out="${out:+$out,}$d"
    done
    printf '%s' "$out"
}
# Exported Tools/Scripts trees, keyed by the WebKit commit they came from:
# artifacts, re-exportable from the mirror, never edited in place.
RUNNER_DIR="$WK_STORE/cache/bench-runner"

# --- plans and payloads -----------------------------------------------------
# A plan is read wherever the caller's run-benchmark lives: a workspace's
# checkout for `wk bench`, the runner tree for `wk pi bench`. The caller
# defines `bench_plan_read <relative path under Tools/Scripts>` and this
# file asks it; one plan resolver, two places to read from.
plan_json() { # <plan>
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

# `wk bench --list` runs before any workspace exists to read a plan from, so
# it cannot go through bench_plan_read (a workspace's checkout) or
# bench_runner_tree (which exports a whole Tools/Scripts tree for a run --
# more than listing names needs). It reads the machine's own WebKit mirror
# instead -- one git ls-tree, read-only and instant, no export, no checkout.
# Prints one plan name per line and returns 0 when the mirror can answer;
# when it cannot (no mirror synced on this machine yet), prints the one
# place the list does live and returns 1 so the caller still has something
# to show.
bench_plan_list() {
    local mirror ref
    mirror=$(wk_mirror)
    ref="${WK_BENCH_RUNNER_REF:-refs/heads/main}"
    if [ ! -d "$mirror" ] || ! git -C "$mirror" rev-parse --verify --quiet "$ref^{commit}" >/dev/null; then
        printf "no mirror at %s to read plans from; 'wk sync' fetches one, or read\n" "$mirror"
        printf "them from a workspace's own checkout: Tools/Scripts/run-benchmark --list-plans\n"
        return 1
    fi
    git -C "$mirror" ls-tree --name-only "$ref" \
            Tools/Scripts/webkitpy/benchmark_runner/data/plans/ 2>/dev/null \
        | sed -n 's#.*/\([^/]*\)\.plan$#\1#p' | sort
}

# Seeded copies are keyed by the exact commit, so a moved upstream branch
# produces a new directory rather than quietly changing an existing one.
# Prints the seeded directory, or nothing when the plan has no fetchable
# source (run-benchmark then fetches it itself, and the run says so).
seed_payload() { # <plan>
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
        # Repairs a cache that may still carry a .git (dropped below), and
        # with it the fsmonitor socket that makes it more than a size issue.
        [ -d "$dest/.git" ] && rm -rf "$dest/.git"
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
    # No .git in a pinned payload: a clone carries git's runtime state,
    # including fsmonitor's Unix domain socket, which no copy tool can
    # reproduce.
    rm -rf "$dest/.git"
    mkdir -p "$dest/.wk-seeded"
    printf 'url=%s\nref=%s\nsha=%s\nsubdir=%s\n' "$url" "$ref" "$sha" "$subdir" > "$dest/.wk-seeded/origin"
    info "seeded $dest"
    echo "$dest"
}

# --- variance knobs -----------------------------------------------------------
# WK_BENCH_ASLR, WK_BENCH_ENV_PAD and WK_BENCH_PATH_PAD (wk bench -h). Turning
# one on is recorded into `configuration`, which `wk bench report` groups
# variance by; off contributes nothing. path_len records the pad *requested*,
# not a resolved path's length: what matters for grouping is which axis was
# varied. Into the global array BENCH_CFG_ARGS, as `wkdata env-record` fields.
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

# --- the runner tree ------------------------------------------------------------
# A board runs a browser and nothing else -- no python, no checkout -- so
# run-benchmark runs here, out of Tools/Scripts exported from the mirror at
# one commit, with this repo's board driver (bench/wk_board_driver.py) beside
# WebKit's own. Pinned to a commit so two arms of one A/B are driven by the
# same runner, and recorded with every result as runner_sha.
#
# Sets BENCH_RUNNER (the tree's path) and BENCH_RUNNER_SHA -- variables, not
# stdout, since a command substitution would lose the second. The export is
# the mirror's main (refs/heads/main; the other remotes sit under
# refs/remotes/<name>) by default, where run-benchmark's driver plugin loader
# is; WK_BENCH_RUNNER_REF names another ref for a runner an older lane needs.
bench_runner_tree() {
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
        # Exported into a sibling and renamed into place, so a tree that is
        # there is whole: a kill mid-export leaves a .tmp- nothing reads.
        ensure_dir "$RUNNER_DIR"
        local tmp; tmp=$(mktemp -d "$RUNNER_DIR/.tmp-XXXXXX")
        info "exporting run-benchmark from the mirror at ${sha:0:12} (Tools/Scripts only)"
        ( git -C "$mirror" archive "$sha" Tools/Scripts | tar -x -C "$tmp" ) \
            || { rm -rf "$tmp"; die "could not export Tools/Scripts at $sha from $mirror"; }
        [ -x "$tmp/Tools/Scripts/run-benchmark" ] || { rm -rf "$tmp"; die "the export has no Tools/Scripts/run-benchmark"; }
        rm -rf "$tree"; mv "$tmp" "$tree"
    fi
    # This repo's driver, refreshed on every use: the tree is an artifact of
    # the WebKit commit, the driver is this checkout's, and copying it is how
    # the two stay in step without the driver living in two places.
    cp "$WK_ROOT/bench/wk_board_driver.py" "$drivers/wk_board_driver.py" \
        || die "could not install the board driver into $drivers"
    BENCH_RUNNER="$tree"
}
