#!/bin/bash
# A samply trace of MiniBrowser with GC-section text markers and JIT data, to split per GC section with split-trace.py. Runs on macOS (Apple WebKit, XPC web process) and Linux (GTK WebKit, the sandbox disabled so the web process inherits the env), and needs a Release WebKit build with the GC text-marker patch and samply on PATH. `wk profile --mode samply` composes the whole invocation and is the shorter road than this script.
# Usage:  capture.sh <periodMS> <durationSec> <out.json.gz> [url] [rateHz]  -- env overrides WEBKIT_ROOT, WEBKIT_BUILD, SAMPLY, TRACE_AUX, JITDUMP
set -u

OS=$(uname -s)
ROOT="${WEBKIT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
if [ -z "$ROOT" ] || [ ! -d "$ROOT/Source/JavaScriptCore" ]; then
    echo "Set WEBKIT_ROOT to your WebKit checkout (or run from inside one)." >&2
    exit 1
fi
SAMPLY="${SAMPLY:-$(command -v samply || echo samply)}"
command -v "$SAMPLY" >/dev/null 2>&1 || SAMPLY=samply

PERIOD_MS=${1:-30000}
DURATION=${2:-600}
OUT=${3:-/tmp/jsc-trace/trace.json.gz}
URL=${4:-http://localhost:8080}
RATE=${5:-1000}
AUX="${TRACE_AUX:-/tmp/jsc-trace-aux}"

mkdir -p "$AUX" "$(dirname "$OUT")"
rm -f "$AUX"/marker-*.txt "$AUX"/jit-*.dump

JSC_OPTS=(   # periodic full GC only, coarse GC-section markers into a fixed directory, so the file names are deterministic and samply can read them
    "useFixedIntervalGCOnly=1"
    "fixedIntervalGCPeriodMS=$PERIOD_MS"
    "useTextMarkers=1"
    "textMarkersDirectory=$AUX"
)

# JIT dump gives JS/JIT frame symbols, and on Linux/GTK JSC_useJITDump=1 kills the web process within ~2s under samply, its perf jitdump mmap colliding with samply's own perf session; GC sections are C++ and need no JIT symbols, so it is off there and on under macOS.
if [ -z "${JITDUMP:-}" ]; then
    [ "$OS" = "Darwin" ] && JITDUMP=1 || JITDUMP=0
fi
if [ "$JITDUMP" = 1 ]; then
    JSC_OPTS+=("useJITDump=1" "jitDumpDirectory=$AUX")
fi

if [ "$OS" = "Darwin" ]; then
    DIR="${WEBKIT_BUILD:-$ROOT/WebKitBuild/Release}"
    MB="$DIR/MiniBrowser.app/Contents/MacOS/MiniBrowser"
    export DYLD_FRAMEWORK_PATH="$DIR" __XPC_DYLD_FRAMEWORK_PATH="$DIR"
    export DYLD_LIBRARY_PATH="$DIR" __XPC_DYLD_LIBRARY_PATH="$DIR"
    for o in "${JSC_OPTS[@]}"; do export "JSC_$o"; export "__XPC_JSC_$o"; done   # WebContent is an XPC service, and libxpc forwards only __XPC_-prefixed env
    WEBPROCS=("com.apple.WebKit.WebContent" "com.apple.WebKit.GPU" "com.apple.WebKit.Networking")
else
    DIR="${WEBKIT_BUILD:-$ROOT/WebKitBuild/GTK/Release}"
    MB="$DIR/bin/MiniBrowser"
    export LD_LIBRARY_PATH="$DIR/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
    for o in "${JSC_OPTS[@]}"; do export "JSC_$o"; done
    WEBPROCS=("WebKitWebProcess" "WebKitGPUProcess" "WebKitNetworkProcess")
    if [ -r /proc/sys/kernel/perf_event_paranoid ]; then
        lvl=$(cat /proc/sys/kernel/perf_event_paranoid)
        [ "$lvl" -le 1 ] 2>/dev/null || echo "WARNING: perf_event_paranoid=$lvl (>1); samply may fail. Run: sudo sysctl kernel.perf_event_paranoid=1" >&2
    fi
fi

if [ ! -x "$MB" ]; then
    echo "MiniBrowser not found at: $MB" >&2
    echo "Set WEBKIT_BUILD to your build dir (Release on macOS, GTK/Release on Linux)." >&2
    exit 1
fi

stop_browser() {
    pkill -x MiniBrowser 2>/dev/null
    for pat in "${WEBPROCS[@]}"; do pkill -f "$pat" 2>/dev/null; done
}

echo "OS:          $OS"
echo "MiniBrowser: $MB"
echo "URL:         $URL"
echo "GC period:   ${PERIOD_MS}ms   duration: ${DURATION}s   rate: ${RATE}Hz"
echo "aux dir:     $AUX   output: $OUT"
echo

stop_browser
sleep 1

"$SAMPLY" record --save-only --presymbolicate -n -o "$OUT" --rate "$RATE" -- "$MB" "$URL" > /tmp/jsc-trace-samply.log 2>&1 &
SAMPLY_PID=$!

sleep "$DURATION"

echo "stopping browser, letting samply finalize ..."
stop_browser
for _ in $(seq 1 30); do kill -0 "$SAMPLY_PID" 2>/dev/null || break; sleep 1; done
if kill -0 "$SAMPLY_PID" 2>/dev/null; then
    kill -INT "$SAMPLY_PID" 2>/dev/null
    for _ in $(seq 1 15); do kill -0 "$SAMPLY_PID" 2>/dev/null || break; sleep 1; done
fi
stop_browser

echo
echo "=== aux files ==="
ls -la "$AUX"/marker-*.txt "$AUX"/jit-*.dump 2>/dev/null
echo "GC marker breakdown:"; cat "$AUX"/marker-*.txt 2>/dev/null | awk '{print $3, $4}' | sort | uniq -c | sort -rn | head
echo "=== output ==="; ls -la "$OUT" 2>/dev/null
echo "next: $(dirname "$0")/split-trace.py $OUT --drop-idle --summary"
