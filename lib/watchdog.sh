# Run a long command so that its state is never in doubt.
#
# Builds fail in three different ways and they look alike from outside:
#
#   finished        exit code says what happened
#   died            process gone, exit code says what happened
#   hung            process alive, producing nothing, forever
#
# Only the third needs machinery, and it is the one that actually costs time,
# because the natural response is to keep waiting. So: watch the log for
# progress rather than watching the process for existence. A compiler that is
# alive but silent for ten minutes is indistinguishable from a deadlock, and
# should be reported as a problem either way.
#
# Everything here writes to stderr so it interleaves with, but never corrupts,
# the command's own output.

WK_POLL_SECONDS="${WK_POLL_SECONDS:-15}"      # how often to check for progress
WK_STALL_SECONDS="${WK_STALL_SECONDS:-300}"   # silence before warning
WK_ABORT_SECONDS="${WK_ABORT_SECONDS:-1800}"  # silence before giving up
WK_HEARTBEAT_SECONDS="${WK_HEARTBEAT_SECONDS:-60}"

_now() { date +%s; }
_fsize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null || echo 0; }

# Something useful to say in the heartbeat, rather than just "still going".
#
# ninja gives a counter, which is the best case: it says how far along the
# build is. xcodebuild gives no counter at all, so the fallback names the most
# recent action instead -- that answers "is it moving, and on what", which is
# the question the heartbeat exists for.
#
# Both read only the tail. A verbose xcodebuild log runs to hundreds of
# megabytes, and re-scanning it every minute would make the watchdog the most
# expensive thing in the build.
_progress_line() {
    local tail_bytes=65536 out

    out=$(tail -c "$tail_bytes" "$1" 2>/dev/null | tr '\r' '\n' \
          | grep -oE '\[[0-9]+/[0-9]+\]' | tail -1)
    [ -n "$out" ] && { printf '%s' "$out"; return 0; }

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

# Diagnostics printed when a stall is detected. The point is to answer, in one
# shot, the question you would otherwise spend ten minutes on: is anything
# actually running, and is it starved?
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
#
# Returns the command's exit status, or 124 if it was killed for stalling.
run_watched() {
    local log="$1"; shift
    [ "${1:-}" = -- ] && shift

    : > "$log"
    "$@" >>"$log" 2>&1 &
    local pid=$!

    local start last_size=0 last_change last_beat warned=0
    start=$(_now); last_change=$start; last_beat=$start

    while kill -0 "$pid" 2>/dev/null; do
        sleep "$WK_POLL_SECONDS"

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

        # Liveness, so a watcher never has to guess whether to keep waiting.
        if [ $(( now - last_beat )) -ge "$WK_HEARTBEAT_SECONDS" ]; then
            log "  ... $(_progress_line "$log" || echo running) ($(( (now - start) / 60 ))m elapsed)"
            last_beat=$now
        fi
    done

    wait "$pid"
}

# Pull the first real error out of a build log.
#
# Without this the answer to "why did it fail" is buried in hundreds of
# megabytes of progress lines, and the interesting line is near neither end:
# compilers keep going after the first error, so the tail is usually unrelated.
# On the real macOS build that found "No space left on device" at line 179,295
# of 180,000, where the tail said only "the Xcode build system has crashed".
#
# Two traps, both hit on real logs rather than imagined:
#
#   `error:` as a bare substring matches inside message text. An Objective-C
#   selector in a deprecation warning -- unarchivedObjectOfClass:fromData:error:
#   -- turned every one of those warnings into a reported error. Hence the
#   anchors: a diagnostic is at line start or after ": ".
#
#   Warnings can contain error-shaped text. Xcode emits
#   "warning: llvmcas://...: No such file or directory" by the hundred on a
#   perfectly good build, so warning lines are dropped outright.
first_error() {
    tr '\r' '\n' < "$1" 2>/dev/null \
        | grep -nE '(^FAILED:|^error:|: error:|: fatal error:|ninja: build stopped|No such file or directory)' \
        | grep -vE 'Performing Test|-- Failed|check for working|(^|[: ])warning:' \
        | head -5
}
