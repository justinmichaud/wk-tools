#!/bin/bash
# Capture a Linux perf profile of MiniBrowser with GC-section text markers and a
# group of hardware PMU events, for splitting per GC section with split-perf.py.
# Unlike capture.sh (samply, wall-clock stacks) this records cycles/instructions/
# cache events, so each section's IPC and cache-miss behaviour can be measured.
#
# Linux/GTK only. Requires a Release WebKit build with the GC text-marker patch,
# and perf (`linux-perf`) whose major.minor matches the running kernel.
#
# Usage:   capture-perf.sh <periodMS> <durationSec> <out.data> [url] [freqHz]
# Env overrides: WEBKIT_ROOT, WEBKIT_BUILD, PERF, TRACE_AUX, PERF_EVENTS
set -u

OS=$(uname -s)
[ "$OS" = "Linux" ] || { echo "capture-perf.sh is Linux-only (perf); use capture.sh on $OS." >&2; exit 1; }

ROOT="${WEBKIT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
if [ -z "$ROOT" ] || [ ! -d "$ROOT/Source/JavaScriptCore" ]; then
    echo "Set WEBKIT_ROOT to your WebKit checkout (or run from inside one)." >&2
    exit 1
fi
PERF="${PERF:-perf}"
command -v "$PERF" >/dev/null 2>&1 || { echo "perf not found; install linux-perf." >&2; exit 1; }

PERIOD_MS=${1:-60000}
DURATION=${2:-600}
OUT=${3:-/tmp/jsc-trace/perf.data}
URL=${4:-http://localhost:8080}
FREQ=${5:-4000}
AUX="${TRACE_AUX:-/tmp/jsc-trace-aux}"

# Leader is cycles, on the fixed counter. Five events fit the Neoverse-N1 PMU
# (cycles plus 4 of 6 programmable), so there is no multiplexing. Override with
# PERF_EVENTS for a different uarch.
EVENTS="${PERF_EVENTS:-cycles,instructions,l1d_cache_refill,ll_cache_miss_rd,stall_backend}"

mkdir -p "$AUX" "$(dirname "$OUT")"
rm -f "$AUX"/marker-*.txt "$AUX"/jit-*.dump

DIR="${WEBKIT_BUILD:-$ROOT/WebKitBuild/GTK/Release}"
MB="$DIR/bin/MiniBrowser"
export LD_LIBRARY_PATH="$DIR/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
# JIT dump segfaults the WebKitGTK process here; markers are all we need and GC is C++.
JSC_OPTS=(
    "useFixedIntervalGCOnly=1"
    "fixedIntervalGCPeriodMS=$PERIOD_MS"
    "useTextMarkers=1"
    "textMarkersDirectory=$AUX"
)
for o in "${JSC_OPTS[@]}"; do export "JSC_$o"; done
WEBPROCS=("WebKitWebProcess" "WebKitGPUProcess" "WebKitNetworkProcess")

[ -x "$MB" ] || { echo "MiniBrowser not found at: $MB (set WEBKIT_BUILD)." >&2; exit 1; }
if [ -r /proc/sys/kernel/perf_event_paranoid ]; then
    lvl=$(cat /proc/sys/kernel/perf_event_paranoid)
    [ "$lvl" -le 1 ] 2>/dev/null || echo "WARNING: perf_event_paranoid=$lvl (>1); perf may fail. Set it on the host to 1." >&2
fi

stop_browser() {
    pkill -x MiniBrowser 2>/dev/null
    for pat in "${WEBPROCS[@]}"; do pkill -f "$pat" 2>/dev/null; done
}

echo "MiniBrowser: $MB"
echo "URL:         $URL"
echo "GC period:   ${PERIOD_MS}ms   duration: ${DURATION}s   freq: ${FREQ}Hz"
echo "events:      $EVENTS"
echo "aux dir:     $AUX   output: $OUT"
echo

stop_browser
sleep 1

# --clockid=monotonic puts perf sample timestamps on CLOCK_MONOTONIC, the same
# base as JSC's MonotonicTime markers, so split-perf.py needs no conversion. perf
# records the launched workload and its children, so the forked WebKitWebProcess
# where GC runs is captured too.
#
# Call graphs are off by default: a callchain per sample makes perf.data ~10x
# larger and the perf-script Python pass minutes-long. Set CALLGRAPH=fp for
# split-perf.py's cache-miss flamegraph (.folded) output.
CG=()
[ "${CALLGRAPH:-}" = "" ] || CG=(--call-graph "${CALLGRAPH}")
"$PERF" record -e "$EVENTS" -F "$FREQ" --clockid=monotonic "${CG[@]}" \
    -o "$OUT" -- "$MB" "$URL" > /tmp/jsc-trace-perf.log 2>&1 &
PERF_PID=$!

sleep "$DURATION"

echo "stopping browser, letting perf finalize ..."
stop_browser
for _ in $(seq 1 60); do kill -0 "$PERF_PID" 2>/dev/null || break; sleep 1; done
if kill -0 "$PERF_PID" 2>/dev/null; then
    kill -INT "$PERF_PID" 2>/dev/null
    for _ in $(seq 1 30); do kill -0 "$PERF_PID" 2>/dev/null || break; sleep 1; done
fi
stop_browser

echo
echo "=== marker files ==="
ls -la "$AUX"/marker-*.txt 2>/dev/null
echo "GC marker breakdown:"; cat "$AUX"/marker-*.txt 2>/dev/null | awk '{$1="";$2="";print}' | sed 's/^  //' | sort | uniq -c | sort -rn
echo "=== output ==="; ls -la "$OUT" 2>/dev/null
echo "next: TRACE_AUX=$AUX $PERF script -i $OUT -s $(dirname "$0")/split-perf.py"
