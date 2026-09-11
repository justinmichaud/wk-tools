#!/usr/bin/env bash
#
# Converges the webkit-container-sdk checkout at $1: fetch, reset onto the
# remote's current default branch, then re-apply wk's patches (sdk-patches/
# apply.sh) -- one operation, run identically by host/linux/sdk.sh,
# host/macos/vmtools.sh (over ssh, inside the podman VM) and `wk new`
# (targets/container.sh t_sdk_refresh). A reset without the patches leaves a
# wkdev-create that refuses wk's --network/--isolated/--additional-flags.
# `git remote set-head -a` follows a renamed default branch instead of
# assuming one. WK_SDK_PATCHER is a test seam for the patch step.

set -euo pipefail

SDK="${1:?usage: sdk-refresh.sh <checkout>}"

[ -d "$SDK/.git" ] || { echo "not an SDK checkout: $SDK" >&2; exit 1; }

cd "$SDK"

_refuse() {
    echo "could not reach the webkit-container-sdk checkout's remote ($SDK) --
'wk new' needs the network for the image pull that follows anyway, so there
is nothing useful to fall back to. Retry once the network is back." >&2
    exit 1
}

# Fetch first: `set-head -a` resolves against refs/remotes/origin/* it already
# has, and errors on a branch rename until a fetch has created the new ref.
git fetch --quiet --prune origin || _refuse
git remote set-head origin -a >/dev/null 2>&1 || _refuse

git reset --hard --quiet "$(git symbolic-ref --short refs/remotes/origin/HEAD)"
git clean -qfd

bash "${WK_SDK_PATCHER:-$(dirname "$0")/sdk-patches/apply.sh}" "$SDK"
