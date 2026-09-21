#!/usr/bin/env bash
# The yocto image build as it runs inside a workspace (image/yocto.sh is the host half), around Tools/Scripts/cross-toolchain-helper, which stays the upstream interface for the build itself.

set -euo pipefail

export WK_BUILD=1  # the build wall (container/bin/wk-build-wall) passes ninja/cmake/make for a wk build
MIRROR="${WK_MIRROR:?WK_MIRROR names the mirror this container mounts, set by targets/container.sh}"   # t_spawn execs this with no WK_ROOT and no lib/target.sh, so there is no t_mirror_dir to ask: the container driver names the mirror in the environment (mirror_in_container, lib/target.sh)

# The wall comes off PATH entirely, which WK_BUILD cannot do: bitbake resolves each HOSTTOOLS name once into
# tmp/hosttools and runs every task with that as the whole PATH, so it captures the wall (ahead of /usr/bin) and gcc-cross-canadian's do_compile dies in oe_runmake with "not on PATH".
# Every directory under container/bin, not just container/bin: the git gate in container/bin/ws is a wall too, and poky's own scripts/git wrapper skips */scripts to find "the real git", reaches it, and the two exec each other at 100% of a core until bitbake's client gives the server up after 60s (measured).
_strip_wall_from_path() {
    local out="" d oldifs=$IFS
    IFS=:
    set -- $PATH
    IFS=$oldifs
    for d in "$@"; do
        [ -n "$d" ] || continue
        case "$d" in */container/bin|*/container/bin/*) continue ;; esac
        out="${out:+$out:}$d"
    done
    printf '%s' "$out"
}
PATH=$(_strip_wall_from_path)
export PATH

TARGET=""; IMAGE=""; STAGE=image; JOBS=""; RM_WORK=1; SRC=/src/WebKit; COMMIT=""; SLOT=""; PROFILE=""
MULTILIB=""; MULTILIB_TUNE=""
CROSS_CONFIG=""; CROSS_CC=""; CROSS_CXX=""; CROSS_CMAKE=""; PGO_DIR=""; PGO_LIB=""
CHROMIUM=0; SSTATE_NS=""
PORT_TARGET_FROM=""; PORT_MACHINE=""; BOARD=""

while [ $# -gt 0 ]; do
    case "$1" in
        --target)  TARGET="${2:-}"; shift 2 ;;
        --image)   IMAGE="${2:-}"; shift 2 ;;
        --stage)   STAGE="${2:-}"; shift 2 ;;
        --jobs)    JOBS="${2:-}"; shift 2 ;;
        # The budget the stage was admitted against (yocto_stage_budget, image/yocto.sh). Enforced rather than recomputed here: a second formula made the watchdog kill a populate_sdk at 15324MB that had been booked 17899MB.
        --mem-budget) WK_MEM_BUDGET_MB="${2:-}"; export WK_MEM_BUDGET_MB; shift 2 ;;
        --rm-work) RM_WORK="${2:-}"; shift 2 ;;
        --src)     SRC="${2:-}"; shift 2 ;;
        --chromium) CHROMIUM="${2:-}"; shift 2 ;;
        --sstate-ns) SSTATE_NS="${2:-}"; shift 2 ;;
        --port-target-from) PORT_TARGET_FROM="${2:-}"; shift 2 ;;
        --multilib)      MULTILIB="${2:-}"; shift 2 ;;
        --multilib-tune) MULTILIB_TUNE="${2:-}"; shift 2 ;;
        --port-machine)     PORT_MACHINE="${2:-}"; shift 2 ;;
        --board)            BOARD="${2:-}"; shift 2 ;;
        --local-layer) LOCAL_LAYER="${2:-}"; shift 2 ;;
        --tailnet) TAILNET="${2:-}"; shift 2 ;;
        --webkit-jobs) WEBKIT_JOBS="${2:-}"; shift 2 ;;
        --cross-config) CROSS_CONFIG="${2:-}"; shift 2 ;;
        --cross-cc)     CROSS_CC="${2:-}"; shift 2 ;;
        --cross-cxx)    CROSS_CXX="${2:-}"; shift 2 ;;
        --cross-cmake)  CROSS_CMAKE="${2:-}"; shift 2 ;;
        --pgo-dir) PGO_DIR="${2:-}"; shift 2 ;;
        --pgo-lib) PGO_LIB="${2:-}"; shift 2 ;;
        --commit)  COMMIT="${2:-}"; shift 2 ;;
        --slot)    SLOT="${2:-}"; shift 2 ;;
        --profile) PROFILE="${2:-}"; shift 2 ;;
        *) echo "yocto-build.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

say()  { printf 'wk-yocto: %s\n' "$*"; }
fail() { printf 'wk-yocto: error: %s\n' "$*" >&2; exit 1; }

trap 'printf "wk-yocto: error: line %s: \"%s\" exited %s\n" "$LINENO" "$BASH_COMMAND" "$?" >&2' ERR

[ -n "$TARGET" ] || fail "--target is required"
[ -d "$SRC" ]    || fail "no checkout at $SRC"

# The wkdev SDK's dev environment breaks a cross build: LD_LIBRARY_PATH stops bitbake's sanity checker and the include paths hand host headers to a compiler building for the target. Unset, not filtered through BB_ENV_PASSTHROUGH: `repo` sync and the native tools need them gone too.
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH OBJC_INCLUDE_PATH \
      OBJCPLUS_INCLUDE_PATH PKG_CONFIG_PATH PKG_CONFIG_LIBDIR \
      LD_LIBRARY_PATH LD_PRELOAD 2>/dev/null || true

# dotfiles/gitconfig's `feature.manyFiles` and `index.skipHash` change the format of every index git writes (version 4, null trailing checksum), and the libgit2 the toolchains pin cannot open one: cargo walks up from the rust sources it fingerprints, and rust-native 1.75.0's do_install fails with "failed to open git index". GIT_CONFIG_* wins over every config file and covers only this build.
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=index.version   GIT_CONFIG_VALUE_0=2
export GIT_CONFIG_KEY_1=index.skipHash  GIT_CONFIG_VALUE_1=false

# cargo reads whichever index is already on disk, so each repo is rewritten and pinned in its own config too: the helper re-initialises its workdir inside every action, running git in an environment this script does not govern. `--really-refresh` keeps work in progress, and its status is non-zero whenever a path differs from the index, as targets.conf does.
refresh_git_index() { # <dir> -- leave an index libgit2 can open, and keep it so
    local d
    [ -e "$1/.git" ] || return 0
    git -C "$1" config index.skipHash false >/dev/null 2>&1 || true
    git -C "$1" config index.version 2     >/dev/null 2>&1 || true
    # A killed stage can leave a 0-byte index ("index file smaller than expected", measured); rebuild it from HEAD.
    if ! git -C "$1" ls-files >/dev/null 2>&1; then
        d=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || d=""
        [ -z "$d" ] || rm -f "$d/index"
        git -C "$1" read-tree HEAD >/dev/null 2>&1 || true
        say "rebuilt an unreadable git index in $1 from HEAD"
    fi
    git -C "$1" update-index --really-refresh >/dev/null 2>&1 || true
}
refresh_git_index "$SRC"

# cross-toolchain-helper hashes the target's section of Tools/yocto/targets.conf, the local.conf that section names and its own source, and wipes WebKitBuild/CrossToolChains/<target> whole -- image, synced layers and installed SDK -- whenever that hash moves (_initialize_workdir). The webkit stage checks out the slot's commit in this lane, and between webkitglib/2.52 and a commit on main all three of those files differ (measured 2026-09-17), so without this a webkit stage takes the lane's image and SDK with it and rebuilds the nativesdk stack inside the webkit stage's budget (measured 2026-09-17: the workdir re-created at 23:33, nine minutes after the webkit stage's last output, and 13213 bitbake tasks where a WebKit compile has none). The image belongs to the image's branch and the slot is only its source: keeping the workdir is the point.
export WEBKIT_CROSS_WIPE_ON_CHANGE=0

# bitbake filters the environment, so DL_DIR/SSTATE_DIR and the GIT_CONFIG_* pin are named here or dropped: without them the cache meant to survive `wk rm` is never written, and a task that runs git unpinned writes an index the next recipe's cargo refuses ("invalid data in index"), which is librsvg's do_compile.
export BB_ENV_PASSTHROUGH_ADDITIONS="${BB_ENV_PASSTHROUGH_ADDITIONS:-} DL_DIR SSTATE_DIR GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1"

# A UTF-8 locale, or poky's sanity check stops the build; C.UTF-8 is always present on glibc.
if locale -a 2>/dev/null | grep -qix 'en_US.utf-\?8'; then
    export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
else
    export LANG=C.UTF-8 LC_ALL=C.UTF-8
fi

missing=""  # together, rather than one at a time by bitbake's sanity checker after the layer sync
for t in gawk chrpath diffstat cpio makeinfo socat file zstd \
         xz bzip2 gcc g++ perl python3 git patch which unzip wget; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
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

for d in "${DL_DIR:-}" "${SSTATE_DIR:-}"; do
    [ -n "$d" ] || fail "DL_DIR/SSTATE_DIR are not set in this workspace.
    They come from the container's store-backed cache mount
    (targets/container.sh); without them the Yocto download and sstate caches
    would land in the workspace and die with it."
    mkdir -p "$d" || fail "cannot create $d"
    [ -w "$d" ] || fail "$d is not writable"
done

# tmp/hosttools symlinks are resolved once, so an earlier run's directory keeps pointing at a stale compiler.
clear_hosttools() {
    if [ -d "$WORKDIR/build/tmp/hosttools" ]; then
        say "clearing tmp/hosttools so bitbake re-resolves the host tools"
        rm -rf "$WORKDIR/build/tmp/hosttools"
    fi
}

# sstate built with uninative on and off is not interchangeable, and target sstate paths carry no host marker to stop bitbake mixing them, corrupting `do_package`. DL_DIR stays shared.
if [ -n "$SSTATE_NS" ]; then
    SSTATE_DIR="$SSTATE_DIR/$SSTATE_NS"
    export SSTATE_DIR
    mkdir -p "$SSTATE_DIR" || fail "cannot create $SSTATE_DIR"
fi

if [ -z "$JOBS" ]; then
    JOBS=$(nproc 2>/dev/null || echo 4)
fi
BB_THREADS=$(( JOBS / 4 )); [ "$BB_THREADS" -lt 1 ] && BB_THREADS=1
PAR_MAKE=$(( JOBS / BB_THREADS ))

# Recipes worth more than their quarter of the machine, bitbake being unable to rebalance PARALLEL_MAKE mid-build. Sized by cores: the link step is bounded per recipe in meta-wk/recipes-devtools/clang.
BIG_RECIPES="clang clang-native clang-cross-arm clang-cross-aarch64
             llvm llvm-native rust-llvm rust-llvm-native
             mozjs-115 boost gdb linux-raspberrypi"

WORKDIR="$SRC/WebKitBuild/CrossToolChains/$TARGET"
CONF="$WORKDIR/build/conf/local.conf"


cd "$SRC"

# The slot's commit goes in before anything reads the checkout: the port below writes into it, the availability check and build-webkit read Tools/yocto out of it, and cross-toolchain-helper hashes it. Forced and cleaned rather than refused when dirty -- a killed webkit stage leaves the checkout mid-checkout (4819 modified and 741 untracked paths, measured 2026-09-17) and every later command refused it with "your local changes would be overwritten by checkout", so a re-run has to converge. `-fd`, never `-fdx`: WebKit's .gitignore carries /WebKitBuild/, where the image, the SDK and the slots live.
checkout_slot_commit() {
    local dirty
    git cat-file -e "$COMMIT^{commit}" 2>/dev/null \
        || git fetch --quiet "$MIRROR" "$COMMIT" \
        || fail "$COMMIT is not in this machine's mirror; 'wk ab' and 'wk pr' fetch a PR head into it first"
    dirty=$(git status --porcelain | wc -l | tr -d ' ') || dirty=0
    [ "$dirty" = 0 ] \
        || say "discarding $dirty uncommitted path(s) in $SRC -- a slot is built from a commit and nothing else"
    git checkout --force --detach --quiet "$COMMIT" \
        || fail "could not check out $COMMIT in $SRC"
    git clean -qfd || fail "could not clean $SRC after checking out $COMMIT"
    refresh_git_index "$SRC"
    say "source        $SRC @ $(git rev-parse --short HEAD) ($(git log -1 --format=%s | cut -c1-60))"
}
if [ "$STAGE" = webkit ] && [ -n "$COMMIT" ]; then
    checkout_slot_commit
fi

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

if [ -n "${PORT_TARGET_FROM:-}" ] && [ -z "${PORT_MACHINE:-}" ]; then
    fail "this profile names a target to derive [$TARGET] from but no YOC_MACHINE for
    it to select, so the derived local.conf would name the wrong machine."
fi
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

Tools/Scripts/cross-toolchain-helper --print-available-targets --log-level quiet \
    | grep -qx "$TARGET" \
    || fail "'$TARGET' is not a cross-target in this checkout.
    Available here:
$(Tools/Scripts/cross-toolchain-helper --print-available-targets --log-level quiet | sed 's/^/      /')"

init_workdir() {
    if [ -f "$WORKDIR/.target-info-version" ] && [ -f "$CONF" ]; then
        say "layers already synced at $WORKDIR"
    else
        say "syncing Yocto layers (repo sync -- this is the network-bound part)"
        Tools/Scripts/cross-toolchain-helper --cross-target="$TARGET" \
            --bitbake-dev-shell < /dev/null > /dev/null \
            || fail "the layer sync failed. It is almost always egress: 'repo'
    fetches from git.yoctoproject.org, github.com and gerrit.googlesource.com,
    and a workspace reaches all three only through the proxy allowlist
    (container/proxy/wk-proxy.py). Its log names what was refused."
        [ -f "$CONF" ] || fail "the layer sync reported success but wrote no $CONF"
    fi
    refresh_git_index "$WORKDIR"  # TMPDIR sits inside this repo, so cargo walks up to this index
}

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

        printf '\nBB_PRESSURE_MAX_MEMORY = "10000"\n\n'

        # Not PREMIRRORONLY: meta-webkit, meta-clang and meta-raspberrypi fetch from github, which the mirror lacks.
        printf 'INHERIT += "own-mirrors"\n'
        printf 'SOURCE_MIRROR_URL = "https://downloads.yoctoproject.org/mirror/sources/"\n'
        printf 'BB_GENERATE_MIRROR_TARBALLS = "1"\n\n'

        printf 'BB_DISKMON_DIRS = "\\\n'
        printf '    STOPTASKS,${TMPDIR},10G,100K \\\n'
        printf '    STOPTASKS,${DL_DIR},5G,100K \\\n'
        printf '    STOPTASKS,${SSTATE_DIR},5G,100K \\\n'
        printf '    HALT,${TMPDIR},5G,1K \\\n'
        printf '    HALT,${DL_DIR},2G,1K \\\n'
        printf '    HALT,${SSTATE_DIR},2G,1K"\n\n'

        if [ "${TAILNET:-1}" = 0 ]; then
            printf '# tailnet: off. This image joins nothing and is reachable only\n'
            printf '# over whatever LAN it lands on.\n\n'
        else
            printf 'IMAGE_INSTALL:append = " tailscale"\n\n'
        fi

        printf 'IMAGE_INSTALL:append = " wk-wifi-join"\n\n'

        printf 'IMAGE_INSTALL:append = " wk-card-priv"\n\n'

        if [ "$CHROMIUM" = 0 ]; then
            printf '# Chromium dropped: about half the build. --chromium puts it back.\n'
            printf 'IMAGE_INSTALL:remove = "chromium-ozone-wayland"\n\n'
        fi

        if [ "$RM_WORK" = 1 ]; then
            printf 'INHERIT += "rm_work"\n'
            printf 'RM_WORK_EXCLUDE += "%s"\n\n' "${IMAGE:-webkit-dev-ci-tools}"
        fi

        _board_conf="$(dirname "$0")/../image/boards/${BOARD:-}/local.conf.append"
        if [ -n "${BOARD:-}" ] && [ -f "$_board_conf" ]; then
            printf '\n# --- image/boards/%s/local.conf.append ---\n' "$BOARD"
            cat "$_board_conf"
        fi
    } >> "$CONF"
}

# WEBKIT_CROSS_TARGET/VERSION come from the file the helper wrote: absent from BB_ORIGENV, meta-webkit's distro conf falls back to the machine name and today's date, invalidating sstate.
bb() {
    local info="$WORKDIR/.target-info-version"
    [ -f "$info" ] || fail "no $info -- the layer sync did not finish"
    WEBKIT_CROSS_TARGET=$(cut -d' ' -f1 < "$info")
    WEBKIT_CROSS_VERSION=$(cut -d' ' -f2 < "$info")
    export WEBKIT_CROSS_TARGET WEBKIT_CROSS_VERSION
    ( set +u  # oe-init-build-env reads BBSERVER and others it does not define first
      cd "$WORKDIR/sources/poky" \
      && . ./oe-init-build-env "$WORKDIR/build" >/dev/null \
      && guard_run "$JOBS" -- bitbake "$@" )
}

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

# The helper's own test for an installed SDK: `.toolchain_path_configured` beside the environment-setup script its install wrote. Asked here rather than left to build-webkit, which bitbakes the whole nativesdk stack through the helper when it is missing -- inside this stage, whose budget is WEBKIT_JOBS * 2560 MB for one cross WebKit compile (yocto_stage_budget, image/yocto.sh). One such build peaked 15406 MB against a 15360 MB book and the memory watchdog killed it at bitbake task 7658 of 13213 (measured 2026-09-17), where the toolchain stage books the machine envelope and would have had 17899 MB.
require_toolchain() {
    local dir="$WORKDIR/build/toolchain" env_setup="" f
    if [ -f "$dir/.toolchain_path_configured" ]; then
        for f in "$dir"/environment-setup-*; do
            if [ -f "$f" ]; then env_setup="$f"; break; fi
        done
    fi
    [ -n "$env_setup" ] || fail "this lane has no cross toolchain installed, so cross-building WebKit here
    would bitbake the whole nativesdk stack under a budget sized for one
    WebKit compile, and the watchdog kills it hours in. Build it as its own
    stage first, which books the whole machine:

        wk sysimage build <profile> --stage toolchain

    ('wk ab' declares that step itself; this is the path a hand-run
    'wk sysimage webkit' takes.) What is looked for is
    $dir/.toolchain_path_configured and an
    environment-setup script beside it."
    say "SDK           $env_setup"
}

# The helper's build_image() treats a file at build/image/<recipe>.<ext> as proof the image is current and returns without calling bitbake, and no flag turns that off, so the directory is empty before every image stage. Moved aside rather than deleted, and moved back by the next stage that finds no image in its place: deleting it leaves a killed or wedged image stage with a lane that holds nothing, `wk sysimage holds` answering no, and the next A/B rebuilding an image the lane already had. One image stage wedged in bitbake for 22800s and was killed with the directory already gone (measured 2026-09-17).
image_copies_aside() { # <build/image dir>
    local dir="$1"
    rm -rf "$dir.previous"
    if [ -d "$dir" ]; then
        say "moving $dir aside to $dir.previous, so the helper cannot report a previous run's image as this one's"
        mv "$dir" "$dir.previous"
    fi
}

image_copies_kept() { # <build/image dir> -- the last good image, once this one is built
    rm -rf "$1.previous"
}

# Every stage, not just the image one: whichever runs next in this lane is what converges the lane back onto the image it last built.
image_copies_back() { # <build/image dir>
    local dir="$1" f
    [ -d "$dir.previous" ] || return 0
    for f in "$dir"/*; do
        [ -f "$f" ] && { rm -rf "$dir.previous"; return 0; }   # this lane built one since: the aside copy is the older
    done
    say "putting the image an earlier stage moved aside back at $dir (it built no replacement)"
    rm -rf "$dir"
    mv "$dir.previous" "$dir"
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
    be impossible: yocto-build.sh empties that directory before every image
    stage precisely so the helper cannot hand back an old copy. If this fires,
    cross-toolchain-helper changed how it decides an image is built, and
    image_copies_aside (image/yocto-build.sh) needs to change with it."
}

image_copies_back "$WORKDIR/build/image"

case "$STAGE" in
    layers)
        init_workdir
        configure_local_conf
        configure_bblayers
        say "layers ready at $WORKDIR"
        ;;
    fetch)
        # `-k`: bitbake otherwise halts on the first unreachable host, one run per host of the allowlist.
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
        image_copies_aside "$WORKDIR/build/image"
        image_stage_start=$(date +%s)
        run_helper "bitbake ${IMAGE:-the image} (rootfs + kernel + wic)" --build-image
        verify_image_freshness "$WORKDIR/build/image" "$image_stage_start"
        image_copies_kept "$WORKDIR/build/image"
        ;;
    toolchain)
        init_workdir
        configure_local_conf
        configure_bblayers
        clear_hosttools
        run_helper "bitbake populate_sdk (the cross toolchain)" --build-toolchain
        ;;
    webkit)
        init_workdir
        configure_local_conf
        configure_bblayers
        clear_hosttools
        require_toolchain
        say "cross-building WebKit (WPE, Release) for $TARGET"
        # wpe-2.46 defaults ENABLE_WPE_1_1_API and ENABLE_WPE_PLATFORM both on and CMake refuses the pair, while run-benchmark's WPE driver hardcodes --use-wpe-platform-api. build-webkit takes only one --cmakeargs and the last wins, so the target's own (bwrap, xdg-dbus-proxy) are read out of targets.conf and ours appended.
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
        extra="$extra${CROSS_CMAKE:+ $CROSS_CMAKE}"   # what the cross config adds (build/configs.sh), after the branch's own so it wins
        say "  config:       ${CROSS_CONFIG:-wpe-cross}"
        say "  target flags: ${tgt_args:-none}"
        say "  cmakeargs:    $tgt_cmake $extra"
        if [ -n "$CROSS_CC" ]; then
            export CC="$CROSS_CC" CXX="$CROSS_CXX"   # the SDK's environment-setup exports its clang only when CC/CXX name one
            say "  compiler:     $CC / $CXX"
        fi

        # build-webkit's own `-j$(numberOfCPUs)` OOMs on WebCore's unified sources, and bitbake never sees this stage, so its PARALLEL_MAKE cap does not reach it.
        webkit_makeargs="-j${WEBKIT_JOBS:-8}"
        say "  jobs:         ${WEBKIT_JOBS:-8} (memory-sized; the default -j$(nproc) OOMs on unified sources)"

        # shellcheck disable=SC2086
        WK_MB_PER_JOB=2560 guard_run "${WEBKIT_JOBS:-8}" -- \
            Tools/Scripts/build-webkit --wpe --release --cross-target="$TARGET" \
            $tgt_args --makeargs="$webkit_makeargs" --cmakeargs="$tgt_cmake $extra" \
            || fail "the cross build of WebKit failed"

        if [ -n "$SLOT" ]; then  # bin/ and lib/ of the cross build, beside the image, with a manifest
            b="$SRC/WebKitBuild/WPE/Release_$TARGET"
            slotdir="$SRC/WebKitBuild/wk-slots/$SLOT"
            [ -x "$b/bin/MiniBrowser" ] || fail "the cross build left no $b/bin/MiniBrowser"
            rm -rf "$slotdir/root"; mkdir -p "$slotdir/root"
            cp -a "$b/bin" "$b/lib" "$slotdir/root/" || fail "could not copy the build into $slotdir"
            python3 /opt/wk-tools/lib/wkslot.py manifest "$slotdir/root" "$slotdir/slot.json" \
                slot="$SLOT" profile="$PROFILE" commit="$COMMIT" target="$TARGET" \
                build_config="${CROSS_CONFIG:-wpe-cross}" \
                browser=minibrowser lib_dir=lib exec_dir=bin bundle_dir=lib \
                jobs="${WEBKIT_JOBS:-8}" built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
                wk_tools="$(git -C /opt/wk-tools rev-parse --short HEAD 2>/dev/null || echo unknown)" \
                || fail "could not describe the build as a slot (above); a cross build with no
    build-id note cannot be told apart on the board"
            say "slot ready: $slotdir ($(du -sh "$slotdir/root" | cut -f1), build-id $(python3 /opt/wk-tools/lib/wkslot.py get "$slotdir/slot.json" build_id))"
        fi
        ;;
    pgo-mix)   # in the cross environment and nowhere else: a .profraw is readable only by the toolchain that wrote it, and that clang is the SDK's rather than this container's
        [ -n "$PGO_DIR" ] && [ -n "$PGO_LIB" ] \
            || fail "the pgo-mix stage needs --pgo-dir and --pgo-lib"
        [ -d "$PGO_DIR" ] || fail "no collection at $PGO_DIR; 'wk pi bench --pgo' puts one there"
        init_workdir
        say "mixing $PGO_LIB profiles from $PGO_DIR at WebKit's own weights"
        run_helper "mix the collected profiles" --cross-toolchain-run-cmd \
            python3 /opt/wk-tools/lib/wkpgo.py mix \
                --scripts "$SRC/Tools/Scripts" --dir "$PGO_DIR" --lib "$PGO_LIB"
        run_helper "read the mixed profile back" --cross-toolchain-run-cmd \
            python3 /opt/wk-tools/lib/wkpgo.py check \
                --scripts "$SRC/Tools/Scripts" --dir "$PGO_DIR" --lib "$PGO_LIB" \
                --json "$PGO_DIR/profile-check.json"
        ;;
    *)  fail "unknown stage '$STAGE' (layers, fetch, image, toolchain, webkit, pgo-mix)" ;;
esac

say "stage '$STAGE' done"
