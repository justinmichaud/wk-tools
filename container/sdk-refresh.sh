#!/usr/bin/env bash
# The one clone-and-pin step: <checkout> is the fork <repo> at commit <pin>.
set -euo pipefail

SDK="${1:?usage: sdk-refresh.sh <checkout> <repo> <pin>}"
REPO="${2:?usage: sdk-refresh.sh <checkout> <repo> <pin>}"
PIN="${3:?usage: sdk-refresh.sh <checkout> <repo> <pin>}"

_unreachable() {
    echo "could not reach $REPO for the webkit-container-sdk checkout ($SDK) --
'wk new' needs the network for the image pull that follows anyway. Retry once the network is back." >&2
    exit 1
}

if [ ! -d "$SDK/.git" ] && [ -z "$(ls -A "$SDK" 2>/dev/null)" ]; then
    mkdir -p "$(dirname "$SDK")"
    git clone -q "$REPO" "$SDK" || _unreachable
fi
[ -d "$SDK/.git" ] || { echo "not an SDK checkout: $SDK" >&2; exit 1; }

cd "$SDK"
_origin=$(git remote get-url origin 2>/dev/null || true)
if [ "$_origin" != "$REPO" ]; then
    if [ -n "$_origin" ]; then git remote set-url origin "$REPO"; else git remote add origin "$REPO"; fi
    echo "origin moved to $REPO"
fi

if [ "$(git rev-parse HEAD)" = "$PIN" ] && [ -z "$(git status --porcelain --untracked-files=all)" ]; then
    exit 0
fi
git cat-file -e "$PIN^{commit}" 2>/dev/null || git fetch --quiet origin || _unreachable
git cat-file -e "$PIN^{commit}" 2>/dev/null || { echo "$PIN is not a commit of $REPO: SDK_PIN in lib/wk/project.json names a commit the fork does not have." >&2; exit 1; }
git checkout --quiet --force --detach "$PIN"
git clean -qfd
