#!/usr/bin/env bash
#
# The yocto image build, as it runs INSIDE a workspace; image/yocto.sh is
# the host half. Tools/Scripts/cross-toolchain-helper stays the upstream
# interface for the build; this only adds what it leaves to whoever drives it.

set -euo pipefail

# The build wall (container/bin/wk-build-wall) lets ninja/cmake/make through
# for wk's own builds and refuses them to an agent's shell.
export WK_BUILD=1

# The wall comes off PATH altogether here, which WK_BUILD alone cannot achieve.
# bitbake resolves each HOSTTOOLS name once, symlinks what it found into
# tmp/hosttools, and runs every task with that directory as the whole PATH.
# container/bin sits ahead of /usr/bin (shell/path.sh), so what gets captured is
# the wall, leaving it no real tool to hand off to: gcc-cross-canadian's
# do_compile dies in oe_runmake with "not on PATH". Both trees, since a person's
# clone and the one `wk` pushed are routinely both on PATH.
_strip_wall_from_path() {
    local out="" d oldifs=$IFS
    IFS=:
    set -- $PATH
    IFS=$oldifs
    for d in "$@"; do
        [ -n "$d" ] || continue
        case "$d" in */container/bin) continue ;; esac
        out="${out:+$out:}$d"
    done
    printf '%s' "$out"
}
PATH=$(_strip_wall_from_path)
export PATH

TARGET=""; IMAGE=""; STAGE=image; JOBS=""; RM_WORK=1; SRC=/src/WebKit; COMMIT=""; SLOT=""; PROFILE=""
MULTILIB=""; MULTILIB_TUNE=""
CHROMIUM=0; SSTATE_NS=""
PORT_TARGET_FROM=""; PORT_MACHINE=""

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
        --port-target-from) PORT_TARGET_FROM="${2:-}"; shift 2 ;;
        --multilib)      MULTILIB="${2:-}"; shift 2 ;;
        --multilib-tune) MULTILIB_TUNE="${2:-}"; shift 2 ;;
        --port-machine)     PORT_MACHINE="${2:-}"; shift 2 ;;
        --local-layer) LOCAL_LAYER="${2:-}"; shift 2 ;;
        --tailnet) TAILNET="${2:-}"; shift 2 ;;
        --webkit-jobs) WEBKIT_JOBS="${2:-}"; shift 2 ;;
        --commit)  COMMIT="${2:-}"; shift 2 ;;
        --slot)    SLOT="${2:-}"; shift 2 ;;
        --profile) PROFILE="${2:-}"; shift 2 ;;
        *) echo "yocto-build.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

say()  { printf 'wk-yocto: %s\n' "$*"; }
fail() { printf 'wk-yocto: error: %s\n' "$*" >&2; exit 1; }

# Runs detached, so this log is the only channel; `set -euo pipefail` alone
# exits with no message.
trap 'printf "wk-yocto: error: line %s: \"%s\" exited %s\n" "$LINENO" "$BASH_COMMAND" "$?" >&2' ERR

[ -n "$TARGET" ] || fail "--target is required"
[ -d "$SRC" ]    || fail "no checkout at $SRC"

# The wkdev SDK's dev environment breaks a cross build: LD_LIBRARY_PATH makes
# bitbake's sanity checker refuse to start, and the include/pkg-config paths
# hand the host's headers to a compiler building for the target. Unset rather
# than filtered through BB_ENV_PASSTHROUGH: `repo` sync and native tools need
# them gone too.
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH OBJC_INCLUDE_PATH \
      OBJCPLUS_INCLUDE_PATH PKG_CONFIG_PATH PKG_CONFIG_LIBDIR \
      LD_LIBRARY_PATH LD_PRELOAD 2>/dev/null || true

# A git index this build writes is read by the toolchains it builds, which pin
# their own libgit2. dotfiles/gitconfig turns on `feature.manyFiles` and
# `index.skipHash`, and both change the format of every index git writes:
# manyFiles implies index version 4, skipHash leaves the trailing checksum null.
# `cross-toolchain-helper` does a `git init` in its workdir, and cargo walks up
# from the rust sources it fingerprints, finds that index and cannot open it --
# rust-native 1.75.0's do_install fails with "failed to open git index" on the
# 2.42/2.46/2.48 branches. 2.52 pins a newer rust whose libgit2 copes.
#
# GIT_CONFIG_* rather than editing the gitconfig: these win over every config
# file and apply only to this build.
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=index.version   GIT_CONFIG_VALUE_0=2
export GIT_CONFIG_KEY_1=index.skipHash  GIT_CONFIG_VALUE_1=false

# The pin above governs indexes written by git in this environment, but cargo
# reads whichever index is already on disk: the checkout's and the helper's own
# workdir repo. Both are rewritten below, and the pin is written into each
# repo's own config too, because `cross-toolchain-helper` re-initialises its
# workdir inside every action it performs and runs git there in an environment
# this script does not govern.
#
# `--really-refresh` rather than a read-tree: it rewrites the index without
# touching what is staged, so a workspace with work in progress keeps it. Its
# status is ignored -- it reports non-zero whenever a path differs from the
# index, which is certain here since the target port modifies targets.conf.
refresh_git_index() { # <dir> -- leave an index libgit2 can open, and keep it so
    [ -e "$1/.git" ] || return 0
    git -C "$1" config index.skipHash false >/dev/null 2>&1 || true
    git -C "$1" config index.version 2     >/dev/null 2>&1 || true
    git -C "$1" update-index --really-refresh >/dev/null 2>&1 || true
}
refresh_git_index "$SRC"

# bitbake filters the environment, so DL_DIR/SSTATE_DIR (set by
# targets/container.sh to the store-backed cache mount) must be named here or
# they are dropped and the cache meant to survive `wk rm` is never written.
#
# The GIT_CONFIG_* pin needs naming for the same reason: a task that runs git
# without it writes an index with a null checksum, and the next recipe whose
# cargo walks up to /src/WebKit/.git refuses it -- "invalid data in index -
# calculated checksum does not match expected", which is librsvg's do_compile.
export BB_ENV_PASSTHROUGH_ADDITIONS="${BB_ENV_PASSTHROUGH_ADDITIONS:-} DL_DIR SSTATE_DIR GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1"

# A UTF-8 locale, or poky's sanity check stops the build; C.UTF-8 is the
# fallback always present on a glibc system.
if locale -a 2>/dev/null | grep -qix 'en_US.utf-\?8'; then
    export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
else
    export LANG=C.UTF-8 LC_ALL=C.UTF-8
fi

# Together, rather than one at a time by bitbake's own sanity checker several
# minutes in, after the layer sync.
missing=""
for t in gawk chrpath diffstat cpio makeinfo socat file zstd \
         xz bzip2 gcc g++ perl python3 git patch which unzip wget; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
# lz4 ships the tool under either name depending on the Ubuntu release.
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

[ "$(id -u)" != 0 ] || fail "bitbake refuses to run as root, and this shell is root.
    A workspace's builds run as the workspace user; something started this one
    with 'podman exec -u root' or similar."

# Their absence means the store mount is missing, and the build would download
# 12 GB into a layer that `wk rm` deletes.
for d in "${DL_DIR:-}" "${SSTATE_DIR:-}"; do
    [ -n "$d" ] || fail "DL_DIR/SSTATE_DIR are not set in this workspace.
    They come from the container's store-backed cache mount
    (targets/container.sh); without them the Yocto download and sstate caches
    would land in the workspace and die with it."
    mkdir -p "$d" || fail "cannot create $d"
    [ -w "$d" ] || fail "$d is not writable"
done

# tmp/hosttools symlinks are resolved once per link, so a directory built by an
# earlier run keeps pointing at a stale compiler even after this fixes the
# environment. Symlinks only, so discarding them recompiles nothing.
clear_hosttools() {
    if [ -d "$WORKDIR/build/tmp/hosttools" ]; then
        say "clearing tmp/hosttools so bitbake re-resolves the host tools"
        rm -rf "$WORKDIR/build/tmp/hosttools"
    fi
}

# sstate built with uninative on and off is not interchangeable, and target
# sstate paths carry no host marker to stop bitbake mixing them, corrupting
# `do_package`. DL_DIR stays shared: a source tarball is a source tarball
# whatever built it.
if [ -n "$SSTATE_NS" ]; then
    SSTATE_DIR="$SSTATE_DIR/$SSTATE_NS"
    export SSTATE_DIR
    mkdir -p "$SSTATE_DIR" || fail "cannot create $SSTATE_DIR"
fi

# BB_NUMBER_THREADS times PARALLEL_MAKE is the real load, so neither alone is
# set to the core count.
if [ -z "$JOBS" ]; then
    JOBS=$(nproc 2>/dev/null || echo 4)
fi
BB_THREADS=$(( JOBS / 4 )); [ "$BB_THREADS" -lt 1 ] && BB_THREADS=1
PAR_MAKE=$(( JOBS / BB_THREADS ))

# Recipes worth more than their quarter of the machine: bitbake cannot
# rebalance PARALLEL_MAKE mid-build. Sized by cores, not memory -- the link
# step is bounded per-recipe in image/yocto/meta-wk/recipes-devtools/clang.
BIG_RECIPES="clang clang-native clang-cross-arm clang-cross-aarch64
             llvm llvm-native rust-llvm rust-llvm-native
             mozjs-115 boost gdb linux-raspberrypi"

WORKDIR="$SRC/WebKitBuild/CrossToolChains/$TARGET"
CONF="$WORKDIR/build/conf/local.conf"


cd "$SRC"

say "target        $TARGET"
say "image recipe  ${IMAGE:-<from targets.conf>}"
[ -z "${MULTILIB:-}" ] || \
    say "multilib      $MULTILIB at $MULTILIB_TUNE -- the userspace width, not the machine's"
say "stage         $STAGE"
say "chromium      $([ "$CHROMIUM" = 0 ] && echo 'dropped (about half the build; --chromium puts it back)' || echo 'in the image (--chromium)')"
say "branch        $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') at $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
say "jobs          BB_NUMBER_THREADS=$BB_THREADS PARALLEL_MAKE=-j$PAR_MAKE (from $JOBS)"
say "  long poles    -j$JOBS for $(printf '%s' "$BIG_RECIPES" | wc -w | tr -d ' ') named recipes (clang, rust-llvm, ...); links bounded in meta-wk"
say "DL_DIR        $DL_DIR"
say "SSTATE_DIR    $SSTATE_DIR"
say "locale        $LANG"
say "toolchain     gcc $(gcc -dumpversion 2>/dev/null || echo '?'), python $(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || echo '?')"

# A cross-target this branch does not have, derived from one it does, before
# the check below that would otherwise refuse. WebKit gained its rpi5 target
# after the 2.4x releases branched while the layers those branches pin already
# support the machine, so the gap is WebKit's own glue
# (image/configs/wpewebkit-2.46-yocto-rpi5-64.conf states the case).
if [ -n "${PORT_TARGET_FROM:-}" ] && [ -z "${PORT_MACHINE:-}" ]; then
    fail "this profile names a target to derive [$TARGET] from but no YOC_MACHINE for
    it to select, so the derived local.conf would name the wrong machine."
fi
# A multilib variant with no tune takes the machine's own -- the 64-bit one --
# and builds a "32-bit" image that is nothing of the kind.
if [ -n "${MULTILIB:-}" ] && [ -z "${MULTILIB_TUNE:-}" ]; then
    fail "this profile asks for the '$MULTILIB' multilib but names no
    YOC_MULTILIB_TUNE for it, so the variant would inherit the machine's own
    width and the image would not be the width it is named for."
fi
if [ -n "${PORT_TARGET_FROM:-}" ]; then
    say "porting       [$TARGET] from [$PORT_TARGET_FROM] (MACHINE=$PORT_MACHINE) -- this branch has no such section"
    python3 /opt/wk-tools/image/yocto/port-target.py \
        --yocto-dir "$SRC/Tools/yocto" --target "$TARGET" \
        --from-target "$PORT_TARGET_FROM" --machine "$PORT_MACHINE" \
        ${IMAGE:+--image "$IMAGE"} \
        || fail "could not port [$TARGET] into this checkout"
fi

# Before anything is created: cheaper than after a 20-minute layer sync.
Tools/Scripts/cross-toolchain-helper --print-available-targets --log-level quiet \
    | grep -qx "$TARGET" \
    || fail "'$TARGET' is not a cross-target in this checkout.
    Available here:
$(Tools/Scripts/cross-toolchain-helper --print-available-targets --log-level quiet | sed 's/^/      /')"

# cross-toolchain-helper initialises its workdir inside YoctoCrossBuilder's
# constructor, so any action does it; the cheapest is --bitbake-dev-shell with
# no stdin, which syncs the layers, writes conf/, and starts a bash that reaches
# EOF immediately.
init_workdir() {
    if [ -f "$WORKDIR/.target-info-version" ] && [ -f "$CONF" ]; then
        say "layers already synced at $WORKDIR"
    else
        say "syncing Yocto layers (repo sync -- this is the network-bound part)"
        # `repo` fetches the tool itself and git-repo's bootstrap, so this
        # step needs egress the build stage does not.
        Tools/Scripts/cross-toolchain-helper --cross-target="$TARGET" \
            --bitbake-dev-shell < /dev/null > /dev/null \
            || fail "the layer sync failed. It is almost always egress: 'repo'
    fetches from git.yoctoproject.org, github.com and gerrit.googlesource.com,
    and a workspace reaches all three only through the proxy allowlist
    (container/proxy/wk-proxy.py). Its log names what was refused."
        [ -f "$CONF" ] || fail "the layer sync reported success but wrote no $CONF"
    fi
    # bitbake's TMPDIR sits inside this repo, so cargo fingerprinting rust
    # source under it walks up to this index (see refresh_git_index).
    refresh_git_index "$WORKDIR"
}

# Appended last -- bitbake takes the last assignment -- and rewritten from the
# marker on every run, so a knob takes effect immediately.
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

        printf 'DL_DIR = "%s"\n' "$DL_DIR"
        printf 'SSTATE_DIR = "%s"\n\n' "$SSTATE_DIR"

        # poky's own multilib, for a profile whose userspace width is not the
        # machine's: the machine stays as it is, so the kernel, device tree and
        # firmware are unchanged, and every recipe gains a variant at the tune
        # below. meta-wk-multilib points the image's install list at those.
        if [ -n "${MULTILIB:-}" ]; then
            printf 'require conf/multilib.conf\n'
            printf 'MULTILIBS = "multilib:%s"\n' "$MULTILIB"
            printf 'DEFAULTTUNE:virtclass-multilib-%s = "%s"\n\n' \
                "$MULTILIB" "$MULTILIB_TUNE"
        fi

        printf 'BB_NUMBER_THREADS = "%s"\n' "$BB_THREADS"
        printf 'PARALLEL_MAKE = "-j %s"\n' "$PAR_MAKE"
        printf '\n# The long poles get the whole machine; see image/yocto-build.sh.\n'
        for _r in $BIG_RECIPES; do
            printf 'PARALLEL_MAKE:pn-%s = "-j %s"\n' "$_r" "$JOBS"
        done

        # bitbake stops launching tasks while the kernel reports memory stall,
        # throttling on evidence rather than a guessed static cap.
        printf '\nBB_PRESSURE_MAX_MEMORY = "10000"\n\n'

        # One host for almost every fetch keeps the workspace's hostname
        # allowlist short. Not PREMIRRORONLY: meta-webkit, meta-clang and
        # meta-raspberrypi fetch from github, which the mirror lacks.
        printf 'INHERIT += "own-mirrors"\n'
        printf 'SOURCE_MIRROR_URL = "https://downloads.yoctoproject.org/mirror/sources/"\n'
        printf 'BB_GENERATE_MIRROR_TARBALLS = "1"\n\n'

        # The branch's own 100 MB floor is too late on a disk this machine
        # also runs from.
        printf 'BB_DISKMON_DIRS = "\\\n'
        printf '    STOPTASKS,${TMPDIR},10G,100K \\\n'
        printf '    STOPTASKS,${DL_DIR},5G,100K \\\n'
        printf '    STOPTASKS,${SSTATE_DIR},5G,100K \\\n'
        printf '    HALT,${TMPDIR},5G,1K \\\n'
        printf '    HALT,${DL_DIR},2G,1K \\\n'
        printf '    HALT,${SSTATE_DIR},2G,1K"\n\n'

        # Chromium is on by default and is the most expensive part of the
        # build; --chromium=0 drops it. YOC_TAILNET=0 drops meta-wk-tailnet.
        if [ "${TAILNET:-1}" = 0 ]; then
            printf '# tailnet: off. This image joins nothing and is reachable only\n'
            printf '# over whatever LAN it lands on.\n\n'
        else
            printf 'IMAGE_INSTALL:append = " tailscale"\n\n'
        fi

        # wk-wifi-join (meta-wk-wifi): unconditional, since every profile here
        # targets a board with no cable at the bench. The credential comes from
        # the card, never from here.
        printf 'IMAGE_INSTALL:append = " wk-wifi-join"\n\n'

        # wk-card-priv (meta-wk-rescue): unconditional too, since any yocto
        # image may be written as a board's rescue and a rescue that cannot
        # write the other medium leaves the A/B to a person with a card reader.
        printf 'IMAGE_INSTALL:append = " wk-card-priv"\n\n'

        if [ "$CHROMIUM" = 0 ]; then
            printf '# Chromium dropped: about half the build. --chromium puts it back.\n'
            printf 'IMAGE_INSTALL:remove = "chromium-ozone-wayland"\n\n'
        fi

        if [ "$RM_WORK" = 1 ]; then
            # rm_work reclaims a recipe's tree only once its whole chain
            # finishes, so the TMPDIR peak is set by what is in flight. It
            # costs the unpacked source tree a devshell wants, hence a knob.
            printf 'INHERIT += "rm_work"\n'
            printf 'RM_WORK_EXCLUDE += "%s"\n\n' "${IMAGE:-webkit-dev-ci-tools}"
        fi
    } >> "$CONF"
}

# cross-toolchain-helper exposes only --build-image, --build-toolchain and a
# dev shell, so the three lines it uses internally are reproduced here.
# WEBKIT_CROSS_TARGET/VERSION are read from the file the helper wrote: absent
# from BB_ORIGENV, meta-webkit's distro conf falls back to the machine name and
# today's date, invalidating sstate via DISTRO_VERSION.
bb() {
    local info="$WORKDIR/.target-info-version"
    [ -f "$info" ] || fail "no $info -- the layer sync did not finish"
    WEBKIT_CROSS_TARGET=$(cut -d' ' -f1 < "$info")
    WEBKIT_CROSS_VERSION=$(cut -d' ' -f2 < "$info")
    export WEBKIT_CROSS_TARGET WEBKIT_CROSS_VERSION
    # oe-init-build-env reads BBSERVER and other variables it does not first
    # define, so `set -u` aborts on it.
    ( set +u
      cd "$WORKDIR/sources/poky" \
      && . ./oe-init-build-env "$WORKDIR/build" >/dev/null \
      && guard_run "$JOBS" -- bitbake "$@" )
}

# Appended to the generated bblayers.conf rather than the branch's own
# template, and rewritten each run so a layer edit takes effect immediately.
# YOC_LOCAL_LAYER=0 gets the branch's own layer set and nothing else.
BB_MARKER='# --- wk layers (image/yocto-build.sh) ---'
configure_bblayers() {
    local f="$WORKDIR/build/conf/bblayers.conf"
    [ -f "$f" ] || fail "no $f -- the layer sync did not run"

    if [ "${LOCAL_LAYER:-1}" = 0 ]; then
        if grep -qF "$BB_MARKER" "$f"; then
            sed -i "/^$(printf '%s' "$BB_MARKER" | sed 's/[][\.*^$/]/\\&/g')\$/,\$d" "$f"
        fi
        say "no local layer: this profile builds the branch's own configuration unmodified"
        return 0
    fi

    if grep -qF "$BB_MARKER" "$f"; then
        sed -i "/^$(printf '%s' "$BB_MARKER" | sed 's/[][\.*^$/]/\\&/g')\$/,\$d" "$f"
    fi
    printf '\n%s\n' "$BB_MARKER" >> "$f"

    # Four layers, not one: meta-wk changes how the image is built,
    # meta-wk-tailnet, meta-wk-wifi and meta-wk-rescue change what is on the
    # board. meta-wk-multilib joins them only for a profile that asks for a
    # multilib variant.
    local dir layer layers="meta-wk meta-wk-tailnet meta-wk-wifi meta-wk-rescue"
    [ -z "${MULTILIB:-}" ] || layers="$layers meta-wk-multilib"
    for dir in $layers; do
        layer=$(cd "$(dirname "$0")/../image/yocto/$dir" && pwd)
        [ -f "$layer/conf/layer.conf" ] || fail "no layer at $layer"
        printf 'BBLAYERS += "%s"\n' "$layer" >> "$f"
        say "layer added: $layer"
    done
}

. /opt/wk-tools/build/guard.sh
run_helper() {
    local what="$1"; shift
    say "$what"
    guard_run "$JOBS" -- Tools/Scripts/cross-toolchain-helper --cross-target="$TARGET" "$@" \
        || fail "$what failed"
}

# cross-toolchain-helper's build_image() treats the presence of a file at
# build/image/<recipe>.<ext> as proof the image is current and returns without
# calling bitbake. No flag turns this off.
clear_stale_image_copies() {
    local dir="$1"
    if [ -d "$dir" ]; then
        say "clearing $dir so the helper cannot report a previous run's image as this one's"
        rm -rf "$dir"
    fi
}

verify_image_freshness() { # <dir> <start-epoch-seconds>
    local dir="$1" start="$2" f mtime newest=0 found=0
    [ -d "$dir" ] \
        || fail "bitbake produced no image directory at $dir; the helper reported
    success but left nothing behind."
    for f in "$dir"/*; do
        [ -f "$f" ] || continue
        found=1
        mtime=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null) || continue
        [ "$mtime" -gt "$newest" ] && newest="$mtime"
    done
    [ "$found" = 1 ] \
        || fail "bitbake produced no image directory at $dir; the helper reported
    success but left nothing behind."
    [ "$newest" -ge "$start" ] \
        || fail "bitbake produced no new image; the helper reported a stale one.
    Newest file in $dir is older than this stage's own start time, which should
    be impossible: yocto-build.sh clears that directory before every image
    stage precisely so the helper cannot hand back an old copy. If this fires,
    cross-toolchain-helper changed how it decides an image is built, and
    clear_stale_image_copies (image/yocto-build.sh) needs to change with it."
}

case "$STAGE" in
    layers)
        init_workdir
        configure_local_conf
        configure_bblayers
        say "layers ready at $WORKDIR"
        ;;
    fetch)
        # `-k`: without it bitbake halts on the first unreachable host, so
        # growing the egress allowlist from evidence costs one run per host.
        # Also primes DL_DIR, so the compile that follows is offline.
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
        clear_stale_image_copies "$WORKDIR/build/image"
        image_stage_start=$(date +%s)
        run_helper "bitbake ${IMAGE:-the image} (rootfs + kernel + wic)" --build-image
        verify_image_freshness "$WORKDIR/build/image" "$image_stage_start"
        ;;
    toolchain)
        init_workdir
        configure_local_conf
        configure_bblayers
        clear_hosttools
        run_helper "bitbake populate_sdk (the cross toolchain)" --build-toolchain
        ;;
    webkit)
        # The image carries no WebKit (meta-webkit's webkit-dev-ci-tools image
        # is explicit about that); this cross-builds the checkout against the
        # image's own toolchain for `built-product-archive`.
        init_workdir
        configure_local_conf
        configure_bblayers
        clear_hosttools
        # A slot is reproducible from its sha alone, so this is refused dirty.
        if [ -n "$COMMIT" ]; then
            # Porting a cross-target modifies targets.conf and adds a
            # local.conf beside the branch's own (port-target.py), so that
            # workspace is dirty by construction; its files are excluded by
            # name and everything else still counts.
            dirty=$(git -C "$SRC" status --porcelain -- . \
                ':(exclude)Tools/yocto/targets.conf' \
                ':(exclude)Tools/yocto/*/local-*.conf' \
                2>/dev/null | wc -l | tr -d ' ')
            [ "$dirty" = 0 ] || fail "$SRC has $dirty uncommitted change(s); a slot is built from a
    commit and nothing else. Commit or discard them in the workspace first."
            # t_spawn (targets/container.sh) execs this with no WK_ROOT and
            # no lib/target.sh sourced, so there is no t_mirror_dir to ask:
            # this is the fixed bind mount that driver names
            # (tests/test_mirror_path.py's named exception).
            git -C "$SRC" cat-file -e "$COMMIT^{commit}" 2>/dev/null \
                || git -C "$SRC" fetch --quiet /mirror/WebKit.git "$COMMIT" \
                || fail "$COMMIT is not in this machine's mirror; 'wk ab' and 'wk pr' fetch a PR head into it first"
            git -C "$SRC" checkout --detach --quiet "$COMMIT" || fail "could not check out $COMMIT in $SRC"
            say "source        $SRC @ $(git -C "$SRC" rev-parse --short HEAD) ($(git -C "$SRC" log -1 --format=%s | cut -c1-60))"
        fi
        say "cross-building WebKit (WPE, Release) for $TARGET"
        # wpe-2.46 defaults both ENABLE_WPE_1_1_API and ENABLE_WPE_PLATFORM
        # on, and CMake refuses the pair. `run-benchmark`'s WPE driver -- the
        # only thing that produces a score -- hardcodes --use-wpe-platform-api
        # and --maximized (linux_minibrowserwpe_driver.py:39-40), which a
        # MiniBrowser built without WPEPlatform rejects.
        #
        # Not a second --cmakeargs on the command line: build-webkit takes only
        # one and the last wins, dropping the target's own flags (bwrap,
        # xdg-dbus-proxy) -- so the target's are read out of targets.conf and
        # ours appended.
        # No `local`: this case arm runs at file scope, not in a function.
        tgt_args=$(python3 - "$TARGET" <<'PYEOF'
import configparser, sys, shlex
c = configparser.ConfigParser()
c.read("Tools/yocto/targets.conf")
v = c[sys.argv[1]].get("environment[BUILD_WEBKIT_ARGS]", "")
# The value holds --cmakeargs="..." plus flags of its own; keep both apart.
toks = shlex.split(v)
cmake, rest = "", []
for t in toks:
    if t.startswith("--cmakeargs="):
        cmake = t[len("--cmakeargs="):]
    else:
        rest.append(t)
print(shlex.join(rest))
print(cmake)
PYEOF
) || fail "could not read BUILD_WEBKIT_ARGS for $TARGET out of Tools/yocto/targets.conf"
        tgt_cmake=$(printf '%s\n' "$tgt_args" | sed -n 2p)
        tgt_args=$(printf '%s\n' "$tgt_args" | sed -n 1p)
        extra="-DENABLE_WPE_PLATFORM=ON -DENABLE_WPE_1_1_API=OFF"
        say "  target flags: ${tgt_args:-none}"
        say "  cmakeargs:    $tgt_cmake $extra"

        # build-webkit appends `-j$(numberOfCPUs)` only when --makeargs
        # carries no -j, and that default OOMs on WebCore's unified sources.
        # bitbake never sees this stage, so its PARALLEL_MAKE cap does not
        # reach it.
        webkit_makeargs="-j${WEBKIT_JOBS:-8}"
        say "  jobs:         ${WEBKIT_JOBS:-8} (memory-sized; the default -j$(nproc) OOMs on unified sources)"

        # shellcheck disable=SC2086
        WK_MB_PER_JOB=2560 guard_run "${WEBKIT_JOBS:-8}" -- \
            Tools/Scripts/build-webkit --wpe --release --cross-target="$TARGET" \
            $tgt_args --makeargs="$webkit_makeargs" --cmakeargs="$tgt_cmake $extra" \
            || fail "the cross build of WebKit failed"

        # The slot: bin/ and lib/ of the cross build, beside the image,
        # described by a manifest (lib/wkslot.py; image_slot_dir, lib/image.sh).
        if [ -n "$SLOT" ]; then
            b="$SRC/WebKitBuild/WPE/Release_$TARGET"
            slotdir="$SRC/WebKitBuild/wk-slots/$SLOT"
            [ -x "$b/bin/MiniBrowser" ] || fail "the cross build left no $b/bin/MiniBrowser"
            rm -rf "$slotdir/root"; mkdir -p "$slotdir/root"
            cp -a "$b/bin" "$b/lib" "$slotdir/root/" || fail "could not copy the build into $slotdir"
            python3 /opt/wk-tools/lib/wkslot.py manifest "$slotdir/root" "$slotdir/slot.json" \
                slot="$SLOT" profile="$PROFILE" commit="$COMMIT" target="$TARGET" \
                browser=minibrowser lib_dir=lib exec_dir=bin bundle_dir=lib \
                jobs="${WEBKIT_JOBS:-8}" built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
                wk_tools="$(git -C /opt/wk-tools rev-parse --short HEAD 2>/dev/null || echo unknown)" \
                || fail "could not describe the build as a slot (above); a cross build with no
    build-id note cannot be told apart on the board"
            say "slot ready: $slotdir ($(du -sh "$slotdir/root" | cut -f1), build-id $(python3 /opt/wk-tools/lib/wkslot.py get "$slotdir/slot.json" build_id))"
        fi
        ;;
    *)  fail "unknown stage '$STAGE' (layers, fetch, image, toolchain, webkit)" ;;
esac

say "stage '$STAGE' done"
