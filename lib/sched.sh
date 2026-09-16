# Steps are declared into one file and scheduled by lib/sched.py, which runs each step's command and done-predicate in a shell sourcing the preludes named here. Bash declares; it holds no graph.

[ -n "${WK_ROOT:-}" ] || WK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v die >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"

wk() { "$WK_ROOT/wk" "$@"; }   # a step's command is the command a person types

SCHED_STEPS=""
SCHED_PRELUDE=("$WK_ROOT/lib/sched.sh")

sched_begin() { # <file> [prelude...] -- open a graph, and what a step's shell sources
    SCHED_STEPS="$1"; shift
    SCHED_PRELUDE=("$WK_ROOT/lib/sched.sh" "$@")
    : > "$SCHED_STEPS"
}

sched_step() { # <id> <machine> <needs> <holds> <done predicate> <command>
    [ -n "$SCHED_STEPS" ] || die "sched_step: no graph is open; sched_begin names the file"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "$6" >> "$SCHED_STEPS"
}

_sched() { # <mode> <the rest of lib/sched.py's arguments>
    local mode="$1" p; shift
    local args=()
    for p in "${SCHED_PRELUDE[@]}"; do args+=(--prelude "$p"); done
    python3 "$WK_ROOT/lib/sched.py" "$mode" "${args[@]}" "$@" < "$SCHED_STEPS"
}

sched_plan()  { _sched plan >&2; }
sched_steps() { _sched steps; }
sched_run()   { _sched run "$@"; }
