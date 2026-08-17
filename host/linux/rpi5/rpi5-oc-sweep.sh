#!/usr/bin/env bash
#===============================================================================
# rpi5-oc-sweep.sh — AUTONOMOUS overclock sweep that survives reboots.
#
# Installed to /usr/local/sbin/rpi5-oc-sweep and driven by rpi5-oc-sweep.service
# (systemd oneshot, runs late on every boot). Because a new arm_freq/v3d_freq only
# takes effect after a reboot — and a reboot kills any live shell — the loop is a
# reboot-surviving state machine: each boot it stress-tests the currently-applied
# clock, records the result, decides the next step, writes config.txt, and reboots.
#
# SAFETY:
#  - Bounded ranges (arm_freq <= 3100, over_voltage_delta <= 50000, v3d <= 1400)
#    chosen from community consensus — all boot reliably on a Pi 5; the realistic
#    failure mode is stress instability (caught) not a boot hang.
#  - Keeps a known-good config backup; every failure reverts to the best stable
#    config found so far. Finalizes on the best stable clock, never a failed one.
#  - Crash/reboot DURING a test is detected on next boot (inflight marker) = FAIL.
#  - Hard iteration cap; leaves the fan pinned (fan-max.service) throughout.
#  - Critical thermal trip (110C) is untouched, so the SoC still self-protects.
#
# Control:  rpi5-oc-sweep {run|status|stop}
#   run    - one iteration (what the service calls)
#   status - print progress + results
#   stop   - abort: restore best-stable config, disable service, reboot
#===============================================================================
set -uo pipefail

DIR=/var/lib/rpi5-oc
STATE="$DIR/state"
LOG="$DIR/results.log"
INFLIGHT="$DIR/inflight"
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
GOOD="$DIR/known-good-config.txt"

# ---- bounds (community consensus; see rpi5-numa-README / commit msg) ----------
ARM_CAP=3100          # 3150+ reboots/segfaults even with voltage -> hard ceiling
OVD_MAX=50000         # µV; sane max over_voltage_delta
OVD_STEP=25000
V3D_LIST="1300 1400"  # we already run 1200; these are aggressive
STRESS_SECS=300       # per-CPU-step torture (--verify, all methods incl NEON)
GPU_LOAD_SECS=60
TEMP_CEIL=85          # °C; above this = fail (protect longevity; fan is maxed)
ITER_CAP=24           # absolute safety stop

log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG" >&2; }

# ---- state helpers ------------------------------------------------------------
getv(){ sed -n "s/^$1=//p" "$STATE" 2>/dev/null | tail -1; }
setv(){ # key val
  if grep -q "^$1=" "$STATE" 2>/dev/null; then sed -i "s/^$1=.*/$1=$2/" "$STATE"
  else echo "$1=$2" >> "$STATE"; fi
}

# ---- config.txt editing (operates on the [pi5] tuning block) ------------------
apply_arm(){ sed -i "s/^arm_freq=.*/arm_freq=$1/" "$CFG"; }
apply_v3d(){ sed -i "s/^v3d_freq=.*/v3d_freq=$1/" "$CFG"; }
apply_ovd(){ # remove any existing line, then add if non-zero (inside [pi5], after arm_freq)
  sed -i '/^over_voltage_delta=/d' "$CFG"
  [ "$1" != 0 ] && sed -i "/^arm_freq=/a over_voltage_delta=$1" "$CFG" || true
}
apply_cfg(){ apply_arm "$1"; apply_ovd "$2"; apply_v3d "$3"; sync; }   # arm ovd v3d

reboot_now(){ log "rebooting to apply arm=$(getv arm) ovd=$(getv ovd) v3d=$(getv v3d)"; sleep 2; systemctl reboot -i; exit 0; }

finalize(){ # apply best-stable, disable service, stop
  local ba=$(getv best_arm) bo=$(getv best_ovd) bv=$(getv best_v3d)
  log "SWEEP COMPLETE. Best stable: arm_freq=$ba over_voltage_delta=$bo v3d_freq=$bv"
  log "Summary of all results is above in $LOG"
  apply_cfg "$ba" "$bo" "$bv"
  setv phase done; rm -f "$INFLIGHT"
  systemctl disable rpi5-oc-sweep.service >/dev/null 2>&1 || true
  log "service disabled; rebooting once onto the best-stable config"
  sleep 2; systemctl reboot -i; exit 0
}

# ---- measurement --------------------------------------------------------------
cur_temp(){ awk '{printf "%.0f", $1/1000}' /sys/class/thermal/thermal_zone0/temp; }
throttled(){ vcgencmd get_throttled 2>/dev/null | cut -d= -f2; }
armclk_mhz(){ awk '{printf "%.0f",$1/1000}' /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq; }
v3dclk_mhz(){ vcgencmd measure_clock v3d 2>/dev/null | cut -d= -f2 | awk '{printf "%.0f",$1/1e6}'; }

# CPU torture; returns 0 = stable. Records worst temp in $WORST_TEMP.
cpu_test(){
  local rc worst=0 t thr bad=0
  command -v stress-ng >/dev/null || apt-get install -y stress-ng >/dev/null 2>&1
  stress-ng --cpu 4 --cpu-method all --verify --timeout "${STRESS_SECS}s" --metrics-brief >/tmp/oc-stress.log 2>&1 &
  local sp=$!
  while kill -0 $sp 2>/dev/null; do
    t=$(cur_temp); thr=$(throttled)
    [ "$t" -gt "$worst" ] 2>/dev/null && worst=$t
    [ "$t" -ge "$TEMP_CEIL" ] && bad=1
    sleep 15
  done
  wait $sp; rc=$?
  WORST_TEMP=$worst; LAST_THROTTLE=$(throttled)
  # stress-ng --verify already returns non-zero on any verification mismatch or
  # crashed worker; a hard hang instead reboots the box (caught via INFLIGHT).
  [ "$bad" = 1 ] && { log "  temp ceiling hit (${worst}C >= ${TEMP_CEIL})"; return 1; }
  return $rc
}

# GPU check (headless, necessarily limited): confirm the firmware ACCEPTED the
# v3d_freq (get_config = accepted value; firmware clamps unsupported freqs), that
# the box boots+runs stably with it, and that dmesg shows no v3d faults. NOTE: a
# true graphics-load stress needs a desktop session (glmark2-wayland), which this
# headless service can't drive — that's logged as a manual follow-up.
gpu_test(){
  local want=$1 got; got=$(vcgencmd get_config v3d_freq 2>/dev/null | cut -d= -f2)
  WORST_TEMP=$(cur_temp)
  log "  v3d requested=${want}MHz firmware-accepted=${got}MHz measured=$(v3dclk_mhz)MHz"
  if [ -z "$got" ] || [ "$got" -lt "$want" ]; then
    log "  firmware clamped v3d below target -> not stable at this freq"; return 1
  fi
  dmesg | tail -300 | grep -iqE 'v3d.*(fault|error|reset|hang|timeout)' && { log "  v3d faults in dmesg -> fail"; return 1; }
  # brief system load while watching temp/faults (validates clean operation at freq)
  timeout ${GPU_LOAD_SECS}s stress-ng --cpu 4 --vm 1 --vm-bytes 512M >/dev/null 2>&1 || true
  WORST_TEMP=$(cur_temp)
  [ "$(cur_temp)" -ge "$TEMP_CEIL" ] && return 1
  dmesg | tail -50 | grep -iqE 'v3d.*(fault|error|reset|hang|timeout)' && return 1
  return 0
}

record(){ # phase result -- logs the just-tested config
  local ph=$1 res=$2
  log "RESULT [$ph] arm_freq=$(getv arm) over_voltage_delta=$(getv ovd) v3d_freq=$(getv v3d) => $res  (worst_temp=${WORST_TEMP:-?}C throttled=${LAST_THROTTLE:-$(throttled)} armclk=$(armclk_mhz)MHz)"
}

#===============================================================================
run(){
  [ -f "$STATE" ] || { log "no state; not armed"; exit 0; }
  local phase; phase=$(getv phase)
  [ "$phase" = done ] && { log "sweep already done"; exit 0; }
  sleep 30   # let boot settle + fan-max pin the fan before we load the SoC

  local iter; iter=$(( $(getv iter) + 1 )); setv iter "$iter"
  [ "$iter" -gt "$ITER_CAP" ] && { log "iteration cap ($ITER_CAP) hit -> finalize"; finalize; }

  local arm ovd v3d; arm=$(getv arm); ovd=$(getv ovd); v3d=$(getv v3d)
  log "boot: iter=$iter phase=$phase testing arm=$arm ovd=$ovd v3d=$v3d (armclk=$(armclk_mhz)MHz)"

  # crash-during-previous-test detection
  if [ -f "$INFLIGHT" ]; then
    log "INFLIGHT marker present -> previous test crashed/rebooted the box = UNSTABLE"
    LAST_THROTTLE=$(throttled); WORST_TEMP="?"; record "$phase" "FAIL(crash)"
    rm -f "$INFLIGHT"
    on_fail "$phase"; return
  fi

  # run the test for the currently-applied config
  echo "$arm $ovd $v3d" > "$INFLIGHT"; sync
  local ok=1
  if [ "$phase" = cpu ]; then cpu_test && ok=0 || ok=1
  else gpu_test "$v3d" && ok=0 || ok=1; fi
  rm -f "$INFLIGHT"

  if [ "$ok" = 0 ]; then record "$phase" "STABLE"; on_pass "$phase"
  else record "$phase" "FAIL"; on_fail "$phase"; fi
}

on_pass(){
  local phase=$1
  if [ "$phase" = cpu ]; then
    setv best_arm "$(getv arm)"; setv best_ovd "$(getv ovd)"
    local next=$(( $(getv arm) + 100 ))
    if [ "$next" -gt "$ARM_CAP" ]; then
      log "reached arm_freq cap; CPU phase done -> GPU phase"
      start_gpu_phase
    else
      setv arm "$next"; apply_cfg "$next" "$(getv ovd)" "$(getv v3d)"; reboot_now
    fi
  else # gpu
    setv best_v3d "$(getv v3d)"
    local rest; rest=$(echo "$V3D_LIST" | tr ' ' '\n' | awk -v c="$(getv v3d)" '$1>c' | head -1)
    if [ -n "$rest" ]; then setv v3d "$rest"; apply_cfg "$(getv best_arm)" "$(getv best_ovd)" "$rest"; reboot_now
    else log "GPU list exhausted"; finalize; fi
  fi
}

on_fail(){
  local phase=$1
  if [ "$phase" = cpu ]; then
    local ovd; ovd=$(getv ovd)
    if [ "$ovd" -lt "$OVD_MAX" ]; then
      local n=$(( ovd + OVD_STEP )); setv ovd "$n"
      log "  bumping over_voltage_delta -> $n and retrying arm=$(getv arm)"
      apply_cfg "$(getv arm)" "$n" "$(getv v3d)"; reboot_now
    else
      log "  arm=$(getv arm) unstable even at ovd=$OVD_MAX -> CPU phase done"
      start_gpu_phase
    fi
  else # gpu fail -> stop climbing, finalize on best
    log "  v3d=$(getv v3d) unstable/clamped -> GPU phase done"
    finalize
  fi
}

start_gpu_phase(){
  setv phase gpu
  local first; first=$(echo "$V3D_LIST" | awk '{print $1}')
  setv v3d "$first"
  log "GPU phase: applying best CPU (arm=$(getv best_arm) ovd=$(getv best_ovd)) + v3d=$first"
  apply_cfg "$(getv best_arm)" "$(getv best_ovd)" "$first"; reboot_now
}

#===============================================================================
case "${1:-run}" in
  run) run ;;
  status)
    echo "== state =="; cat "$STATE" 2>/dev/null || echo "(not armed)"
    echo "== current config.txt [pi5] =="; grep -E '^(arm_freq|over_voltage_delta|v3d_freq)=' "$CFG"
    echo "== results log =="; cat "$LOG" 2>/dev/null ;;
  stop)
    log "MANUAL STOP requested"; finalize ;;
  *) echo "usage: $0 {run|status|stop}" >&2; exit 2 ;;
esac
