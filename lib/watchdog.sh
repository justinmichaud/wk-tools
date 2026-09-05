# A build's defaults; cmd/bench and cmd/pi set 900/5400, a benchmark reporting per subtest rather than streaming. The last two are shared with lib/detach.sh's remote poll loop.
WK_POLL_SECONDS="${WK_POLL_SECONDS:-15}"      # how often to check for progress
WK_STALL_SECONDS="${WK_STALL_SECONDS:-300}"   # silence before warning
WK_ABORT_SECONDS="${WK_ABORT_SECONDS:-1800}"  # silence before giving up
WK_HEARTBEAT_SECONDS="${WK_HEARTBEAT_SECONDS:-300}"  # how often to say "still going"

_now() { date +%s; }
_fsize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null || echo 0; }

# ninja gives a counter, xcodebuild none, so the fallback names the most recent action. Only the tail is read: a verbose xcodebuild log is hundreds of MB.
_progress_line() {
    local tail_bytes=65536 out

    out=$(tail -c "$tail_bytes" "$1" 2>/dev/null | tr '\r' '\n' \
          | grep -oE '\[[0-9]+/[0-9]+\]' | tail -1)
    [ -n "$out" ] && { printf '%s' "$out"; return 0; }

    out=$(tail -c "$tail_bytes" "$1" 2>/dev/null | tr '\r' '\n' \
          | grep -oE 'Start the iteration [0-9]+ of [0-9]+' | tail -1)
    [ -n "$out" ] && { printf 'iteration %s/%s' "$(printf '%s' "$out" | awk '{print $4}')" "$(printf '%s' "$out" | awk '{print $6}')"; return 0; }

    out=$(tail -c "$tail_bytes" "$1" 2>/dev/null | tr '\r' '\n' \
          | grep -oE '^(CompileC|CompileSwiftSources|SwiftCompile|SwiftDriver|Ld|Libtool|CodeSign|ScanDependencies|ProcessInfoPlistFile|GenerateDSYMFile) [^ ]+' \
          | tail -1)
    if [ -n "$out" ]; then
        printf '%s %s' "${out%% *}" "$(basename "${out##* }")"
        return 0
    fi
    return 1
}

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

# run_watched <logfile> -- <command...>: the command's status, or 124 killed for stalling. A hang is found in the log's progress, not the process's existence. The child is in the foreground, so INT/TERM here stops it; `on_interrupt` (lib/common.sh) covers a terminal whose process-group delivery misses it.
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

# Anchored, since a bare `error:` matches selector text (unarchivedObjectOfClass:fromData:error:) in every deprecation warning; warnings are dropped because Xcode emits hundreds of harmless "warning: llvmcas://...: No such file or directory".
first_error() {
    tr '\r' '\n' < "$1" 2>/dev/null \
        | grep -nE '(^FAILED:|^error:|: error:|: fatal error:|ninja: build stopped|No such file or directory)' \
        | grep -vE 'Performing Test|-- Failed|check for working|(^|[: ])warning:' \
        | head -5
}
