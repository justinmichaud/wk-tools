#!/bin/bash
# run-cog.sh <logfile> <url> [--debug]
# Launch a benchmark URL in cog on the WPE build, with console messages captured.
#   --debug : disable the bubblewrap sandbox (WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1)
#             so the WPEWebProcess is a plain child of cog (easier to attach gdb).
# Env (weston wayland socket) is set here; adjust XDG_RUNTIME_DIR/WAYLAND_DISPLAY if
# weston's values differ (see get-wayland-env in SKILL.md).
set -u
LOG="${1:?usage: run-cog.sh <logfile> <url> [--debug]}"
URL="${2:?usage: run-cog.sh <logfile> <url> [--debug]}"
DEBUG="${3:-}"

WK=/WebKit/WebKit
BUILD="$WK/WebKitBuild/WPE/Release"
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export JSC_validateOptions=1
export WEBKIT_EXEC_PATH="$BUILD/bin"
export WEBKIT_INJECTED_BUNDLE_PATH="$BUILD/lib"
export LD_LIBRARY_PATH="$BUILD/lib"
export COG_MODULEDIR="$BUILD/Tools/cog-prefix/src/cog-build/platform"
[ "$DEBUG" = "--debug" ] && export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1

COG="$BUILD/Tools/cog-prefix/src/cog-build/launcher/cog"
exec "$COG" -P wl --enable-write-console-messages-to-stdout=1 "$URL" > "$LOG" 2>&1
