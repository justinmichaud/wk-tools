#!/bin/bash
# run-cog.sh <logfile> <url> [--debug] -- launches a benchmark URL in cog on the WPE build with console messages captured; --debug drops the bubblewrap sandbox so WPEWebProcess is a plain child of cog, which gdb can attach to.
set -u
LOG="${1:?usage: run-cog.sh <logfile> <url> [--debug]}"
URL="${2:?usage: run-cog.sh <logfile> <url> [--debug]}"
DEBUG="${3:-}"

WK=/WebKit/WebKit
BUILD="$WK/WebKitBuild/WPE/Release"
export XDG_RUNTIME_DIR=/run/user/1000   # weston's socket, if its values differ ask get-wayland-env (SKILL.md)
export WAYLAND_DISPLAY=wayland-1
export JSC_validateOptions=1
export WEBKIT_EXEC_PATH="$BUILD/bin"
export WEBKIT_INJECTED_BUNDLE_PATH="$BUILD/lib"
export LD_LIBRARY_PATH="$BUILD/lib"
export COG_MODULEDIR="$BUILD/Tools/cog-prefix/src/cog-build/platform"
[ "$DEBUG" = "--debug" ] && export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1

COG="$BUILD/Tools/cog-prefix/src/cog-build/launcher/cog"
exec "$COG" -P wl --enable-write-console-messages-to-stdout=1 "$URL" > "$LOG" 2>&1
