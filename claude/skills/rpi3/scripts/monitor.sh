#!/bin/bash
# monitor.sh <logfile> <maxsecs>
# Watch a running benchmark (WPEWebProcess) and emit a verdict:
#   CRASH_OR_ASSERT  - crash/assert/OOM string seen in the console log
#   WEBPROC_GONE     - the web process disappeared after having run
#   IDLE_DONE        - ran, then went truly idle (benchmark finished / results screen)
#   TIMEOUT          - hit the time cap
# "Idle" requires BOTH low CPU AND low load-average, so heavy swap I/O
# (low %CPU but high loadavg, process blocked in D-state) is NOT mistaken for done.
LOG="$1"; MAX="${2:-2700}"
idle=0; ran=0; elapsed=0; interval=10; need_idle=12   # 120s of true idle
verdict="UNKNOWN"
while [ $elapsed -lt $MAX ]; do
  if grep -qiE "assertion failed|Bail out|RELEASE_ASSERT|SIGSEGV|SIGABRT|SIGTRAP|CRASH|received signal|Segmentation fault|terminate called|out of memory|Cannot allocate|renderer process crashed" "$LOG" 2>/dev/null; then
    verdict="CRASH_OR_ASSERT"; break
  fi
  cpu=$(ps -o pcpu= -C WPEWebProcess 2>/dev/null | awk '{s+=$1} END{print int(s+0)}')
  alive=$(pgrep -c WPEWebProcess 2>/dev/null || echo 0)
  load1=$(awk '{print $1}' /proc/loadavg); loadi=${load1%.*}
  if [ "$alive" -eq 0 ]; then
    if [ $ran -eq 1 ]; then verdict="WEBPROC_GONE"; break; fi
  else
    [ "${cpu:-0}" -gt 30 ] && ran=1
    if [ "${cpu:-0}" -lt 12 ] && [ "${loadi:-9}" -lt 2 ]; then idle=$((idle+1)); else idle=0; fi
  fi
  if [ $ran -eq 1 ] && [ $idle -ge $need_idle ]; then verdict="IDLE_DONE"; break; fi
  sleep $interval; elapsed=$((elapsed+interval))
done
[ "$verdict" = "UNKNOWN" ] && verdict="TIMEOUT"
echo "VERDICT=$verdict elapsed=${elapsed}s ran=$ran idle_streak=$idle"
echo "=== webproc ==="; ps -o pid,pcpu,pmem,rss,etimes,comm -C WPEWebProcess 2>/dev/null || echo none
echo "=== mem/swap ==="; free -m | grep -E "Mem|Swap"
echo "=== loadavg ==="; cat /proc/loadavg
echo "=== log tail ==="; tail -15 "$LOG"
