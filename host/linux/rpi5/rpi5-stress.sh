#!/usr/bin/env bash
# rpi5-stress.sh — RIGOROUS validation of the CPU overclock (arm_freq, currently 2.8GHz).
# Research finding: short stress tests can PASS while compute/NEON workloads FAIL
# on an under-volted OC. So we use --verify (detects silent miscalculations from
# instability) and cycle ALL cpu methods (incl. NEON) for 10 min.
# If this fails: add `over_voltage_delta=25000` (then 50000) to config.txt [pi5].
# Run AFTER reboot:  sudo bash ~/rpi5-tune/rpi5-stress.sh
set -u
command -v stress-ng >/dev/null || { echo "installing stress-ng..."; apt-get install -y stress-ng >/dev/null 2>&1; }

# vcgencmd (temp/clock/throttle) needs /dev/vcio, which is root-only. Without sudo
# it returns nothing, temp/throttle monitoring is blank, and the empty $t used to
# make the awk comparison below fail with a syntax error (harmless, but looks like
# an error). Warn loudly — stress-ng itself still runs and its exit code is valid.
[ "$(id -u)" -eq 0 ] || echo "WARNING: not running as root — vcgencmd sensors need sudo; temp/throttle will be blank. For full monitoring: sudo bash $0"

echo "Clock: $(vcgencmd measure_clock arm 2>/dev/null | cut -d= -f2 | awk '{printf "%.0f MHz",$1/1e6}')   throttled: $(vcgencmd get_throttled 2>/dev/null)"
echo "Running 10-min CPU torture (--verify, all methods incl. NEON) + monitoring..."
# Per-run private temp file. A hardcoded /tmp/stress.err collides across users:
# fs.protected_regular=2 then blocks even root from writing a file it doesn't own
# in sticky /tmp, which silently aborts the launch and fakes a "NOT STABLE" result.
ERR="$(mktemp "${TMPDIR:-/tmp}/rpi5-stress.XXXXXX.err")"
trap 'rm -f "$ERR"' EXIT
stress-ng --cpu 4 --cpu-method all --verify --timeout 600s --metrics-brief 2>"$ERR" &
SPID=$!
worst=0; bad=0
while kill -0 $SPID 2>/dev/null; do
  t=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9.]+'); thr=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
  [ -n "$t" ] && awk "BEGIN{exit !($t>$worst)}" && worst=$t   # guard: empty $t (no sudo) must not crash awk
  [ -n "$thr" ] && [ "$thr" != "0x0" ] && { bad=1; echo "  !! throttled=$thr at ${t}C"; }
  printf "  temp=%sC  throttled=%s  clock=%sMHz\n" "$t" "$thr" "$(vcgencmd measure_clock arm|cut -d= -f2|awk '{printf "%.0f",$1/1e6}')"
  sleep 20
done
wait $SPID; RC=$?
echo "-------------------------------------------------------------"
echo "stress-ng exit: $RC   (0 = no crashes AND no --verify mismatches)"
echo "worst temp: ${worst}C   (keep <80C for long-term reliability)"
echo "final throttled: $(vcgencmd get_throttled)   (0x0 = clean)"
grep -iE "fail|error|verif" "$ERR" >/dev/null 2>&1 && { echo "!! verify/errors in log:"; grep -iE "fail|error|verif" "$ERR"; }
if [ "$RC" = 0 ] && [ "$bad" = 0 ]; then
  echo "RESULT: overclock STABLE under torture + verify. ✅"
  echo "  (For total confidence, also run a real compute load, e.g. a kernel build or 'openssl speed'.)"
else
  echo "RESULT: NOT STABLE. Add to config.txt [pi5]:  over_voltage_delta=25000  (retry; then 50000). Do NOT use force_turbo."
fi