#!/bin/zsh
# Interleaved full-suite JetStream3 loop: baseline vs patched (WebKit MiniBrowser).
# Pairs with quiesce.sh (run `./quiesce.sh on` first — it keeps MiniBrowser frontmost
# and pauses background noise) and js3-ci.py (per-subtest b/a + 95% CI on the JSONs).
#
#   BASE_SHA=<sha> WEBKIT_ROOT=<path> ./js3-run-loop.sh [ROUNDS] [START]
#
# baseline build is expected at /tmp/js3-builds/$BASE_SHA/Release (see the jsc-jetstream-compare
# skill, Step 2); patched build is $WEBKIT_ROOT/WebKitBuild/Release. Writes one JSON per build
# per round to /tmp/js3-runs/. Drop the --subtests line in run_one for the full suite.
set -u

WEBKIT_ROOT=${WEBKIT_ROOT:-/Users/justinmichaud/Development/DebugVersion/OpenSource}
cd "$WEBKIT_ROOT"
BASE_SHA=${BASE_SHA:-$(git rev-parse HEAD)}
CACHE=/tmp/js3-builds/$BASE_SHA/Release
PATCHED=$WEBKIT_ROOT/WebKitBuild/Release
LOCAL_COPY=${JS3_LOCAL_COPY:-/tmp/js3-builds/jetstream3-localcopy}
J3=/tmp/js3-runs; mkdir -p "$J3"
ROUNDS=${1:-6}
START=${2:-1}

[ -d "$CACHE" ]   || { echo "[loop] baseline build missing: $CACHE" >&2; exit 1; }
[ -d "$PATCHED" ] || { echo "[loop] patched build missing: $PATCHED" >&2; exit 1; }
echo "[loop] baseline=$CACHE"
echo "[loop] patched =$PATCHED"

# MiniBrowser is kept frontmost by the quiesce.sh raiser (./quiesce.sh on); nothing to do here.

run_one(){ # $1=build-dir  $2=out.json  $3=logtag
  Tools/Scripts/run-benchmark --plan jetstream3 --browser minibrowser \
    --build-directory "$1" --output-file "$2" --count 1 \
    --local-copy "$LOCAL_COPY" > "$J3/$3.log" 2>&1
}

for i in $(seq $START $((START+ROUNDS-1))); do
  k=$(printf %02d $i)
  echo "[loop] === round $k start ==="
  if [ $((i%2)) -eq 0 ]; then
    run_one "$CACHE"    "$J3/base_$k.json"    "base_$k"
    run_one "$PATCHED"  "$J3/patched_$k.json" "patched_$k"
  else
    run_one "$PATCHED"  "$J3/patched_$k.json" "patched_$k"
    run_one "$CACHE"    "$J3/base_$k.json"    "base_$k"
  fi
  echo "[loop] === round $k done: base=$(test -s $J3/base_$k.json && echo ok || echo MISSING) patched=$(test -s $J3/patched_$k.json && echo ok || echo MISSING) ==="
done
echo "[loop] ALL DONE rounds $START..$((START+ROUNDS-1))"
