#!/usr/bin/env bash
# Validate the CPU overclock, after a reboot. A short stress test passes while
# compute/NEON workloads fail on an under-volted OC, hence --verify.
set -euo pipefail
command -v stress-ng >/dev/null || {
  echo "installing stress-ng..."
  apt-get install -y stress-ng >/dev/null 2>&1 || { echo "ERROR: could not install stress-ng" >&2; exit 1; }
}

# vcgencmd needs /dev/vcio: without sudo an empty $t breaks the awk below.
[ "$(id -u)" -eq 0 ] || echo "WARNING: not running as root — vcgencmd sensors need sudo; temp/throttle will be blank. For full monitoring: sudo bash $0"

echo "Clock: $(vcgencmd measure_clock arm 2>/dev/null | cut -d= -f2 | awk '{printf "%.0f MHz",$1/1e6}')   throttled: $(vcgencmd get_throttled 2>/dev/null)"
echo "Running 10-min CPU torture (--verify, all methods incl. NEON) + monitoring..."
# fs.protected_regular=2 blocks even root from writing a file it does not own in
# sticky /tmp, so a hardcoded path silently aborts the launch.
ERR="$(mktemp "${TMPDIR:-/tmp}/rpi5-stress.XXXXXX.err")"
trap 'rm -f "$ERR"' EXIT
stress-ng --cpu 4 --cpu-method all --verify --timeout 600s --metrics-brief 2>"$ERR" &
SPID=$!
worst=0; bad=0
while kill -0 $SPID 2>/dev/null; do
  t=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9.]+') || true
  thr=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2) || true
  [ -n "$t" ] && awk "BEGIN{exit !($t>$worst)}" && worst=$t   # guard: empty $t (no sudo) must not crash awk
  [ -n "$thr" ] && [ "$thr" != "0x0" ] && { bad=1; echo "  !! throttled=$thr at ${t}C"; }
  printf "  temp=%sC  throttled=%s  clock=%sMHz\n" "$t" "$thr" "$(vcgencmd measure_clock arm|cut -d= -f2|awk '{printf "%.0f",$1/1e6}')"
  sleep 20
done
if wait "$SPID"; then RC=0; else RC=$?; fi
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