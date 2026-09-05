#!/bin/bash
# jsc-gdb.sh <jsfile> [JSC_opt=val ...] -- runs the file in the jsc shell under gdb, options passed as `JSC_*` environment, and prints a backtrace on a crash. A crash that needs the browser's real memory pressure will not reproduce here, the shell having the machine to itself.
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
