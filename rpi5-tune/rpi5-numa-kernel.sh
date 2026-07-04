#!/usr/bin/env bash
#===============================================================================
# rpi5-numa-kernel.sh — Build a linux-raspi kernel with CONFIG_NUMA_EMU=y so
#   that `numa=fake=N` works on Ubuntu 26.04 "Resolute Raccoon" (kernel 7.0).
#   This is "Path B" from rpi5-numa-README.md, made turnkey.
#
# WHY: stock 26.04 ships all NUMA plumbing enabled EXCEPT the emulation switch
#   (# CONFIG_NUMA_EMU is not set). It is a config-only change — the code is
#   already upstream (Igalia's arm64 NUMA work, mainline since 6.12).
#
# RUN AS YOUR NORMAL USER (must have sudo). Do NOT prefix with sudo:
#     bash rpi5-numa-kernel.sh
#   Steps that need root call sudo internally (you'll be prompted once).
#   Takes ~1-2h to compile on the Pi. IDEMPOTENT + RESUMABLE: safe to re-run;
#   it skips phases already completed (source fetch, build) and re-uses them.
#
# SAFETY: this only BUILDS + (optionally) installs .debs. 26.04's A/B boot
#   auto-reverts a kernel that fails to boot, so a bad build won't brick you.
#   The compile ABORTS before the long step if CONFIG_NUMA_EMU didn't stick,
#   so you never burn 1-2h on a mis-configured tree.
#
# Tunables (override via env):
KBUILD_DIR="${KBUILD_DIR:-$HOME/kbuild}"
JOBS="${JOBS:-$(nproc)}"
DO_INSTALL="${DO_INSTALL:-ask}"     # ask | yes | no  -> dpkg -i the built .debs
#===============================================================================
set -euo pipefail
log(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok(){  printf '   \033[32m✓\033[0m %s\n' "$*"; }
skip(){ printf '   \033[33m-\033[0m %s\n' "$*"; }
die(){ printf '\n\033[1;31mABORT:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run as your normal user (not root/sudo). sudo is used internally."
command -v sudo >/dev/null || die "sudo is required."

# Confirm we can sudo non-interactively (cached creds or a NOPASSWD rule). If we
# only have interactive sudo, prime the timestamp now and keep it warm so the
# long build doesn't stall on a password prompt halfway through.
if ! sudo -n true 2>/dev/null; then
  sudo -v || die "sudo authentication failed."
  ( while true; do sudo -n true 2>/dev/null; sleep 50; done ) &
  SUDO_KEEPALIVE=$!
  trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true' EXIT
fi

#-------------------------------------------------------------------------------
log "1  Enable deb-src (needed for 'apt-get source linux-raspi')"
# 26.04 uses the deb822 format in /etc/apt/sources.list.d/ubuntu.sources with
# only 'Types: deb'. Derive a parallel deb-src file from it (idempotent).
SRC=/etc/apt/sources.list.d/ubuntu.sources
DSRC=/etc/apt/sources.list.d/ubuntu-src.sources
if [ -f "$SRC" ]; then
  if [ ! -f "$DSRC" ] || ! grep -q 'deb-src' "$DSRC" 2>/dev/null; then
    sudo sed 's/^Types: deb$/Types: deb-src/' "$SRC" | sudo tee "$DSRC" >/dev/null
    ok "wrote $DSRC (deb-src for the same suites)"
  else skip "deb-src already present ($DSRC)"; fi
else
  # Fallback for classic sources.list layout.
  if ! grep -rqsE '^deb-src ' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
    . /etc/os-release
    printf 'deb-src http://archive.ubuntu.com/ubuntu %s main\ndeb-src http://archive.ubuntu.com/ubuntu %s-updates main\n' \
      "$VERSION_CODENAME" "$VERSION_CODENAME" | sudo tee /etc/apt/sources.list.d/deb-src.list >/dev/null
    ok "wrote classic deb-src list"
  else skip "deb-src already present"; fi
fi
sudo apt-get update -qq || die "apt-get update failed"

#-------------------------------------------------------------------------------
log "2  Install build toolchain + kernel build-deps"
sudo apt-get install -y build-essential fakeroot dpkg-dev debhelper rsync kmod cpio bc \
  flex bison libssl-dev libncurses-dev libelf-dev libdw-dev dwarves zstd python3 \
  || die "toolchain install failed"
# build-dep pulls anything else the raspi kernel needs (best-effort; the
# explicit list above already covers the essentials).
sudo apt-get build-dep -y linux-raspi >/dev/null 2>&1 && ok "build-dep linux-raspi satisfied" \
  || skip "build-dep linux-raspi unavailable (explicit deps installed above — continuing)"

#-------------------------------------------------------------------------------
log "3  Fetch linux-raspi kernel source"
mkdir -p "$KBUILD_DIR"
cd "$KBUILD_DIR"
# The extracted kernel tree is named linux-raspi-<upstream> (e.g. linux-raspi-7.0.0),
# NOT linux-meta-raspi-* — match only the real tree.
find_srcdir(){ find "$KBUILD_DIR" -maxdepth 1 -type d -name 'linux-raspi-*' 2>/dev/null | sort -V | tail -1; }
SRCDIR="$(find_srcdir)"
if [ -z "$SRCDIR" ]; then
  # GOTCHA: the *binary* pkg 'linux-raspi' is a metapackage whose source is
  # 'linux-meta-raspi' (~14kB, no kernel). The real kernel tree is source
  # 'linux-raspi', produced by the versioned image binary linux-image-<abi>-raspi.
  # Fetch source via that concrete binary name so apt resolves the real tree.
  IMG_PKG="$(apt-cache pkgnames linux-image-7 2>/dev/null | grep -E '^linux-image-[0-9].*-raspi$' | grep -v realtime | sort -V | tail -1)"
  [ -n "$IMG_PKG" ] || IMG_PKG="linux-image-$(uname -r)"
  ok "resolving kernel source via binary pkg: $IMG_PKG"
  apt-get source "$IMG_PKG" || die "apt-get source $IMG_PKG failed (is deb-src active for main?)"
  SRCDIR="$(find_srcdir)"
  [ -n "$SRCDIR" ] || die "kernel source tree (linux-raspi-*) not found after apt-get source"
  ok "fetched source -> $SRCDIR"
else
  skip "source already present -> $SRCDIR (delete it to re-fetch)"
fi
cd "$SRCDIR"

# Ubuntu ships symlinks/permissions that the .orig tarball can't carry; they're
# recreated by debian.<flavour>/reconstruct during Ubuntu's own build prep, which
# a bare `make bindeb-pkg` never runs. Most important: arch/arm64/.../overlays is
# meant to be a symlink to the arm/ overlays tree — without it `make dtbs` dies on
# a missing overlays/Makefile. Run reconstruct (chmod noise about missing debian/*
# is harmless) then force the overlays symlink (reconstruct's own `ln -sf` misfires
# when the empty dir already exists, nesting the link inside it).
for rc in debian.raspi/reconstruct debian.master/reconstruct; do
  [ -f "$rc" ] && sh "$rc" >/dev/null 2>&1 || true
done
OVL=arch/arm64/boot/dts/overlays
if [ ! -L "$OVL" ] && [ -d arch/arm/boot/dts/overlays ]; then
  rm -rf "$OVL"; ln -sf ../../../arm/boot/dts/overlays "$OVL"
fi
[ -f "$OVL/Makefile" ] && ok "dts overlays symlink OK" || skip "overlays Makefile still missing (dtbs may fail)"

#-------------------------------------------------------------------------------
log "4  Configure: base on running config; trim modules (lean) + NUMA + max-perf"
# Base on the currently-running kernel's config so we inherit Ubuntu's raspi
# choices, then flip on the emulation switch. olddefconfig fills any new symbols.
LEAN="${LEAN:-yes}"        # yes = localmodconfig: build only currently-loaded modules
MAXPERF="${MAXPERF:-yes}"  # yes = throughput-oriented tuning (see below)
if [ ! -f .config ] || [ "${RECONFIG:-}" = 1 ]; then
  # Purge objects from any prior (full) build so we don't ship stale modules.
  make clean >/dev/null 2>&1 || true
  cp "/boot/config-$(uname -r)" .config

  if [ "$LEAN" = yes ]; then
    # localmodconfig disables every module NOT currently loaded (lsmod). Cuts the
    # module count from thousands to ~100-200 -> build drops from hours to minutes.
    # Boots on THIS hardware; drivers for devices not plugged in at build time are
    # omitted (rebuild, or grab them from the stock kernel, if you add hardware).
    # Feed defaults to the oldconfig prompts localmodconfig fires for NEW symbols.
    # Use process substitution, not `yes | make`: under `set -o pipefail`, yes's
    # harmless SIGPIPE (141) when make stops reading would false-fail the pipeline.
    make LSMOD=/proc/modules localmodconfig < <(yes '') || die "localmodconfig failed"
    ok "localmodconfig: trimmed to $(grep -c '=m' .config) modules"
  fi

  # The whole point: NUMA emulation.
  scripts/config --enable NUMA --enable NUMA_MEMBLKS --enable NUMA_EMU

  if [ "$MAXPERF" = yes ]; then
    # Throughput/benchmark tuning (trade some interactivity/latency for raw speed):
    # PREEMPTION: do NOT force PREEMPT_NONE on arm64. This kernel has
    # ARCH_HAS_PREEMPT_LAZY=y, which makes PREEMPT_NONE (depends on ARCH_NO_PREEMPT)
    # and PREEMPT_VOLUNTARY (depends on !ARCH_HAS_PREEMPT_LAZY) UNBUILDABLE. The only
    # throughput-oriented model available is PREEMPT_LAZY (the modern NONE-successor),
    # which is already the default; forcing the choice lands on PREEMPT (full) = WORSE
    # throughput. So leave the preemption choice at its default (lazy).
    scripts/config --disable HZ_1000 --disable HZ_300 --disable HZ_250 --enable HZ_100 --set-val HZ 100  # fewer timer ticks
    scripts/config --enable NO_HZ_IDLE                                                    # tickless idle
    scripts/config --enable  CPU_FREQ_DEFAULT_GOV_PERFORMANCE                             # governor default = performance
    scripts/config --disable CPU_FREQ_DEFAULT_GOV_SCHEDUTIL --disable CPU_FREQ_DEFAULT_GOV_ONDEMAND --disable CPU_FREQ_DEFAULT_GOV_POWERSAVE
    scripts/config --enable  CC_OPTIMIZE_FOR_PERFORMANCE --disable CC_OPTIMIZE_FOR_SIZE   # -O2 for speed, not size
    # sched_ext (SCX): Meta-led BPF-pluggable scheduler class — load custom schedulers
    # (scx_lavd/scx_bpfland for interactivity, scx_rusty/scx_layered for throughput) at
    # runtime. REQUIRES kernel BTF, so keep DEBUG_INFO_BTF ON. (Do NOT set
    # DEBUG_INFO_NONE — it strips BTF and breaks sched_ext, bpftrace, and BPF CO-RE.)
    # BTF needs the debug-info CHOICE off NONE (scripts/config can't drive a choice
    # by --enable alone), so disable NONE + pick the DWARF default first.
    scripts/config --disable DEBUG_INFO_NONE --enable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
                   --enable DEBUG_INFO --enable DEBUG_INFO_BTF \
                   --enable BPF_SYSCALL --enable SCHED_CLASS_EXT
    # Strip pure runtime debug/instrumentation overhead (safe; does not affect BTF):
    scripts/config --disable DEBUG_PREEMPT --disable SCHEDSTATS --disable LATENCYTOP
    scripts/config --disable PROVE_LOCKING --disable DEBUG_ATOMIC_SLEEP
    ok "max-perf knobs applied (HZ=100, gov=performance, -O2, sched_ext+BTF kept, lazy preempt)"
  fi

  # Localversion so the kernel is clearly ours and won't clash with the stock
  # 7.0.0-10xx-raspi package (A/B boot keeps the stock one as fallback).
  scripts/config --set-str LOCALVERSION "-numa" --disable LOCALVERSION_AUTO
  # Out-of-tree module signing / debian cert paths break a bare `make`.
  scripts/config --disable SYSTEM_TRUSTED_KEYS   --disable SYSTEM_REVOCATION_KEYS 2>/dev/null || true
  scripts/config --set-str SYSTEM_TRUSTED_KEYS "" --set-str SYSTEM_REVOCATION_KEYS "" 2>/dev/null || true
  make olddefconfig
  ok "config prepared"
else
  skip ".config already exists (RECONFIG=1 to rebuild it)"
fi

#-------------------------------------------------------------------------------
log "5  PRE-FLIGHT: verify CONFIG_NUMA_EMU=y before the long build"
if ! grep -q '^CONFIG_NUMA_EMU=y' .config; then
  echo "   current state:"; grep -E 'CONFIG_NUMA(_EMU|_MEMBLKS)?=' .config || true
  die "CONFIG_NUMA_EMU did not stick — refusing to spend 1-2h on a bad build.
     Investigate: does this arch/config allow NUMA_EMU? (needs NUMA=y, which is set.)"
fi
grep -E '^CONFIG_NUMA(_EMU|_MEMBLKS)?=y' .config | sed 's/^/   /'
ok "CONFIG_NUMA_EMU=y confirmed"
if [ "$MAXPERF" = yes ]; then
  echo "   max-perf knobs (as resolved by olddefconfig):"
  grep -E '^CONFIG_(PREEMPT_LAZY|PREEMPT_DYNAMIC|HZ|NO_HZ_IDLE|CPU_FREQ_DEFAULT_GOV_PERFORMANCE|CC_OPTIMIZE_FOR_PERFORMANCE|SCHED_CLASS_EXT|DEBUG_INFO_BTF)=' .config | sed 's/^/     /'
  if grep -q '^CONFIG_SCHED_CLASS_EXT=y' .config && grep -q '^CONFIG_DEBUG_INFO_BTF=y' .config; then
    ok "sched_ext + BTF enabled (Meta SCX schedulers usable)"
  else
    die "sched_ext/BTF did not stick — refusing to build. SCHED_CLASS_EXT needs
     DEBUG_INFO_BTF, which needs the debug-info choice off NONE. (Base config may
     have DEBUG_INFO_NONE=y; the script disables it + selects DWARF — check above.)"
  fi
fi

#-------------------------------------------------------------------------------
log "6  Build .deb packages (~1-2h on the Pi; JOBS=$JOBS)"
# A reconfig invalidates any previously-built debs — clear them so we don't skip
# the compile and ship a kernel that doesn't match the new .config.
if [ "${RECONFIG:-}" = 1 ]; then
  rm -f "$KBUILD_DIR"/linux-{image,headers}-*-numa_*.deb 2>/dev/null || true
fi
if ls "$KBUILD_DIR"/linux-image-*-numa_*.deb >/dev/null 2>&1; then
  skip "built .debs already exist in $KBUILD_DIR — skipping compile (rm them to rebuild)"
else
  # bindeb-pkg produces installable linux-image + linux-headers debs in the
  # parent dir. LOCALVERSION=-numa keeps them side-by-side with the stock kernel.
  # NOTE: scripts/Makefile.dtbinst uses `install -D`, which races under -j (two
  # jobs mkdir the same overlays/ dir → "cannot create directory"). If the
  # parallel run trips on that, retry the packaging serially (compile is cached,
  # so the retry is quick and race-free).
  if ! make -j"$JOBS" bindeb-pkg 2>&1 | tee "$KBUILD_DIR/build.log"; then
    echo "   (parallel packaging failed — retrying dtbs_install/packaging with -j1)"
    make -j1 bindeb-pkg 2>&1 | tee -a "$KBUILD_DIR/build.log" || die "kernel build failed (see $KBUILD_DIR/build.log)"
  fi
  ok "build complete"
fi
ls -1 "$KBUILD_DIR"/linux-{image,headers}-*.deb 2>/dev/null | sed 's/^/   /' || true

#-------------------------------------------------------------------------------
log "7  Install"
IMG_DEB="$(ls -t "$KBUILD_DIR"/linux-image-*-numa_*.deb 2>/dev/null | head -1)"
HDR_DEB="$(ls -t "$KBUILD_DIR"/linux-headers-*-numa_*.deb 2>/dev/null | head -1)"
[ -n "$IMG_DEB" ] || die "no built linux-image .deb found to install"
case "$DO_INSTALL" in
  ask) printf '   Install now with dpkg -i? [y/N] '; read -r a; [ "$a" = y ] || [ "$a" = Y ] && DO_INSTALL=yes || DO_INSTALL=no ;;
esac
if [ "$DO_INSTALL" = yes ]; then
  sudo dpkg -i "$IMG_DEB" ${HDR_DEB:+"$HDR_DEB"} || die "dpkg -i failed"
  ok "installed: $(basename "$IMG_DEB")"
  echo
  echo "   IMPORTANT — before reboot, confirm the raspi boot integration picked it up:"
  echo "     ls -l /boot/vmlinuz-*-numa           # kernel image present"
  echo "     ls /boot/firmware/                    # Ubuntu raspi flash-kernel copies here"
  echo "     grep -R numa /boot/firmware/*.txt 2>/dev/null"
  echo "   If /boot/firmware wasn't updated automatically, run: sudo flash-kernel"
  echo "   26.04 A/B boot will auto-revert to the stock kernel if this one fails to boot."
else
  echo
  echo "   Skipping install. To install later:"
  echo "     sudo dpkg -i $IMG_DEB${HDR_DEB:+ $HDR_DEB}"
fi

#-------------------------------------------------------------------------------
log "DONE"
cat <<EOF
   Next:
     1) (if not done above) sudo dpkg -i $KBUILD_DIR/linux-image-*-numa_*.deb
     2) verify raspi boot integration (see notes above), then: sudo reboot
     3) after reboot, confirm the new kernel + flag:
          uname -r                                   # ...-numa
          grep CONFIG_NUMA_EMU /boot/config-\$(uname -r)   # =y
     4) enable emulation and reboot again:
          NUMA_FAKE=4 bash "$(dirname "$0")/rpi5-setup.sh"
          sudo reboot
     5) verify NUMA is live:
          dmesg | grep -i numa      # expect interleave policy 'interleave:0-3'
          numactl --hardware        # expect 4 nodes (install: sudo apt install numactl)
   Benchmark numa=fake=4 vs =8 with:  numactl --interleave=all <workload>
EOF
