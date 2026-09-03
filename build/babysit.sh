#!/usr/bin/env bash
#
# The detached half of `wk build --babysit`: build, and when the build fails,
# have Claude fix it from inside the workspace, then build again.
#
# Runs on the host under nohup with no terminal, so it survives the ssh
# session that started it. Everything it wants to say goes to babysit.log;
# everything a human should read afterwards goes to babysit.report; and its
# current state goes to babysit.status, which `wk status` renders.
#
# The agent runs through `wk ai claude` -- never claude directly -- so every fix
# attempt passes the same sandbox verification an interactive session does,
# and refuses the same targets. The babysitter itself edits nothing: the model
# does, inside the workspace, or nobody does.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/watchdog.sh"
. "$WK_ROOT/lib/target.sh"

NAME="$1"; CONFIG="$2"; MODEL="$3"; MAX="$4"; BRANCH="$5"; shift 5

TARGET="$(ws_target "$NAME")"
load_target "$TARGET"

WS=$(wk_ws_dir "$NAME")
BLOG="$WS/build.log"
REPORT="$WS/babysit.report"
STATUSF="$WS/babysit.status"
ATTEMPT=0

bs_status() {
    cat > "$STATUSF" <<EOF
state=$1
config=$CONFIG
model=$MODEL
attempt=$ATTEMPT
max=$MAX
pid=$$
report=$REPORT
log=$WS/babysit.log
updated=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
}

note() {
    printf '=== %s  %s ===\n%s\n\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" >> "$REPORT"
}

# A fresh run is a fresh report. The status file outlives the run on purpose --
# it is how `wk status` still shows the outcome tomorrow -- but stale fix notes
# under a new one would read as part of it.
: > "$REPORT"
bs_status starting
info "babysitting '$CONFIG' in '$NAME' (model $MODEL, up to $MAX fixes)"

# The branch, first and once. Not per attempt: a fix the model just made must
# not be checked out from under it.
if [ -n "$BRANCH" ]; then
    info "checking out '$BRANCH'"
    if ! t_exec "$NAME" bash -c "cd $(sh_quote "$(t_src "$NAME")") && {
            git checkout -q $(sh_quote "$BRANCH") 2>/dev/null ||
            { $(origin_branch_fetch_step "$BRANCH" "$(t_mirror_dir "$NAME")") &&
              git checkout -q $(sh_quote "$BRANCH"); }; }"; then
        bs_status error
        note "gave up before building" "could not check out branch '$BRANCH'"
        die "could not check out '$BRANCH' in '$NAME'"
    fi
    note "checkout" "built from branch '$BRANCH'"
fi

while :; do
    bs_status building
    rc=0
    "$WK_ROOT/wk" build "$NAME" "$CONFIG" ${@+"$@"} || rc=$?

    if [ "$rc" -eq 0 ]; then
        bs_status ok
        if [ "$ATTEMPT" -eq 0 ]; then
            note "done" "build succeeded on its own -- nothing to fix"
        else
            note "done" "build succeeded after $ATTEMPT fix(es)"
        fi
        info "BUILD OK after $ATTEMPT fix(es) -- report: $REPORT"
        exit 0
    fi

    # A stall is memory pressure or a wedged process, not a source defect; a
    # model cannot edit its way out of it, and rebuilding to find out is an
    # hour of the same.
    if [ "$rc" -eq 124 ]; then
        bs_status stalled
        note "gave up" "the build stalled (no output; exit 124). That is load or
memory, not source -- nothing for a fix attempt to act on. See $BLOG"
        die "build stalled; not something a fix can reach"
    fi

    ATTEMPT=$((ATTEMPT + 1))
    if [ "$ATTEMPT" -gt "$MAX" ]; then
        ATTEMPT=$((ATTEMPT - 1))
        bs_status gave-up
        note "gave up" "still failing after $MAX fix attempt(s); last exit $rc.
The log is $BLOG; the attempts above say what was tried."
        die "gave up after $MAX fix attempts"
    fi

    bs_status fixing
    info "build failed (exit $rc) -- fix attempt $ATTEMPT of $MAX"

    # What the model gets: the classified errors, then the raw tail. The log
    # lives on the host and the model runs in the workspace, so the excerpt
    # goes in the prompt rather than by path.
    ERRS=$(first_error "$BLOG" 2>/dev/null || true)
    TAIL=$(tail -c 8000 "$BLOG" 2>/dev/null || true)
    PROMPT="You are an unattended build-fixer. The '$CONFIG' build of the WebKit
checkout in the current directory failed (exit $rc); this is fix attempt
$ATTEMPT of $MAX. Find the cause and fix it in the checkout, then stop.

Rules: make the smallest change that fixes the build, following the
repository's house rules. Do not run the full build -- the babysitter reruns
it when you finish -- but you may syntax-check or compile a single file. If
the failure is not fixable from inside the workspace (toolchain, disk,
network), say so plainly and change nothing.

End your reply with a short paragraph: what failed, what you changed, and
which files you touched.

Build errors (classified):
$ERRS

Log tail:
$TAIL"

    FIX_RC=0
    # WK_NAME, not a positional: cmd/ai reads the workspace from the
    # environment the dispatcher sets (this runs detached, with no dispatcher
    # above it), and everything in its argv after the agent word belongs to the
    # agent -- a name passed there would reach `claude` as a stray argument.
    FIX_OUT=$(WK_NAME="$NAME" "$WK_ROOT/cmd/ai" claude --model "$MODEL" -p "$PROMPT" </dev/null 2>>"$WS/babysit.log") || FIX_RC=$?
    note "fix attempt $ATTEMPT (exit $FIX_RC)" "$FIX_OUT"

    # The agent failing to *run* is not a failed fix, it is the loop's own
    # substrate gone -- a broken sandbox, a dead guest, no model. Retrying
    # would fail identically forever.
    if [ "$FIX_RC" -ne 0 ] && [ -z "$FIX_OUT" ]; then
        bs_status error
        note "gave up" "claude did not run (exit $FIX_RC) -- see $WS/babysit.log"
        die "claude did not run (exit $FIX_RC)"
    fi
done
