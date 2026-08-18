#!/bin/bash
# probe-tests.sh <test1> <test2> ...
# Run each named JetStream2.2 subtest ALONE (browserbench ?report=true&test=NAME) and
# report whether it crashes/OOMs or passes, plus peak RSS. Used to find which single
# subtest is responsible for a JetStream OOM/crash on the memory-constrained rpi3.
# Requires /tmp/run-cog.sh present and the fake seat running.
set -u
RESULTS=/tmp/probe_results.txt
: > "$RESULTS"
for NAME in "$@"; do
  pkill -9 -f "cog -P wl" 2>/dev/null; pkill -9 -f WPEWebProcess 2>/dev/null; sleep 3
  LOG="/tmp/probe_${NAME}.log"; rm -f "$LOG"
  base=$(dmesg 2>/dev/null | grep -c "sig=11")
  setsid /tmp/run-cog.sh "$LOG" "https://browserbench.org/JetStream2.2/?report=true&test=${NAME}" < /dev/null > /dev/null 2>&1 &
  peak=0; ran=0; idle=0; e=0; MAX=360; verdict="TIMEOUT"
  while [ $e -lt $MAX ]; do
    now=$(dmesg 2>/dev/null | grep -c "sig=11")
    if [ "$now" -gt "$base" ]; then verdict="CRASH_SIGSEGV"; break; fi
    if grep -qiE "renderer process crashed|RangeError: Out of memory|Cannot allocate" "$LOG" 2>/dev/null; then verdict="CRASH_OR_OOM"; break; fi
    rss=$(ps -o rss= -C WPEWebProcess 2>/dev/null | awk '{s+=$1} END{print int(s/1024)}')
    cpu=$(ps -o pcpu= -C WPEWebProcess 2>/dev/null | awk '{s+=$1} END{print int(s)}')
    [ "${rss:-0}" -gt "$peak" ] && peak=$rss
    [ "${cpu:-0}" -gt 30 ] && ran=1
    load=$(awk '{print int($1)}' /proc/loadavg)
    if [ "${cpu:-0}" -lt 12 ] && [ "${load:-9}" -lt 2 ]; then idle=$((idle+1)); else idle=0; fi
    if [ $ran -eq 1 ] && [ $idle -ge 8 ]; then verdict="PASSED"; break; fi
    sleep 5; e=$((e+5))
  done
  echo "$(printf '%-22s' "$NAME") $verdict  peakRSS=${peak}MB  ran=$ran  t=${e}s" | tee -a "$RESULTS"
done
pkill -9 -f "cog -P wl" 2>/dev/null
echo "=== DONE ===" | tee -a "$RESULTS"
