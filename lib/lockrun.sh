#!/usr/bin/env bash
# <resource> [-w seconds] -- cmd...: the command under the lock (lib/wk/lock.py), and its status.
set -euo pipefail
WK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$WK_ROOT/lib" exec python3 -m wk.lock run "$@"
