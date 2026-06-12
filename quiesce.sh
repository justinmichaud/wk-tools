#!/bin/zsh
# jsc-jetstream-compare quiescing helper.
# Cuts JetStream3 benchmarking noise via (1) machine/OS quiescing and (3) input/network determinism.
#
#   ./quiesce.sh on       quiesce the machine, then seed a pinned local JS3 copy and settle thermals
#   ./quiesce.sh off       undo: re-enable Spotlight indexing, stop caffeinate
#   ./quiesce.sh status    report quiescing-relevant state; change nothing
#
# `on` and `status` print `JS3_LOCAL_COPY=<path>` on the last line — capture it and pass
# `--local-copy "$JS3_LOCAL_COPY"` to run-benchmark so every round copies a fixed checkout instead of
# re-cloning JetStream3 from GitHub (removes per-run network + disk variance, and pins the commit).
#
# Spotlight on/off and stopping a Time Machine backup need sudo; run in a terminal where you can
# authenticate. Everything degrades to a warning if sudo is unavailable.

set -u

STATE_DIR=/tmp/js3-quiesce
CAF_PID_FILE=$STATE_DIR/caffeinate.pid
SPOTLIGHT_FLAG=$STATE_DIR/spotlight_disabled
LOCALCOPY=/tmp/js3-builds/jetstream3-localcopy        # pinned JS3 checkout for --local-copy
JS3_REPO=https://github.com/WebKit/JetStream.git      # matches the jetstream3.plan git_repository
JS3_BRANCH=JetStream3.0
SETTLE_SECONDS=${JS3_SETTLE_SECONDS:-30}
mkdir -p "$STATE_DIR"

note() { print -r -- "[quiesce] $*"; }
warn() { print -r -- "[quiesce][WARN] $*"; }

check_machine() {
  if pmset -g batt 2>/dev/null | grep -q "AC Power"; then
    note "power: AC ✓"
  else
    warn "power: ON BATTERY — plug in (DVFS/throttling is the #1 laptop noise source)"
  fi

  local lim
  lim=$(pmset -g therm 2>/dev/null | sed -n 's/.*CPU_Speed_Limit *= *\([0-9]*\).*/\1/p')
  if [ -n "$lim" ] && [ "$lim" -lt 100 ]; then
    warn "thermal: CPU_Speed_Limit=${lim}% — throttled; let the machine cool before timing"
  else
    note "thermal: no throttle ✓"
  fi

  local asleep
  asleep=$(python3 -c "import Quartz;print(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))" 2>/dev/null)
  if [ "$asleep" = "1" ]; then
    warn "display: ASLEEP — a browser run will stall; wake the display or use the headless jsc path"
  else
    note "display: awake ✓"
  fi

  note "top CPU consumers right now:"
  ps -Ao pcpu,comm -r 2>/dev/null | sed -n '2,6p' | while read -r l; do print -r -- "         $l"; done

  local p
  for p in mds_stores mdworker bird cloudd backupd Dropbox "Google Drive" OneDrive; do
    if pgrep -fil "$p" >/dev/null 2>&1; then
      warn "background: '$p' active — quit/pause it (indexing/cloud-sync/backup aliases into subtests)"
    fi
  done
}

seed_local_copy() {
  if [ -d "$LOCALCOPY/.git" ]; then
    note "local JS3 copy present: $LOCALCOPY ($(git -C "$LOCALCOPY" rev-parse --short HEAD 2>/dev/null)) — pinned; rm -rf to refresh"
  else
    note "cloning pinned JS3 ($JS3_BRANCH) → $LOCALCOPY (one-time; avoids per-run GitHub clone)"
    rm -rf "$LOCALCOPY"
    if git clone --depth 1 --branch "$JS3_BRANCH" "$JS3_REPO" "$LOCALCOPY" >/dev/null 2>&1; then
      note "cloned $(git -C "$LOCALCOPY" rev-parse --short HEAD)"
    else
      warn "clone failed — runs will fall back to a per-run network clone"
      return 1
    fi
  fi
}

disable_spotlight() {
  if sudo -n mdutil -a -i off >/dev/null 2>&1 || sudo mdutil -a -i off >/dev/null 2>&1; then
    note "Spotlight indexing: OFF (restored by './quiesce.sh off')"
    touch "$SPOTLIGHT_FLAG"
  else
    warn "could not disable Spotlight — run manually: sudo mdutil -a -i off"
  fi
}

start_caffeinate() {
  if [ -f "$CAF_PID_FILE" ] && kill -0 "$(cat "$CAF_PID_FILE")" 2>/dev/null; then
    note "caffeinate already running (pid $(cat "$CAF_PID_FILE"))"
  else
    caffeinate -dimsu &
    echo $! > "$CAF_PID_FILE"
    note "caffeinate started (pid $(cat "$CAF_PID_FILE")) — note: does NOT wake an asleep display"
  fi
}

case "${1:-status}" in
  on)
    note "=== quiescing for a benchmarking run ==="
    check_machine
    disable_spotlight
    sudo -n tmutil stopbackup >/dev/null 2>&1 && note "Time Machine: stopped in-flight backup"
    start_caffeinate
    seed_local_copy
    note "display refresh: if this is a ProMotion/variable-refresh panel, set a FIXED rate in"
    note "                 System Settings > Displays — rAF-driven runs inherit refresh jitter (manual step)"
    note "settling ${SETTLE_SECONDS}s for thermals..."
    sleep "$SETTLE_SECONDS"
    note "=== quiesced. run your interleaved loop, then './quiesce.sh off' when done ==="
    [ -d "$LOCALCOPY/.git" ] && print -r -- "JS3_LOCAL_COPY=$LOCALCOPY"
    ;;
  off)
    note "=== restoring ==="
    if [ -f "$SPOTLIGHT_FLAG" ]; then
      if sudo -n mdutil -a -i on >/dev/null 2>&1 || sudo mdutil -a -i on >/dev/null 2>&1; then
        note "Spotlight indexing: re-enabled"; rm -f "$SPOTLIGHT_FLAG"
      else
        warn "could not re-enable Spotlight — run manually: sudo mdutil -a -i on"
      fi
    fi
    if [ -f "$CAF_PID_FILE" ]; then
      kill "$(cat "$CAF_PID_FILE")" 2>/dev/null && note "caffeinate stopped"
      rm -f "$CAF_PID_FILE"
    fi
    note "=== restored ==="
    ;;
  status)
    check_machine
    if [ -d "$LOCALCOPY/.git" ]; then
      print -r -- "JS3_LOCAL_COPY=$LOCALCOPY"
    else
      note "no local JS3 copy seeded yet — './quiesce.sh on' will clone one"
    fi
    ;;
  *)
    print -r -- "usage: ./quiesce.sh on|off|status"
    exit 2
    ;;
esac
