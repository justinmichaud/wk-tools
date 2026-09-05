# Read-only key=value readings; runs in the guest under macOS bash 3.2, sources nothing.

set -euo pipefail

printf 'mem_total_mb=%s\n' "$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1048576 ))"

printf 'mem_free_pct=%s\n' "$(memory_pressure 2>/dev/null \
    | sed -n 's/^System-wide memory free percentage: *\([0-9]*\).*/\1/p' | tail -1)"

printf 'swapusage=%s\n' "$(sysctl -n vm.swapusage 2>/dev/null)"

ps -Ao rss=,comm= 2>/dev/null | sed 's/^ *//; s/^/proc=/'
