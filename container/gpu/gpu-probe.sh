#!/bin/bash
#
# Build the EGL probe once per workspace and run it. Built rather than shipped:
# the toolchain is right there, the binary is 20 KB, and a prebuilt one would
# have to match the image's libEGL rather than the host's driver.

set -euo pipefail
SRC=/opt/wk-tools/container/gpu/gpu-probe.c
BIN=${WK_GPU_PROBE_BIN:-/tmp/.wk-gpu-probe}

if [ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]; then
    cc -O1 -o "$BIN" "$SRC" -lEGL -lGLESv2 -lgbm 2>/tmp/.wk-gpu-probe.log || {
        echo "gpu-probe: build failed" >&2
        sed 's/^/  /' /tmp/.wk-gpu-probe.log >&2
        exit 3
    }
fi

exec "$BIN"
