#!/bin/bash
# probe-sets.sh "a,b" "a,b,c" ... -- runs each comma-separated set of JetStream2.2 subtests together and reports crash/OOM against pass with peak RSS, bisecting a cumulative OOM down to a minimal multi-test repro. Needs /tmp/run-cog.sh and the fake seat.
set -u
RESULTS=/tmp/probe_sets_results.txt
: > "$RESULTS"
for SET in "$@"; do
  pkill -9 -f "cog -P wl" 2>/dev/null; pkill -9 -f WPEWebProcess 2>/dev/null; sleep 3
  URL="https://browserbench.org/JetStream2.2/?report=true"
  IFS=',' read -ra TESTS <<< "$SET"
  for t in "${TESTS[@]}"; do URL="${URL}&test=${t}"; done
  LOG="/tmp/set_$(echo "$SET" | tr ',' '_').log"; rm -f "$LOG"
  base=$(dmesg 2>/dev/null | grep -c "sig=11")
  setsid /tmp/run-cog.sh "$LOG" "$URL" < /dev/null > /dev/null 2>&1 &
  peak=0; ran=0; idle=0; e=0; MAX=420; verdict="TIMEOUT"
  while [ $e -lt $MAX ]; do
    now=$(dmesg 2>/dev/null | grep -c "sig=11")
    if [ "$now" -gt "$base" ]; then verdict="CRASH_SIGSEGV"; break; fi
    if grep -qiE "renderer process crashed|RangeError: Out of memory|Cannot allocate" "$LOG" 2>/dev/null; then verdict="CRASH_OOM"; break; fi
    rss=$(ps -o rss= -C WPEWebProcess 2>/dev/null | awk '{s+=$1} END{print int(s/1024)}')
    cpu=$(ps -o pcpu= -C WPEWebProcess 2>/dev/null | awk '{s+=$1} END{print int(s)}')
    [ "${rss:-0}" -gt "$peak" ] && peak=$rss
    [ "${cpu:-0}" -gt 30 ] && ran=1
    load=$(awk '{print int($1)}' /proc/loadavg)
    if [ "${cpu:-0}" -lt 12 ] && [ "${load:-9}" -lt 2 ]; then idle=$((idle+1)); else idle=0; fi
    if [ $ran -eq 1 ] && [ $idle -ge 8 ]; then verdict="PASSED"; break; fi
    sleep 5; e=$((e+5))
  done
  echo "$(printf '%-45s' "$SET") $verdict  peakRSS=${peak}MB  t=${e}s" | tee -a "$RESULTS"
done
pkill -9 -f "cog -P wl" 2>/dev/null
echo "=== DONE ===" | tee -a "$RESULTS"
