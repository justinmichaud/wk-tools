#!/usr/bin/env bash
#
# The in-workspace half of `wk sysimage webkit`: build one WebKit commit into
# a *slot* beside a finished buildroot image, so a board running that image
# carries several WebKits and an A/B alternates between them without a
# reflash (`wk pi bench --ab`). Driven by image/buildroot.sh
# (buildroot_webkit) via t_spawn from /opt/wk-tools, the same host/worker
# split as buildroot-build.sh. Runs detached: nothing here may fail silently.
#
# Everything is buildroot's own developer workflow, nothing of it re-done
# here (buildroot manual, "Using Buildroot during development"):
#
#   local.mk        WPEWEBKIT_OVERRIDE_SRCDIR points the wpewebkit package at
#                   the workspace checkout, moved to the commit; buildroot
#                   rsyncs the source into its build directory and configures,
#                   builds and installs it exactly as it did the image's own
#   make wpewebkit-rebuild
#                   the package, again, from that source -- incremental
#   make target-finalize
#                   what buildroot does to the root filesystem before an
#                   image: strip, drop development files
#   packages-file-list.txt
#                   buildroot's own record of which files the package
#                   installed (step_pkg_size); the slot is those files
#
# What that leaves behind: output/target holds the slot's WebKit until the
# next image build, which drops local.mk and rebuilds the package from the
# pinned tarball first (image/buildroot-build.sh), so an image never ships
# the last slot built. The identifier a slot is told apart by is the
# build-id its linker wrote (BR2_TARGET_LDFLAGS, set by the image build).

set -euo pipefail

SRC=/src/WebKit
MIRROR=/mirror/WebKit.git
NAME=""; COMMIT=""; SLOT=""; JOBS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --name)   NAME="${2:-}"; shift 2 ;;
        --commit) COMMIT="${2:-}"; shift 2 ;;
        --slot)   SLOT="${2:-}"; shift 2 ;;
        --jobs)   JOBS="${2:-}"; shift 2 ;;
        *) echo "buildroot-webkit.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

say()  { printf 'wk-buildroot-webkit: %s\n' "$*"; }
fail() { printf 'wk-buildroot-webkit: error: %s\n' "$*" >&2; exit 1; }

[ -n "$NAME" ]   || fail "--name is required"
[ -n "$COMMIT" ] || fail "--commit is required"
[ -n "$SLOT" ]   || fail "--slot is required"
[ -n "$JOBS" ]   || JOBS=$(nproc 2>/dev/null || echo 4)
case "$COMMIT" in
    *[!0-9a-f]*) fail "--commit takes a full sha, got '$COMMIT'" ;;
esac
[ "${#COMMIT}" -eq 40 ] || fail "--commit takes a full 40-character sha, got '$COMMIT'"

WORKDIR="$SRC/WebKitBuild/buildroot/$NAME"
OUT="$WORKDIR/output"
SLOTDIR="$OUT/wk-slots/$SLOT"
ROOT="$SLOTDIR/root"
TCF="$OUT/host/share/buildroot/toolchainfile.cmake"

[ -d "$WORKDIR/package" ] || fail "no buildroot tree at $WORKDIR.
    A slot is built beside a finished image; build the image first:
        wk sysimage build $NAME"
[ -f "$OUT/build/packages-file-list.txt" ] || fail "the image in $WORKDIR was never built to the end
    (no output/build/packages-file-list.txt); 'wk sysimage build $NAME' first."
grep -q -- '--build-id' "$TCF" 2>/dev/null || fail "the image's toolchain file carries no --build-id
    ($TCF). Every slot binary must carry the identifier the board is checked
    against; the image build sets it (BR2_TARGET_LDFLAGS) and regenerates the
    file:  wk sysimage build $NAME   (incremental)"

# BR2_EXTERNAL must be passed on every make: buildroot records it in
# output/.br-external.mk, and a later make without it fails on the recorded path.
BR_EXT=""
[ -f /opt/wk-tools/image/buildroot/external/external.desc ] \
    && BR_EXT="BR2_EXTERNAL=/opt/wk-tools/image/buildroot/external"

# --- the source ------------------------------------------------------------------
# The workspace's own checkout, moved to the commit. Refused dirty: a slot
# must be reproducible from its sha alone.
dirty=$(git -C "$SRC" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
[ "$dirty" = 0 ] || fail "$SRC has $dirty uncommitted change(s); a slot is built from a
    commit and nothing else. Commit or discard them in the workspace first."
if ! git -C "$SRC" cat-file -e "$COMMIT^{commit}" 2>/dev/null; then
    say "fetching $COMMIT from the mirror"
    git -C "$SRC" fetch --quiet "$MIRROR" "$COMMIT" \
        || fail "$COMMIT is not in this machine's mirror ($MIRROR).
    'wk ab' and 'wk pr' fetch a PR head into the mirror first; a bare sha has
    to be reachable from a branch the mirror carries."
fi
git -C "$SRC" checkout --detach --quiet "$COMMIT" \
    || fail "could not check out $COMMIT in $SRC"
say "source      $SRC @ $(git -C "$SRC" rev-parse --short HEAD) ($(git -C "$SRC" log -1 --format=%s | cut -c1-60))"
say "slot        $SLOT -> $SLOTDIR"
say "jobs        -j$JOBS"

# --- the override ----------------------------------------------------------------
# BR2_PACKAGE_OVERRIDE_FILE is $(CONFIG_DIR)/local.mk on this tree. The rsync
# exclusions keep buildroot's copy of the checkout to what a build reads:
# WebKitBuild is this very tree, LayoutTests is a gigabyte of nothing a
# compiler opens.
cat > "$WORKDIR/local.mk" <<EOF
# Written by wk (image/buildroot-webkit.sh) for slot '$SLOT'; dropped by the
# next image build. Points the wpewebkit package at the workspace checkout.
WPEWEBKIT_OVERRIDE_SRCDIR = $SRC
WPEWEBKIT_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = --exclude WebKitBuild --exclude LayoutTests --exclude JSTests --exclude ManualTests --exclude WebDriverTests --exclude Websites
EOF

# --- the build -------------------------------------------------------------------
# Under the guard every heavy step in a target runs under (build/guard.sh).
. /opt/wk-tools/build/guard.sh
build_start=$(date +%s)
say "building (make wpewebkit-rebuild; the output below is the whole account of it)"
# shellcheck disable=SC2086
WK_MB_PER_JOB=2048 guard_run "$JOBS" -- env FORCE_UNSAFE_CONFIGURE=1 BR2_JLEVEL="$JOBS" \
    make -C "$WORKDIR" $BR_EXT wpewebkit-rebuild \
    || fail "the WebKit build failed. The last lines above are the failing step."
say "built in $(( ($(date +%s) - build_start) / 60 )) min"
say "finalising the root filesystem (strip, development files) the way an image build does"
# shellcheck disable=SC2086
make -C "$WORKDIR" $BR_EXT target-finalize >/dev/null || fail "target-finalize failed"

# --- the slot --------------------------------------------------------------------
# The files buildroot recorded installing for wpewebkit, as they are in
# output/target after target-finalize (the development files it removed are
# not there to copy). A fresh root every time: an install over a previous one
# keeps files the new commit no longer produces.
rm -rf "$ROOT"; mkdir -p "$ROOT"
sed -n 's/^wpewebkit,//p' "$OUT/build/packages-file-list.txt" > "$SLOTDIR/files.txt"
[ -s "$SLOTDIR/files.txt" ] || fail "packages-file-list.txt records nothing for wpewebkit"
( cd "$OUT/target" && while IFS= read -r f; do [ -e "$f" ] && printf '%s\0' "$f"; done < "$SLOTDIR/files.txt" \
    | tar --null -T - -cf - ) | tar -C "$ROOT" -xf - \
    || fail "could not copy wpewebkit's installed files into $ROOT"

# --- proof ----------------------------------------------------------------------
lib=$(find "$ROOT/usr/lib" -maxdepth 1 -name 'libWPEWebKit-*.so.*.*.*' | head -1)
[ -n "$lib" ] || fail "the slot has no libWPEWebKit under $ROOT/usr/lib"
execdir=$(find "$ROOT/usr/libexec" -maxdepth 1 -mindepth 1 -type d -name 'wpe-webkit-*' | head -1)
[ -x "$execdir/WPEWebProcess" ] || fail "no WPEWebProcess under $ROOT/usr/libexec"
bundledir=$(find "$ROOT/usr/lib" -mindepth 2 -maxdepth 2 -type d -name injected-bundle | head -1)
[ -f "$bundledir/libWPEInjectedBundle.so" ] || fail "no injected bundle under $ROOT/usr/lib"

# The toolchain's own readelf reads the build-id; its prefix is the compiler's.
readelf="$OUT/host/bin/$(sed -n 's/^set(CMAKE_C_COMPILER .*bin\/\(.*\)-gcc")$/\1/p' "$TCF")-readelf"
python3 /opt/wk-tools/lib/wkslot.py manifest --readelf "$readelf" "$ROOT" "$SLOTDIR/slot.json" \
    slot="$SLOT" profile="$NAME" commit="$COMMIT" \
    browser=cog lib_dir="usr/lib" exec_dir="${execdir#"$ROOT/"}" bundle_dir="${bundledir#"$ROOT/"}" \
    jobs="$JOBS" built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    wk_tools="$(git -C /opt/wk-tools rev-parse --short HEAD 2>/dev/null || echo unknown)" \
    || fail "could not write $SLOTDIR/slot.json"

say "slot ready: $SLOTDIR"
say "  $(du -sh "$ROOT" | cut -f1) in root/, $(basename "$lib") build-id $(python3 /opt/wk-tools/lib/wkslot.py get "$SLOTDIR/slot.json" build_id)"
say "stage 'webkit-$SLOT' done"
