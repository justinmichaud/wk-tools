# Run a long command so that its state is never in doubt.
# Builds fail three ways that look alike from outside: finished (exit code
# says what happened), died (process gone, exit code says what happened),
# hung (process alive, producing nothing, forever). Only the third needs
# machinery, since the natural response is to keep waiting -- so watch the
# log for progress rather than the process for existence. A compiler alive
# but silent for ten minutes is indistinguishable from a deadlock.
# Everything here writes to stderr so it interleaves with, but never
# corrupts, the command's own output.

# WK_POLL_SECONDS, WK_STALL_SECONDS, WK_ABORT_SECONDS, WK_HEARTBEAT_SECONDS:
# overridable for a slower or noisier build than the defaults below assume.
# WK_STALL_SECONDS and WK_HEARTBEAT_SECONDS share a name and a default with
# lib/detach.sh's remote poll loop -- one value for "silence before warning"
# and "how often to say still going" everywhere a job is watched.
#
# The defaults below are a build's. A benchmark (cmd/bench, cmd/pi) sets
# WK_STALL_SECONDS/WK_ABORT_SECONDS to 900/5400 before sourcing this file,
# because a benchmark reports once per subtest rather than streaming compiler
# output; image/yocto.sh's poll loop reads the same 900 for the same reason.
WK_POLL_SECONDS="${WK_POLL_SECONDS:-15}"      # how often to check for progress
WK_STALL_SECONDS="${WK_STALL_SECONDS:-300}"   # silence before warning
WK_ABORT_SECONDS="${WK_ABORT_SECONDS:-1800}"  # silence before giving up
WK_HEARTBEAT_SECONDS="${WK_HEARTBEAT_SECONDS:-300}"  # how often to say "still going"

_now() { date +%s; }
_fsize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null || echo 0; }

# Something useful to say in the heartbeat, rather than just "still going".
# ninja gives a counter; xcodebuild gives none, so the fallback names the
# most recent action instead, answering "is it moving, and on what." Both
# read only the tail -- a verbose xcodebuild log runs to hundreds of
# megabytes, and re-scanning it every minute would make the watchdog the
# most expensive thing in the build.
_progress_line() {
    local tail_bytes=65536 out

    out=$(tail -c "$tail_bytes" "$1" 2>/dev/null | tr '\r' '\n' \
          | grep -oE '\[[0-9]+/[0-9]+\]' | tail -1)
    [ -n "$out" ] && { printf '%s' "$out"; return 0; }

    # run-benchmark (wk pi bench, wk bench): "Start the iteration 2 of 4".
    out=$(tail -c "$tail_bytes" "$1" 2>/dev/null | tr '\r' '\n' \
          | grep -oE 'Start the iteration [0-9]+ of [0-9]+' | tail -1)
    [ -n "$out" ] && { printf 'iteration %s/%s' "$(printf '%s' "$out" | awk '{print $4}')" "$(printf '%s' "$out" | awk '{print $6}')"; return 0; }

    out=$(tail -c "$tail_bytes" "$1" 2>/dev/null | tr '\r' '\n' \
          | grep -oE '^(CompileC|CompileSwiftSources|SwiftCompile|SwiftDriver|Ld|Libtool|CodeSign|ScanDependencies|ProcessInfoPlistFile|GenerateDSYMFile) [^ ]+' \
          | tail -1)
    if [ -n "$out" ]; then
        # "CompileC /very/long/path/foo.o" -> "CompileC foo.o"
        printf '%s %s' "${out%% *}" "$(basename "${out##* }")"
        return 0
    fi
    return 1
}

# Diagnostics for a detected stall: is anything actually running, and is it
# starved -- the question you'd otherwise spend ten minutes on.
_stall_report() {
    local log="$1" idle="$2"
    warn "no output for ${idle}s -- possible stall"
    log  "  last progress: $(_progress_line "$log" || echo unknown)"
    log  "  compilers:     $(ps -eo comm= 2>/dev/null | grep -cE '^(cc1plus|clang|ld|lld|ninja)' 2>/dev/null || true)"
    if [ -r /proc/meminfo ]; then
        log  "  memory:        $(awk '/^MemAvailable:/ {printf "%d MB available", $2/1024}' /proc/meminfo)"
    fi
    if [ -r /sys/fs/cgroup/memory.events ]; then
        local oom; oom=$(awk '/^oom_kill /{print $2}' /sys/fs/cgroup/memory.events 2>/dev/null)
        [ -n "$oom" ] && [ "$oom" != 0 ] && warn "  cgroup has OOM-killed $oom process(es) -- lower the job count"
    fi
    log  "  tail: $(tr '\r' '\n' < "$log" 2>/dev/null | grep -v '^$' | tail -1 | cut -c1-100)"
}

# run_watched <logfile> -- <command...>
# Returns the command's exit status, or 124 if it was killed for stalling.
# The watched command is a foreground child of this process (not detached,
# unlike lib/detach.sh's jobs), so INT/TERM here means stop it, not merely
# stop watching -- otherwise a Ctrl-C during a build leaves the compiler
# running unattended. `on_interrupt` (lib/common.sh) covers the case a
# real terminal's process-group delivery doesn't reach the child.
run_watched() {
    local log="$1"; shift
    [ "${1:-}" = -- ] && shift

    : > "$log"
    "$@" >>"$log" 2>&1 &
    local pid=$!

    _run_watched_interrupted() {
        kill -TERM "$pid" 2>/dev/null || true
        wk_sleep 2
        kill -KILL "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    }
    on_interrupt _run_watched_interrupted

    local start last_size=0 last_change last_beat warned=0
    start=$(_now); last_change=$start; last_beat=$start

    while kill -0 "$pid" 2>/dev/null; do
        wk_sleep "$WK_POLL_SECONDS"

        local size now idle
        size=$(_fsize "$log"); now=$(_now)

        if [ "$size" != "$last_size" ]; then
            last_size=$size; last_change=$now; warned=0
        fi

        idle=$(( now - last_change ))

        if [ "$idle" -ge "$WK_ABORT_SECONDS" ]; then
            warn "no output for ${idle}s -- giving up and killing the job"
            _stall_report "$log" "$idle"
            kill -TERM "$pid" 2>/dev/null
            sleep 5
            kill -KILL "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
            return 124
        fi

        if [ "$idle" -ge "$WK_STALL_SECONDS" ] && [ "$warned" -eq 0 ]; then
            _stall_report "$log" "$idle"
            log "  will abort if still silent at ${WK_ABORT_SECONDS}s"
            warned=1
        fi

        if [ $(( now - last_beat )) -ge "$WK_HEARTBEAT_SECONDS" ]; then
            log "  ... $(_progress_line "$log" || echo running) ($(( (now - start) / 60 ))m elapsed)"
            last_beat=$now
        fi
    done

    wait "$pid"
}

# Pull the first real error out of a build log: compilers keep going after
# the first error, so the tail is usually unrelated and the interesting
# line is buried near neither end.
# `error:` as a bare substring matches inside message text -- an
# Objective-C selector like unarchivedObjectOfClass:fromData:error: reads
# as an error in every deprecation warning that names it. Hence the
# anchors: a diagnostic is at line start or after ": ". Warnings can also
# contain error-shaped text (Xcode's "warning: llvmcas://...: No such file
# or directory" by the hundred on a good build), so warning lines are
# dropped outright.
first_error() {
    tr '\r' '\n' < "$1" 2>/dev/null \
        | grep -nE '(^FAILED:|^error:|: error:|: fatal error:|ninja: build stopped|No such file or directory)' \
        | grep -vE 'Performing Test|-- Failed|check for working|(^|[: ])warning:' \
        | head -5
}
