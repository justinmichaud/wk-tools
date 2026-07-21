#!/bin/bash
# attach-gdb.sh <url> [gdb_bt_log] [console_log]
# Launch a benchmark under cog (sandbox DISABLED so the WPEWebProcess is a plain
# child) and capture the FATAL crash backtrace with gdb.
#
# Why gdb-attach and not a core file: WebKit makes the web process non-dumpable
# (RLIMIT_CORE=0 / PR_SET_DUMPABLE 0), so the kernel writes NO core even with
# core_pattern set. gdb-attach is the working method (matches the prior debugging).
#
# Key detail: WTF uses SIGUSR1 for GC stop-the-world thread suspension (and SIGUSR2
# for VMTraps). gdb stops on these by default, which would trap a benign signal
# instead of the crash. We pass them through silently and only trap SIGSEGV/SIGABRT/
# SIGBUS/SIGILL/SIGTRAP (the real faults).
set -u
URL="${1:?usage: attach-gdb.sh <url> [gdb_bt_log] [console_log]}"
GLOG="${2:-/tmp/cog_gdb_bt.log}"
CONSOLE="${3:-/tmp/cog_console.log}"

WK=/WebKit/WebKit
BUILD="$WK/WebKitBuild/WPE/Release"
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
export JSC_validateOptions=1
export WEBKIT_EXEC_PATH="$BUILD/bin"
export WEBKIT_INJECTED_BUNDLE_PATH="$BUILD/lib"
export LD_LIBRARY_PATH="$BUILD/lib"
export COG_MODULEDIR="$BUILD/Tools/cog-prefix/src/cog-build/platform"
COG="$BUILD/Tools/cog-prefix/src/cog-build/launcher/cog"

rm -f "$CONSOLE" "$GLOG"
setsid "$COG" -P wl --enable-write-console-messages-to-stdout=1 "$URL" > "$CONSOLE" 2>&1 &

PID=""
for i in $(seq 1 40); do PID=$(pgrep -n WPEWebProcess); [ -n "$PID" ] && break; sleep 1; done
[ -z "$PID" ] && { echo "WPEWebProcess never started; see $CONSOLE" | tee -a "$GLOG"; exit 1; }
echo "attaching to WPEWebProcess pid=$PID  url=$URL" | tee "$GLOG"

gdb -p "$PID" -batch \
  -ex "set pagination off" \
  -ex "handle SIGUSR1 nostop noprint pass" \
  -ex "handle SIGUSR2 nostop noprint pass" \
  -ex "handle SIGPIPE nostop noprint pass" \
  -ex "continue" \
  -ex "printf \"\n=== FATAL SIGNAL CAUGHT ===\n\"" \
  -ex "print/x \$_siginfo._sifields._sigfault.si_addr" \
  -ex "info registers pc sp lr r0 r1 r2 r3 r4 r5 r6 r7 r10" \
  -ex "printf \"\n--- disas around pc ---\n\"" \
  -ex "x/16i \$pc-28" \
  -ex "printf \"\n--- crashing thread backtrace ---\n\"" \
  -ex "bt 40" \
  -ex "printf \"\n--- all threads (short) ---\n\"" \
  -ex "thread apply all bt 6" \
  >> "$GLOG" 2>&1

echo "GDB DONE $(date -u)" >> "$GLOG"
pkill -f "cog -P wl" 2>/dev/null
exit 0
