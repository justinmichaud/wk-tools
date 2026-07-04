#!/usr/bin/env bash
# rpi5-stress.sh — RIGOROUS validation of the CPU overclock (arm_freq, currently 2.8GHz).
# Research finding: short stress tests can PASS while compute/NEON workloads FAIL
# on an under-volted OC. So we use --verify (detects silent miscalculations from
# instability) and cycle ALL cpu methods (incl. NEON) for 10 min.
# If this fails: add `over_voltage_delta=25000` (then 50000) to config.txt [pi5].
# Run AFTER reboot:  sudo bash ~/rpi5-tune/rpi5-stress.sh
set -u
command -v stress-ng >/dev/null || { echo "installing stress-ng..."; apt-get install -y stress-ng >/dev/null 2>&1; }

echo "Clock: $(vcgencmd measure_clock arm | cut -d= -f2 | awk '{printf "%.0f MHz",$1/1e6}')   throttled: $(vcgencmd get_throttled)"
echo "Running 10-min CPU torture (--verify, all methods incl. NEON) + monitoring..."
stress-ng --cpu 4 --cpu-method all --verify --timeout 600s --metrics-brief 2>/tmp/stress.err &
SPID=$!
worst=0; bad=0
while kill -0 $SPID 2>/dev/null; do
  t=$(vcgencmd measure_temp | grep -oE '[0-9.]+'); thr=$(vcgencmd get_throttled | cut -d= -f2)
  awk "BEGIN{exit !($t>$worst)}" && worst=$t
  [ "$thr" != "0x0" ] && { bad=1; echo "  !! throttled=$thr at ${t}C"; }
  printf "  temp=%sC  throttled=%s  clock=%sMHz\n" "$t" "$thr" "$(vcgencmd measure_clock arm|cut -d= -f2|awk '{printf "%.0f",$1/1e6}')"
  sleep 20
done
wait $SPID; RC=$?
echo "-------------------------------------------------------------"
echo "stress-ng exit: $RC   (0 = no crashes AND no --verify mismatches)"
echo "worst temp: ${worst}C   (keep <80C for long-term reliability)"
echo "final throttled: $(vcgencmd get_throttled)   (0x0 = clean)"
grep -iE "fail|error|verif" /tmp/stress.err >/dev/null 2>&1 && { echo "!! verify/errors in log:"; grep -iE "fail|error|verif" /tmp/stress.err; }
if [ "$RC" = 0 ] && [ "$bad" = 0 ]; then
  echo "RESULT: overclock STABLE under torture + verify. ✅"
  echo "  (For total confidence, also run a real compute load, e.g. a kernel build or 'openssl speed'.)"
else
  echo "RESULT: NOT STABLE. Add to config.txt [pi5]:  over_voltage_delta=25000  (retry; then 50000). Do NOT use force_turbo."
fi