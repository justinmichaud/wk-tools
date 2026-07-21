#!/bin/bash
# jsc-gdb.sh <jsfile> [JSC_opt=val ...]
# Run a JS file in the jsc shell under gdb and print a backtrace if it crashes.
# Useful for trying to get a fast standalone repro of a browser crash.
# Pass JSC options as environment, e.g.:
#   JSC_useConcurrentGC=0 jsc-gdb.sh /tmp/repro.js
#   jsc-gdb.sh /tmp/repro.js         # default options
# NOTE: some JSC crashes only reproduce under the browser's real memory pressure
# and will NOT reproduce in the jsc shell (which has the whole machine to itself).
set -u
JS="${1:?usage: jsc-gdb.sh <jsfile> [env JSC_*=..]}"
BUILD=/WebKit/WebKit/WebKitBuild/WPE/Release
export LD_LIBRARY_PATH="$BUILD/lib"
JSC="$BUILD/bin/jsc"
echo "### jsc $JS  (options below)"; env | grep '^JSC_' | sort
gdb -batch \
  -ex "run" \
  -ex "printf \"\n=== SIGNAL CAUGHT ===\n\"" \
  -ex "print/x \$_siginfo._sifields._sigfault.si_addr" \
  -ex "info registers pc sp lr r0 r1 r2 r3 r4 r5 r6 r7 r10" \
  -ex "x/16i \$pc-28" \
  -ex "bt 30" \
  --args "$JSC" "$JS"
