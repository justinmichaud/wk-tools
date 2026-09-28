#!/usr/bin/env bash
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

# Fetch before `set-head -a`: it resolves against refs/remotes/origin/* already here and errors on a renamed default branch until a fetch has made the new ref; the patches go back on because a stock wkdev-create refuses wk's --network/--isolated/--additional-flags (WK_SDK_PATCHER is the test seam).
git fetch --quiet --prune origin || _refuse
git remote set-head origin -a >/dev/null 2>&1 || _refuse

_tree() { { git rev-parse HEAD; git diff HEAD; git status --porcelain --untracked-files=all; } | cksum; }
_before=$(_tree)
git reset --hard --quiet "$(git symbolic-ref --short refs/remotes/origin/HEAD)"
git clean -qfd

_rc=0
_said=$(bash "${WK_SDK_PATCHER:-$(dirname "$0")/sdk-patches/apply.sh}" "$SDK" 2>&1) || _rc=$?
if [ "$_rc" -ne 0 ] || [ "$(_tree)" != "$_before" ]; then
    [ -z "$_said" ] || printf '%s\n' "$_said" >&2
fi
exit "$_rc"
