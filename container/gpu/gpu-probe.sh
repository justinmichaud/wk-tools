#!/bin/bash
# Build the EGL probe against the image's own libEGL, then run it.

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
