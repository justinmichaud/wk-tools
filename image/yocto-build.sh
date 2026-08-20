#!/usr/bin/env bash
#
# The yocto image build, as it runs INSIDE a workspace.
#
# The host half is image/yocto.sh; this is the part that runs where the
# toolchain, the checkout and the caches are. Split the same way
# build/build-in-target.sh is split from cmd/build, and for the same reason:
# the environment a build needs is long, and assembling it over ssh or through
# `podman exec` quoting is how it ends up subtly different from the environment
# that was tested.
#
# Everything here is one of five things:
#
#   1. undoing the SDK's environment, which bitbake refuses to run inside
#   2. the host-tooling and locale preflight, so a missing package is reported
#      in the first second rather than by bitbake's sanity checker
#   2b. re-resolving bitbake's own host-tool symlinks, which it caches
#   3. local.conf additions -- the caches, the parallelism, the mirror
#   4. one call to WebKit's own Tools/Scripts/cross-toolchain-helper per stage
#
# Nothing here reimplements any part of the Yocto build. cross-toolchain-helper
# is the upstream interface and stays the interface; what this adds is the
# things it deliberately leaves to whoever drives it.

set -euo pipefail

TARGET=""; IMAGE=""; STAGE=image; JOBS=""; RM_WORK=1; SRC=/src/WebKit
CHROMIUM=0; SSTATE_NS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --target)  TARGET="${2:-}"; shift 2 ;;
        --image)   IMAGE="${2:-}"; shift 2 ;;
        --stage)   STAGE="${2:-}"; shift 2 ;;
        --jobs)    JOBS="${2:-}"; shift 2 ;;
        --rm-work) RM_WORK="${2:-}"; shift 2 ;;
        --src)     SRC="${2:-}"; shift 2 ;;
        --chromium) CHROMIUM="${2:-}"; shift 2 ;;
        --sstate-ns) SSTATE_NS="${2:-}"; shift 2 ;;
        *) echo "yocto-build.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

say()  { printf 'wk-yocto: %s\n' "$*"; }
fail() { printf 'wk-yocto: error: %s\n' "$*" >&2; exit 1; }

# Nothing here may fail silently. This script runs detached, so its log is the
# only channel there is, and `set -euo pipefail` exits with no message at all --
# which is how one run ended, after a `find` on a directory that did not exist
# yet fed `pipefail` a failure. An ERR trap costs one line and turns that into
# an address.
trap 'printf "wk-yocto: error: line %s: \"%s\" exited %s\n" "$LINENO" "$BASH_COMMAND" "$?" >&2' ERR

[ -n "$TARGET" ] || fail "--target is required"
[ -d "$SRC" ]    || fail "no checkout at $SRC"

# --- 1. undo the SDK's environment -------------------------------------------
#
# The wiki calls this step "VERY IMPORTANT" in capitals, and it is not
# superstition. The wkdev SDK exports a development environment -- include
# paths, a pkg-config path, a library path -- so that a native WebKit build
# finds the jhbuild prefix. Every one of those variables is poison to a cross
# build: bitbake's own sanity checker refuses to start with LD_LIBRARY_PATH
# set, and the include/pkg-config paths are worse than a refusal, because they
# silently offer the *host's* aarch64 headers to a compiler building for the
# target. That produces a build that works and an image that does not.
#
# Unset rather than filtered through BB_ENV_PASSTHROUGH: they must be gone for
# the `repo` sync and the native tools that run before bitbake is reached too.
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH OBJC_INCLUDE_PATH \
      OBJCPLUS_INCLUDE_PATH PKG_CONFIG_PATH PKG_CONFIG_LIBDIR \
      LD_LIBRARY_PATH LD_PRELOAD 2>/dev/null || true

# DL_DIR and SSTATE_DIR arrive as environment variables (targets/container.sh
# sets them to the store-backed cache mount), and bitbake filters the
# environment: without naming them here they are dropped, the build silently
# uses TOPDIR/downloads instead, and the cache that is supposed to survive `wk
# rm` never gets written. They are also written into local.conf below -- belt
# and braces, because a bitbake dev shell entered by hand reads the
# environment and not our local.conf additions.
export BB_ENV_PASSTHROUGH_ADDITIONS="${BB_ENV_PASSTHROUGH_ADDITIONS:-} DL_DIR SSTATE_DIR"

# A UTF-8 locale, or poky's sanity check stops the build. en_US.UTF-8 when the
# image has it (poky's own documentation names it), C.UTF-8 otherwise -- which
# is always present on a glibc system and satisfies the same check.
if locale -a 2>/dev/null | grep -qix 'en_US.utf-\?8'; then
    export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
else
    export LANG=C.UTF-8 LC_ALL=C.UTF-8
fi

# --- 2. preflight ------------------------------------------------------------
#
# Collected and reported together. bitbake's own sanity checker does find
# these, but one at a time and several minutes in, after the layer sync -- and
# each missing package costs a whole round trip through that.
missing=""
for t in gawk chrpath diffstat cpio makeinfo socat file zstd \
         xz bzip2 gcc g++ perl python3 git patch which unzip wget; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
# lz4 ships the tool under either name depending on the Ubuntu release
# (liblz4-tool became lz4); the recipes call whichever is on PATH.
command -v lz4c >/dev/null 2>&1 || command -v lz4 >/dev/null 2>&1 \
    || missing="$missing lz4" 
python3 -c 'import git' 2>/dev/null || missing="$missing python3-git"
python3 -c 'import jinja2' 2>/dev/null || missing="$missing python3-jinja2"
python3 -c 'import pexpect' 2>/dev/null || missing="$missing python3-pexpect"
if [ -n "$missing" ]; then
    fail "the workspace image is missing Yocto host tooling:$missing

    These are host-side build dependencies of the Yocto build, so they belong
    in the SDK image rather than being apt-installed into a workspace that is
    thrown away. Add them to webkit-container-sdk's
    images/wkdev_sdk/required_system_packages and rebuild the image; the
    Ubuntu package names are in the wiki's Yocto page. For a one-off, 'wk
    enter' and apt-install them by hand -- and expect to do it again in the
    next workspace."
fi

# bitbake will not run as root, and a container makes that mistake easy to
# make. Said here rather than discovered from bitbake's own message, which
# does not mention that this is a container.
[ "$(id -u)" != 0 ] || fail "bitbake refuses to run as root, and this shell is root.
    A workspace's builds run as the workspace user; something started this one
    with 'podman exec -u root' or similar."

# The caches. Their absence is not fatal -- bitbake would create them -- but
# their absence *here* means the store mount is missing, and the whole build
# would then download 12 GB into a layer that `wk rm` deletes.
for d in "${DL_DIR:-}" "${SSTATE_DIR:-}"; do
    [ -n "$d" ] || fail "DL_DIR/SSTATE_DIR are not set in this workspace.
    They come from the container's store-backed cache mount
    (targets/container.sh); without them the Yocto download and sstate caches
    would land in the workspace and die with it."
    mkdir -p "$d" || fail "cannot create $d"
    [ -w "$d" ] || fail "$d is not writable"
done

# --- 2b. the toolchain -------------------------------------------------------
#
# There is nothing to install: the workspace image *is* a supported Yocto build
# host (Ubuntu 24.04, GCC 13, Python 3.12, glibc 2.39), which is what scarthgap
# was written against. This was forty lines of buildtools-tarball handling while
# the image was a layer on the wkdev SDK; container/yocto/Containerfile records
# why the base image changed instead. One piece of it is worth keeping.

# bitbake does not run tasks with our PATH: it builds `tmp/hosttools`, a
# directory of symlinks to the host tools it found, and gives tasks *that* as
# their PATH. It creates each link only when one is missing, so a hosttools
# directory built by an earlier run keeps pointing wherever it pointed then.
#
# That cost a whole debugging cycle once: a run from before the toolchain was
# fixed left `gcc -> /usr/bin/gcc`, and the next run reported the new compiler
# in its log and quietly built with the old one. Symlinks only, so discarding
# them recompiles nothing.
clear_hosttools() {
    if [ -d "$WORKDIR/build/tmp/hosttools" ]; then
        say "clearing tmp/hosttools so bitbake re-resolves the host tools"
        rm -rf "$WORKDIR/build/tmp/hosttools"
    fi
}

# The sstate cache is namespaced by the build-host image, and this is a
# correctness requirement rather than tidiness.
#
# bitbake said it outright on the abandoned 26.04 host: "Disabling uninative so
# that sstate is not corrupted." A build with uninative off and a build with it
# on do not produce interchangeable sstate -- and *target* sstate paths carry no
# host marker, so bitbake will happily hand the one to the other. It did: this
# build reused 3007 packages written under the old host, and then `pseudo` failed
# to intercept `*at()` syscalls in `do_package` for five recipes
# ("got *at() syscall for unknown directory", "tar: Cannot mkdir: Bad address").
#
# So each build-host image gets its own namespace, which makes the mixing
# impossible instead of documented. DL_DIR stays shared, because a source
# tarball is a source tarball whatever built it -- and it is the 24 GB that is
# actually expensive to refill.
if [ -n "$SSTATE_NS" ]; then
    SSTATE_DIR="$SSTATE_DIR/$SSTATE_NS"
    export SSTATE_DIR
    mkdir -p "$SSTATE_DIR" || fail "cannot create $SSTATE_DIR"
fi

# Parallelism. Two knobs, and bitbake needs both: BB_NUMBER_THREADS is how many
# recipes run at once, PARALLEL_MAKE is the -j inside each one. Their product is
# the real load, which is why neither is set to the core count.
if [ -z "$JOBS" ]; then
    JOBS=$(nproc 2>/dev/null || echo 4)
fi
BB_THREADS=$(( JOBS / 4 )); [ "$BB_THREADS" -lt 1 ] && BB_THREADS=1
PAR_MAKE=$(( JOBS / BB_THREADS ))

WORKDIR="$SRC/WebKitBuild/CrossToolChains/$TARGET"
CONF="$WORKDIR/build/conf/local.conf"


cd "$SRC"

say "target        $TARGET"
say "image recipe  ${IMAGE:-<from targets.conf>}"
say "stage         $STAGE"
say "chromium      $([ "$CHROMIUM" = 0 ] && echo 'dropped (about half the build; --chromium puts it back)' || echo 'in the image (--chromium)')"
say "branch        $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') at $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
say "jobs          BB_NUMBER_THREADS=$BB_THREADS PARALLEL_MAKE=-j$PAR_MAKE (from $JOBS)"
say "DL_DIR        $DL_DIR"
say "SSTATE_DIR    $SSTATE_DIR"
say "locale        $LANG"
say "toolchain     gcc $(gcc -dumpversion 2>/dev/null || echo '?'), python $(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || echo '?')"

# The target has to exist in *this* checkout, and that is a property of the
# branch rather than of this repo. Checked before anything is created, because
# the answer is a one-line grep and the alternative is finding out after a
# 20-minute layer sync.
Tools/Scripts/cross-toolchain-helper --print-available-targets --log-level quiet \
    | grep -qx "$TARGET" \
    || fail "'$TARGET' is not a cross-target in this checkout.
    Available here:
$(Tools/Scripts/cross-toolchain-helper --print-available-targets --log-level quiet | sed 's/^/      /')"

# --- 3. the layer sync -------------------------------------------------------
#
# cross-toolchain-helper initialises its workdir inside YoctoCrossBuilder's
# constructor, so *any* action does it. The cheapest one that does nothing else
# is --bitbake-dev-shell with no stdin: it syncs the layers, writes conf/, and
# then starts a bash that reaches EOF immediately and exits.
#
# Doing it as its own step is what makes the expensive stage restartable and
# the cheap stage testable: a 20-minute `repo sync` and a multi-hour bitbake
# have completely different failure modes, and mixing them means every
# network-side failure looks like a build failure.
init_workdir() {
    if [ -f "$WORKDIR/.target-info-version" ] && [ -f "$CONF" ]; then
        say "layers already synced at $WORKDIR"
        return 0
    fi
    say "syncing Yocto layers (repo sync -- this is the network-bound part)"
    # The helper downloads the `repo` tool itself and clones git-repo's own
    # bootstrap, so this step needs egress that the build stage does not.
    Tools/Scripts/cross-toolchain-helper --cross-target="$TARGET" \
        --bitbake-dev-shell < /dev/null > /dev/null \
        || fail "the layer sync failed. It is almost always egress: 'repo'
    fetches from git.yoctoproject.org, github.com and gerrit.googlesource.com,
    and a workspace reaches all three only through the proxy allowlist
    (container/proxy/wk-proxy.py). Its log names what was refused."
    [ -f "$CONF" ] || fail "the layer sync reported success but wrote no $CONF"
}

# --- 4. local.conf ----------------------------------------------------------
#
# Appended, never rewritten: local.conf is generated from the branch's own
# rpi/local-*.conf, and cross-toolchain-helper appends a line of its own after
# that. Ours goes last, which is also what makes it win -- bitbake takes the
# last assignment.
#
# Guarded by a marker so it is idempotent. This function runs on every build,
# including the ones that resume an interrupted one, and three copies of
# `INHERIT += "rm_work"` is not the same as one.
# Rewritten on every run, not skipped when present. An earlier version returned
# early if the marker was there, which made every flag that lands in local.conf
# -- rm_work, chromium, the job counts -- take effect only on the run that
# happened to create the file. A knob that silently does nothing on the second
# invocation is worse than no knob.
#
# Safe to truncate from the marker to EOF because ours is last: the branch's own
# local-*.conf comes first and cross-toolchain-helper appends its one line at
# init, both before this.
MARKER='# --- wk (image/yocto-build.sh) ---'
configure_local_conf() {
    [ -f "$CONF" ] || fail "no $CONF -- the layer sync did not run"
    if grep -qF "$MARKER" "$CONF"; then
        say "refreshing the wk additions in local.conf"
        sed -i "/^$(printf '%s' "$MARKER" | sed 's/[][\.*^$/]/\\&/g')\$/,\$d" "$CONF"
    else
        say "appending the wk additions to local.conf"
    fi
    {
        printf '\n%s\n' "$MARKER"
        printf '# Written by image/yocto-build.sh. Edit that, not this: the\n'
        printf '# workdir is wiped whenever the target config changes.\n\n'

        # The caches, by absolute path. Also in the environment, but bitbake
        # filters that and a value in local.conf cannot be filtered.
        printf 'DL_DIR = "%s"\n' "$DL_DIR"
        printf 'SSTATE_DIR = "%s"\n\n' "$SSTATE_DIR"

        printf 'BB_NUMBER_THREADS = "%s"\n' "$BB_THREADS"
        printf 'PARALLEL_MAKE = "-j %s"\n\n' "$PAR_MAKE"

        # One host for almost every fetch. Two things come out of this: the
        # build stops depending on a hundred upstream servers still being up
        # and still serving the same bytes, and -- because a workspace reaches
        # the network only through a hostname allowlist -- the set of names
        # that has to be allowed collapses to something a person can read.
        # Not PREMIRRORONLY: layers outside poky (meta-webkit, meta-clang,
        # meta-raspberrypi) fetch from github, which the mirror does not carry,
        # and forbidding the upstream fallback outright would fail those.
        printf 'INHERIT += "own-mirrors"\n'
        printf 'SOURCE_MIRROR_URL = "https://downloads.yoctoproject.org/mirror/sources/"\n'
        printf 'BB_GENERATE_MIRROR_TARBALLS = "1"\n\n'

        # A shared workstation's root filesystem is not the build's to fill.
        # The stock BB_DISKMON_DIRS in the branch's local.conf halts at 100 MB
        # free, which is far too late on a disk this machine also runs from.
        printf 'BB_DISKMON_DIRS = "\\\n'
        printf '    STOPTASKS,${TMPDIR},10G,100K \\\n'
        printf '    STOPTASKS,${DL_DIR},5G,100K \\\n'
        printf '    STOPTASKS,${SSTATE_DIR},5G,100K \\\n'
        printf '    HALT,${TMPDIR},5G,1K \\\n'
        printf '    HALT,${DL_DIR},2G,1K \\\n'
        printf '    HALT,${SSTATE_DIR},2G,1K"\n\n'

        # Chromium. The branch's own local-rpi4-64bits-mesa.conf adds it --
        # "Add chromium to image to be able to compare WPE/Chromium
        # performance" -- and that is a real reason on a fleet whose whole
        # purpose is comparative browser benchmarking, so upstream's choice is
        # the default here. It is also, by a wide margin, the most expensive
        # thing in the build: measured on this run, `chromium-ozone-wayland`
        # and `gn-native` were 21 GB of TMPDIR each, plus rust-native,
        # cargo-native and rust-llvm-native behind them, and roughly half of
        # the 13,379 tasks. Dropping it is what to do when the image is wanted
        # for WPE alone or the disk is tight.
        if [ "$CHROMIUM" = 0 ]; then
            printf '# Chromium dropped: about half the build. --chromium puts it back.\n'
            printf 'IMAGE_INSTALL:remove = "chromium-ozone-wayland"\n\n'
        fi

        if [ "$RM_WORK" = 1 ]; then
            # Measured on this build rather than estimated, because the
            # estimate was wrong: with rm_work on, TMPDIR was **79 GB** at 62%
            # of the tasks, not the ~30 GB first written here. rm_work reclaims
            # a recipe's tree only once that recipe's whole chain finishes, and
            # with 19 running at once -- two of them Chromium and gn at 21 GB
            # each -- the peak is set by what is in flight, not by what is
            # done. It is still the difference between finishing and filling
            # the disk; it is not a way to keep TMPDIR small.
            # What it costs is the unpacked
            # source and build tree of each recipe after that recipe is built,
            # which is exactly what `bitbake -c menuconfig virtual/kernel` and
            # a devshell want -- so it is a knob (YOC_RM_WORK) and not a fact,
            # and the kernel-configuration flow in the wiki wants it off.
            # sstate is untouched by it, so rebuilds stay fast.
            printf 'INHERIT += "rm_work"\n'
            printf 'RM_WORK_EXCLUDE += "%s"\n\n' "${IMAGE:-webkit-dev-ci-tools}"
        fi
    } >> "$CONF"
}

# Run a bitbake command directly.
#
# cross-toolchain-helper has no pass-through for an arbitrary bitbake command --
# it exposes `--build-image`, `--build-toolchain` and a dev shell, and nothing
# else -- so the three lines it uses internally are reproduced here. Two things
# have to match it or the build is subtly different:
#
#   the build directory.  `oe-init-build-env <builddir>`, from inside poky.
#
#   WEBKIT_CROSS_TARGET and WEBKIT_CROSS_VERSION.  meta-webkit's distro conf
#   reads both out of BB_ORIGENV and, when they are absent, falls back to the
#   machine name and *today's date* -- which lands in DISTRO_VERSION, and so in
#   the signature of everything that depends on it. Running bitbake without
#   them would quietly invalidate sstate and change what is being built. They
#   are read from the file the helper already wrote rather than recomputed: the
#   hash is over every file that can affect the build, and duplicating that
#   calculation here would be a second copy of it that can disagree.
bb() {
    local info="$WORKDIR/.target-info-version"
    [ -f "$info" ] || fail "no $info -- the layer sync did not finish"
    WEBKIT_CROSS_TARGET=$(cut -d' ' -f1 < "$info")
    WEBKIT_CROSS_VERSION=$(cut -d' ' -f2 < "$info")
    export WEBKIT_CROSS_TARGET WEBKIT_CROSS_VERSION
    # `set +u` around the sourcing: oe-init-build-env reads BBSERVER and other
    # variables it does not first define, so under `set -u` it aborts with
    # "BBSERVER: unbound variable" -- which is why cross-toolchain-helper runs
    # it in a plain `bash -c` and why this has to as well. In a subshell, so the
    # relaxation does not outlive the call.
    ( set +u
      cd "$WORKDIR/sources/poky" \
      && . ./oe-init-build-env "$WORKDIR/build" >/dev/null \
      && bitbake "$@" )
}

# --- bblayers ----------------------------------------------------------------
#
# One extra layer, `image/yocto/meta-wk`, for local fixes to the build. Appended
# to the generated bblayers.conf rather than added to the branch's own
# rpi/bblayers.conf template: the same reasoning as the local.conf additions,
# and the same pattern the wiki's custom-kernel flow uses with a local
# meta-webkit checkout.
#
# Rewritten each run for the same reason configure_local_conf is -- a guard that
# skipped when its marker was present would make an edit to the layer take
# effect only on the run that created the file.
BB_MARKER='# --- wk layers (image/yocto-build.sh) ---'
configure_bblayers() {
    local f="$WORKDIR/build/conf/bblayers.conf"
    local layer; layer=$(cd "$(dirname "$0")/../image/yocto/meta-wk" && pwd)
    [ -f "$f" ] || fail "no $f -- the layer sync did not run"
    [ -f "$layer/conf/layer.conf" ] || fail "no layer at $layer"

    if grep -qF "$BB_MARKER" "$f"; then
        sed -i "/^$(printf '%s' "$BB_MARKER" | sed 's/[][\.*^$/]/\\&/g')\$/,\$d" "$f"
    fi
    {
        printf '\n%s\n' "$BB_MARKER"
        printf '# Build-time fixes only -- see that layer conf/layer.conf.\n'
        printf 'BBLAYERS += "%s"\n' "$layer"
    } >> "$f"
    say "layer added: $layer"
}

run_helper() {
    local what="$1"; shift
    say "$what"
    Tools/Scripts/cross-toolchain-helper --cross-target="$TARGET" "$@" \
        || fail "$what failed"
}

case "$STAGE" in
    layers)
        init_workdir
        configure_local_conf
        configure_bblayers
        say "layers ready at $WORKDIR"
        ;;
    fetch)
        # Every source, and nothing built. `-k` is the whole point: without it
        # bitbake halts on the first unreachable host, so growing the egress
        # allowlist from evidence would cost one full run per host. With it, one
        # pass names all of them, and the proxy's log is the list.
        #
        # Useful in its own right as well -- it primes DL_DIR, which is the
        # store-backed cache, so the compile that follows is offline.
        init_workdir
        configure_local_conf
        configure_bblayers
        clear_hosttools
        say "fetching every source for ${IMAGE:-the image} (--runall=fetch -k)"
        bb --runall=fetch -k "${IMAGE:-webkit-dev-ci-tools}" \
            || warn_fetch=1
        if [ -n "${warn_fetch:-}" ]; then
            say "some fetches failed; the ERROR lines above name the recipes and"
            say "the proxy's log names the hosts it refused:"
            say "  journalctl --user -u wk-proxy -g DENY"
            fail "not every source could be fetched"
        fi
        ;;
    image|all)
        init_workdir
        configure_local_conf
        configure_bblayers
        clear_hosttools
        run_helper "bitbake ${IMAGE:-the image} (rootfs + kernel + wic)" --build-image
        ;;
    toolchain)
        init_workdir
        configure_local_conf
        configure_bblayers
        clear_hosttools
        run_helper "bitbake populate_sdk (the cross toolchain)" --build-toolchain
        ;;
    webkit)
        # The other half of a useful board: the image is only the runtime, and
        # carries no WebKit at all (meta-webkit's webkit-dev-ci-tools image is
        # explicit about that). This cross-builds the checkout against the
        # image's own toolchain, which is what `built-product-archive` then
        # packs up for the board.
        init_workdir
        configure_local_conf
        configure_bblayers
        clear_hosttools
        say "cross-building WebKit (WPE, Release) for $TARGET"
        # No --cmakeargs here, deliberately. targets.conf sets
        # BUILD_WEBKIT_ARGS for this target with a --cmakeargs of its own, and
        # build-webkit takes only one: a second on the command line replaces
        # the first, silently dropping the target's flags (the same trap
        # `wk build --cmake` exists to prevent). MiniBrowser is on by default
        # for WPE developer builds anyway -- this target's flags do not turn it
        # off -- so the wiki's extra -DENABLE_MINIBROWSER=ON buys nothing and
        # costs the rest.
        Tools/Scripts/build-webkit --wpe --release --cross-target="$TARGET" \
            || fail "the cross build of WebKit failed"
        ;;
    *)  fail "unknown stage '$STAGE' (layers, fetch, image, toolchain, webkit)" ;;
esac

say "stage '$STAGE' done"
