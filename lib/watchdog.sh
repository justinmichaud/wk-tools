# A build's defaults; cmd/bench and cmd/pi set 900/5400, a benchmark reporting per subtest rather than streaming. The last two are shared with lib/detach.sh's remote poll loop.
WK_POLL_SECONDS="${WK_POLL_SECONDS:-15}"      # how often to check for progress
WK_STALL_SECONDS="${WK_STALL_SECONDS:-300}"   # silence before warning
WK_ABORT_SECONDS="${WK_ABORT_SECONDS:-1800}"  # silence before giving up
WK_HEARTBEAT_SECONDS="${WK_HEARTBEAT_SECONDS:-300}"  # how often to say "still going"

command -v build_processes >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
command -v task_field     >/dev/null 2>&1 || . "$WK_ROOT/lib/task.sh"

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
    local log="$1" idle="$2" n
    n=$(build_processes)
    if [ "${n:-0}" -gt 0 ]; then
        warn "no output for ${idle}s, and this machine is running $n compiler/linker process(es) -- a full-LTO link is silent for minutes at a time"
        log  "  busiest:       $(busiest_process)"
    else
        warn "no output for ${idle}s, and nothing here is compiling or linking"
    fi
    log  "  last progress: $(_progress_line "$log" || echo unknown)"
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
# Killing the job is not killing what the job started: run-benchmark spawns its own http server, and a TERM to the parent alone orphans it -- one such server was still holding a port eleven days later (measured 2026-09-10). Descendants first, so nothing is left holding a port or a device once the job is gone. (build/mem-watchdog.sh has its own, inside the target, over a pid list it already has.)
_watched_descendants() { # <pid> -- depth first, children before parents
    local p="$1" kid
    for kid in $(pgrep -P "$p" 2>/dev/null); do _watched_descendants "$kid"; done
    printf '%s\n' "$p"
}

watched_kill() { # <pid> <signal>
    local pid="$1" sig="$2" p
    for p in $(_watched_descendants "$pid"); do
        [ "$p" = "$$" ] && continue
        kill "-$sig" "$p" 2>/dev/null || true
    done
}

run_watched() {
    local log="$1"; shift
    [ "${1:-}" = -- ] && shift

    : > "$log"
    "$@" >>"$log" 2>&1 &
    local pid=$!

    _run_watched_interrupted() {
        watched_kill "$pid" TERM
        wk_sleep 2
        watched_kill "$pid" KILL
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
            watched_kill "$pid" TERM
            sleep 5
            watched_kill "$pid" KILL
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

# A pid from inside a workspace is the workspace's own claim: it arrives down a log the workspace can write, or in a pid file in its home, and a wkdev container shares the host's PID namespace (wkdev-create passes --pid host), so an unchecked pid is a signal at another workspace's build or at anything else on the machine. So a pid is adopted, and later signalled, only while its command line inside the target matches the pattern its job declared (`pid_match`).
_job_pid_args() { # <ws> <pid> -- its command line inside the target, empty when the pid is gone
    t_exec "$1" ps -o args= -p "$2" 2>/dev/null | tr -d '\r' | tr '\n' ' '
}

# The patterns are one word each and separated by spaces, because a job's pid has more than one shape: build-in-target.sh execs the port's build script, so the same pid is the driver before the exec and the script after it. `|` inside an expansion is not alternation to `case` or `[[`, hence the loop.
_job_pid_matches() { # <command line> <patterns> -- 0 when one of them matches
    local args="$1" p
    for p in $2; do
        case "$args" in $p) return 0 ;; esac
    done
    return 1
}

job_pid_adopt() { # <ws> <task dir> <pid> <patterns its command line must match one of> -- 0 when the pid is the job's
    local ws="$1" dir="$2" pid="$3" want="$4" args
    [ -n "$want" ] || die "job_pid_adopt: ${dir##*/} named no pattern for pid $pid's command line"
    args=$(_job_pid_args "$ws" "$pid")
    if _job_pid_matches "$args" "$want"; then
        task_set "$dir" pid_match "$want"
        task_pid "$dir" "$pid"
        task_set "$dir" where target
        return 0
    fi
    warn "'$ws' names pid $pid as its job, and that pid inside '$ws' is running
  '${args:-nothing -- it is already gone}', not $want. It is not adopted, so
  nothing here will signal it; stop the job where it runs:  wk enter $ws"
    return 1
}

# The job announces its pid down its log (`wk: <label> pid <n>`, build/build-in-target.sh and cmd/test's inner shell), the one channel back from every target kind, and runs in the background because the job holds the shell that started it. `where` goes last, so no reader tests the target's pid against this kernel. WK_JOB_PID_TRIES bounds the wait at one second a try.
job_pid_watch() { # <task dir> <log> <label> <patterns the pid's command line must match one of>
    local dir="$1" log="$2" label="$3" want="${4:-}" i=0 pid ws
    ws=$(task_field "$dir" name)
    while [ "$i" -lt "${WK_JOB_PID_TRIES:-900}" ]; do
        pid=$(sed -n "s/^wk: $label pid \([0-9][0-9]*\).*/\1/p" "$log" 2>/dev/null | head -1)
        if [ -n "$pid" ]; then
            job_pid_adopt "$ws" "$dir" "$pid" "$want"
            return $?
        fi
        wk_sleep 1
        i=$((i + 1))
    done
    return 1
}

# Descendants first: ninja's children reparent to init the moment their parent is gone. No pattern kill -- a wkdev container shares the host's PID namespace (wkdev-create passes --pid host), so `pkill -f <build dir>` in one matches another workspace's build of the same config. WK_KILL_WAIT is the seconds a TERM gets before the KILL.
job_kill() { # <ws> <task dir> <word to end the record with> -- 0 when it is gone
    local ws="$1" dir="$2" word="$3" pid i=0
    pid=$(task_field "$dir" pid)
    if [ -z "$pid" ] || [ "$pid" = "$$" ]; then task_end "$dir" "$word"; return 0; fi   # `$$` is this driver, the pid the record holds until the job announces its own, and it is leaving anyway

    _job_signal "$ws" "$dir" "$pid" TERM
    while [ "$i" -lt "${WK_KILL_WAIT:-15}" ] && task_alive "$dir"; do
        wk_sleep 1; i=$((i + 1))
    done
    if task_alive "$dir"; then
        warn "pid $pid did not stop on TERM after ${i}s -- killing it"
        _job_signal "$ws" "$dir" "$pid" KILL
        i=0
        while [ "$i" -lt 5 ] && task_alive "$dir"; do wk_sleep 1; i=$((i + 1)); done
    fi
    local left=0; task_alive "$dir" && left=1
    task_end "$dir" "$word"
    [ "$left" = 0 ]
}

# What `wk build --kill` and `wk test --kill` do, once.
job_stop() { # <ws> <kind> -- 0 stopped, 2 nothing was running, 1 it outlived a KILL
    local ws="$1" kind="$2" dir
    dir=$(task_find "$kind" "$ws")
    if [ -z "$dir" ] || ! task_alive "$dir"; then
        log "no $kind is running in '$ws' -- 'wk status $ws' says what it last did"
        return 2
    fi
    info "stopping the $kind in '$ws' (pid $(task_field "$dir" pid) on $(task_field "$dir" machine))"
    local rc=0
    job_kill "$ws" "$dir" cancelled || rc=1
    t_task_put "$ws" "$dir"
    [ "$rc" = 0 ] && info "stopped '$ws's $kind and recorded it as cancelled"
    return "$rc"
}

# `wk ai claude <ws> --rc --stop`, and `wk stop` / `wk rm` taking its workspace.
rc_task()  { task_find rc "$1"; }

rc_alive() { # <ws>
    local dir; dir=$(rc_task "$1")
    [ -n "$dir" ] || return 1
    task_alive "$dir"
}

rc_stop() { # <ws>
    local name="$1" dir
    dir=$(rc_task "$name")

    if ! rc_alive "$name"; then
        info "claude remote-control is not running in '$name'"
        [ -z "$dir" ] || task_end "$dir" stopped
        return 0
    fi

    info "stopping claude remote-control in '$name' (pid $(task_field "$dir" pid))"
    job_kill "$name" "$dir" stopped \
        || die "claude remote-control in '$name' outlived a KILL, so it is still
    running:  wk enter $name"
}

_job_signal() { # <ws> <task dir> <pid> <signal>
    local ws="$1" dir="$2" pid="$3" sig="$4"
    if [ "$(task_field "$dir" where)" = target ]; then
        local want args
        want=$(task_field "$dir" pid_match)
        [ -n "$want" ] || die "the record ${dir##*/} holds pid $pid inside '$ws' and no pattern its
    command line must match, so nothing can tell it from any other pid in a
    shared PID namespace. Whatever adopted that pid did not go through
    job_pid_adopt (lib/watchdog.sh), which is a bug."
        args=$(_job_pid_args "$ws" "$pid")
        [ -n "$args" ] || return 0   # gone between the liveness read and here: nothing to signal
        _job_pid_matches "$args" "$want" || die "refusing to send $sig to pid $pid inside '$ws': it is running
    '$args', not $want. The pid is what the workspace announced, and this one
    is another process -- in a shared PID namespace it could be another
    workspace's build. Stop the job where it runs:  wk enter $ws"
        t_exec "$ws" bash -c "$(declare -f _watched_descendants)
for p in \$(_watched_descendants $pid); do kill -$sig \"\$p\" 2>/dev/null || true; done
true" >/dev/null 2>&1 || true
        return 0
    fi
    watched_kill "$pid" "$sig"
}

# Anchored, since a bare `error:` matches selector text (unarchivedObjectOfClass:fromData:error:) in every deprecation warning; warnings are dropped because Xcode emits hundreds of harmless "warning: llvmcas://...: No such file or directory".
first_error() {
    tr '\r' '\n' < "$1" 2>/dev/null \
        | grep -nE '(^FAILED:|^error:|: error:|: fatal error:|ninja: build stopped|No such file or directory)' \
        | grep -vE 'Performing Test|-- Failed|check for working|(^|[: ])warning:' \
        | head -5
}
