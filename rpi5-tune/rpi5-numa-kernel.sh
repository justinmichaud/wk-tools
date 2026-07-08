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

  # Removable storage + USB — PIN EXPLICITLY. localmodconfig (LEAN=yes) disables
  # any module not loaded AT BUILD TIME, so anything not plugged in when you build
  # gets silently dropped. On this box that killed USB_STORAGE/USB_UAS (no USB disk
  # attached) and MMC_BLOCK (empty microSD slot) — leaving the numa kernel unable
  # to see USB drives OR create /dev/mmcblk* for the SD reader. Core USB (USB,
  # USB_XHCI_HCD, USB_DWC2, USB_HID) usually survives via the keyboard/mouse being
  # present, but pin it too so a headless build can't strip it.
  #   USB core stays =y (built-in, matches stock); leaf drivers are =m (modular),
  #   which is fine since root is on NVMe so none are needed at early boot.
  #   olddefconfig (below) resolves deps — SCSI/BLK_DEV_SD/MMC/SDHCI are already =y.
  scripts/config \
    --enable USB_SUPPORT --enable USB --enable USB_XHCI_HCD --enable USB_DWC2 \
    --module USB_HID --module HID_GENERIC \
    --module USB_STORAGE --module USB_UAS \
    --module MMC_BLOCK --module MMC_SDHCI_BRCMSTB --module MMC_SDHCI_OF_DWCMSHC \
    --module EXFAT_FS --module NTFS3_FS --module NLS_ISO8859_1
  ok "storage/USB pinned (USB_STORAGE, USB_UAS, MMC_BLOCK, exFAT/NTFS) — survives LEAN"

  # Localversion so the kernel is clearly ours and won't clash with the stock
  # 7.0.0-10xx-raspi package (A/B boot keeps the stock one as fallback).
  # CRITICAL: the name MUST be <version>-<ABInum>-<flavour>, i.e. include a
  # NUMERIC ABI field ("-1-numa", not "-numa"). Ubuntu boots via flash-kernel's
  # pi-try method, whose latest-kernel selection is:
  #   linux-version list | include_only_flavors raspi raspi-realtime numa | linux-version sort | tail -1
  # include_only_flavors only recognises the Debian/Ubuntu VERSION-ABINUM-FLAVOUR
  # form. A bare "-numa" (no numeric ABI) is silently dropped by that filter, so
  # flash-kernel decides the stock kernel is "latest", prints "Ignoring old or
  # unknown version <kver>", exit 0 — and NEVER stages our kernel into
  # /boot/firmware/new/. Result: reboots keep landing on stock and numa=fake is
  # ignored. The "-1-" makes the name parseable and (7.0.x release > stock's
  # 7.0.0) rank newest, so flash-kernel stages + tryboots it.
  scripts/config --set-str LOCALVERSION "-1-numa" --disable LOCALVERSION_AUTO
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

# Removable storage / USB must survive localmodconfig — a kernel that can't see
# USB drives or the SD reader is a bad build. Assert the essentials are set (as
# =y or =m) before the long compile. olddefconfig can silently drop any whose
# dependency it couldn't satisfy, so check the *resolved* .config, not our intent.
MISSING=""
for sym in USB USB_XHCI_HCD USB_STORAGE USB_UAS MMC_BLOCK MMC_SDHCI_BRCMSTB; do
  grep -qE "^CONFIG_$sym=[ym]$" .config || MISSING="$MISSING $sym"
done
if [ -n "$MISSING" ]; then
  echo "   storage/USB state in resolved .config:"
  grep -E '^(CONFIG_USB=|CONFIG_USB_STORAGE|CONFIG_USB_UAS|CONFIG_MMC_BLOCK|CONFIG_MMC_SDHCI_BRCMSTB)' .config | sed 's/^/     /'
  die "these storage/USB symbols did not stick:$MISSING
     A build without them can't mount USB drives or the SD card reader. They are
     pinned in step 4; if one won't set, its dependency (SCSI/MMC/SDHCI) may have
     been trimmed by localmodconfig — enable that dep too, or build with LEAN=no."
fi
grep -E '^CONFIG_(USB=|USB_STORAGE|USB_UAS|MMC_BLOCK|MMC_SDHCI_BRCMSTB|EXFAT_FS|NTFS3_FS)' .config | sed 's/^/   /'
ok "USB mass storage + SD card reader (MMC_BLOCK) + exFAT/NTFS confirmed"
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

  # Derive the kernel version (uname -r style) from the deb name:
  #   linux-image-7.0.6-numa_7.0.6-1_arm64.deb -> 7.0.6-numa
  KVER="$(basename "$IMG_DEB")"; KVER="${KVER#linux-image-}"; KVER="${KVER%%_*}"
  [ -f "/boot/vmlinuz-$KVER" ] || die "dpkg installed but /boot/vmlinuz-$KVER is missing"

  # Pi 5 firmware GATEKEEPER: os_check. By default (os_check=1) the Pi 5 bootloader
  # refuses to boot a kernel it can't confirm is Pi5-compatible ("...OS does not
  # support..."). Ubuntu's official raspi images carry a trailer that satisfies it;
  # a locally-built bindeb-pkg kernel does NOT. So under the pi-try scheme the
  # firmware rejects our kernel during the one-shot tryboot, piboot marks new/ as
  # 'bad', and it falls back to stock — the kernel never boots. Disable os_check so
  # the firmware will load our kernel. (Idempotent; inserted globally, before the
  # first section, so it applies to both the normal and [tryboot] paths.)
  CFG=/boot/firmware/config.txt
  if [ -f "$CFG" ]; then
    if grep -qE '^\s*os_check\s*=\s*0' "$CFG"; then
      skip "os_check=0 already set in $CFG"
    elif grep -qE '^\s*os_check\s*=' "$CFG"; then
      sudo sed -i -E 's/^\s*os_check\s*=.*/os_check=0/' "$CFG"
      ok "set os_check=0 in $CFG (was non-zero — Pi5 firmware would reject the custom kernel)"
    else
      sudo sed -i '1i os_check=0' "$CFG"
      ok "added os_check=0 to $CFG (Pi5 firmware would otherwise reject the custom kernel)"
    fi
  else
    skip "no $CFG found — if boot fails with an 'OS does not support' error, set os_check=0 there"
  fi

  # THE STEP THAT ACTUALLY MAKES IT BOOT. On RPi Ubuntu the firmware boots the
  # image under /boot/firmware/ (os_prefix, e.g. current/vmlinuz) — NOT
  # /boot/vmlinuz-*. dpkg's zz-flash-kernel hook is supposed to promote it there,
  # but it is unreliable for a bindeb-pkg kernel and can be clobbered by a later
  # flash-kernel run against the stock kernel. So promote it EXPLICITLY, by
  # version, rather than trusting the hook.
  if command -v flash-kernel >/dev/null; then
    log "7b Promote $KVER into /boot/firmware (flash-kernel)"
    # flash-kernel's pi-try method only STAGES the kernel it considers "latest"
    # among the accepted flavours; a version it can't rank it dismisses with
    # "Ignoring old or unknown version ... (latest is ...)" AND STILL EXITS 0.
    # So capture the output and treat that message as a hard failure — otherwise
    # the numa kernel is never staged and the reboot lands back on stock.
    FK_OUT="$(sudo flash-kernel "$KVER" 2>&1)"; printf '%s\n' "$FK_OUT" | sed 's/^/   /'
    if printf '%s' "$FK_OUT" | grep -q 'Ignoring old or unknown version'; then
      die "flash-kernel refused to stage $KVER (see 'Ignoring old or unknown version' above).
     Its flavour filter (include_only_flavors) needs a numeric ABI in the name
     — build with LOCALVERSION='-1-numa' (not '-numa'). Check:
        linux-version list | . /usr/share/flash-kernel/functions; include_only_flavors raspi raspi-realtime numa"
    fi
  else
    skip "flash-kernel not installed — cannot promote to /boot/firmware automatically"
  fi

  # DEVICE TREE FIX (pi-try + bindeb-pkg). flash-kernel's pi-try method copies the
  # board DTB + overlays into new/ by searching /usr/lib/firmware/$KVER/device-tree/
  # — where Ubuntu's PACKAGED kernels put them. A bindeb-pkg kernel instead ships
  # them at /usr/lib/linux-image-$KVER/{broadcom/*.dtb,overlays/*.dtbo}, so the
  # search finds nothing (watch for "find: ... device-tree: No such file" in 7b) and
  # new/ ends up with NO DTB and an EMPTY overlays/. The Pi5 firmware cannot boot
  # without bcm2712-rpi-5-b.dtb from the os_prefix dir → the tryboot is marked 'bad'
  # and falls back to stock, WITHOUT the kernel ever executing (nothing in the
  # journal). So copy the kernel's own device trees into new/ ourselves.
  NEWDIR=/boot/firmware/new
  if [ -d "$NEWDIR" ]; then
    log "7b+ Stage device tree(s) + overlays into $NEWDIR (flash-kernel's pi-try misses them)"
    DTB_SRC="/usr/lib/linux-image-$KVER/broadcom"; [ -d "$DTB_SRC" ] || DTB_SRC="/boot/dtbs/$KVER"
    OVL_SRC="/usr/lib/linux-image-$KVER/overlays"
    if ls "$DTB_SRC"/*.dtb >/dev/null 2>&1; then
      sudo cp "$DTB_SRC"/*.dtb "$NEWDIR"/ && ok "copied $(ls "$DTB_SRC"/*.dtb | wc -l) board DTB(s) from $DTB_SRC"
    else
      die "no board DTB found for $KVER (looked in $DTB_SRC) — cannot make new/ bootable"
    fi
    if [ -d "$OVL_SRC" ]; then
      sudo mkdir -p "$NEWDIR"/overlays
      sudo cp "$OVL_SRC"/*.dtbo "$NEWDIR"/overlays/ 2>/dev/null
      # bring the overlay_map/README too if present (config.txt overlay resolution)
      sudo cp "$OVL_SRC"/overlay_map.dtb "$OVL_SRC"/README "$NEWDIR"/overlays/ 2>/dev/null || true
      ok "copied $(ls "$NEWDIR"/overlays/*.dtbo 2>/dev/null | wc -l) overlays into new/overlays"
    fi
    # The board DTB MUST now be present in new/ or the firmware won't boot it.
    DTB_ID="bcm2712-rpi-5-b.dtb"
    sudo test -e "$NEWDIR/$DTB_ID" || die "$NEWDIR/$DTB_ID still missing after copy — tryboot would fail at the firmware"
    ok "$NEWDIR/$DTB_ID present — firmware has a device tree to boot"
  fi

  # HARD-VERIFY the numa image actually reached the firmware boot dir. Compare by
  # content: find any vmlinuz under /boot/firmware whose md5 matches our image.
  # Without this a silent promotion failure sails straight through to DONE and you
  # reboot back into the stock kernel (numa=fake is then silently ignored).
  log "7c Verify the firmware boot image IS the numa kernel"
  NUMA_MD5="$(md5sum "/boot/vmlinuz-$KVER" | awk '{print $1}')"
  FW_MATCH="$(find /boot/firmware -maxdepth 2 -type f -name 'vmlinuz*' 2>/dev/null | while read -r f; do
    [ "$(md5sum "$f" 2>/dev/null | awk '{print $1}')" = "$NUMA_MD5" ] && echo "$f"
  done)"
  if [ -n "$FW_MATCH" ]; then
    printf '%s\n' "$FW_MATCH" | sed 's/^/   /'
    ok "firmware boot image matches vmlinuz-$KVER — reboot will land on the numa kernel"
  else
    echo "   /boot/firmware vmlinuz images and their md5s:"
    find /boot/firmware -maxdepth 2 -type f -name 'vmlinuz*' -exec md5sum {} \; 2>/dev/null | sed 's/^/     /'
    echo "   expected (vmlinuz-$KVER): $NUMA_MD5"
    die "no /boot/firmware vmlinuz matches the numa kernel — promotion did NOT take.
     A reboot now would boot the STOCK kernel and silently ignore numa=fake.
     Try: sudo flash-kernel $KVER   (then re-run this script; build is cached).
     26.04 A/B boot means fixing this is safe — a bad kernel auto-reverts."
  fi
  echo
  echo "   26.04 A/B boot will auto-revert to the stock kernel if this one fails to boot."
else
  echo
  echo "   Skipping install. To install later:"
  echo "     sudo dpkg -i $IMG_DEB${HDR_DEB:+ $HDR_DEB}"
  echo "     sudo flash-kernel <version>   # e.g. 7.0.6-numa — promote into /boot/firmware, else it won't boot"
fi

#-------------------------------------------------------------------------------
log "DONE"
cat <<EOF
   Next:
     1) (if not done above) sudo dpkg -i $KBUILD_DIR/linux-image-*-numa_*.deb
        then promote it:            sudo flash-kernel <version>   # e.g. 7.0.6-numa
     2) reboot (step 7c above confirmed the firmware image is the numa kernel):
          sudo reboot
     3) after reboot, confirm the new kernel + flag:
          uname -r                                   # ...-numa
          grep CONFIG_NUMA_EMU /boot/config-\$(uname -r)   # =y
     4) enable emulation and reboot again:
          NUMA_FAKE=auto bash "$(dirname "$0")/rpi5-setup.sh"   # let firmware pick N (16GB Pi5 -> 8); do NOT hardcode 4
          sudo reboot
     5) verify NUMA is live:
          dmesg | grep -i numa      # expect interleave policy 'interleave:0-7'
          numactl --hardware        # expect 8 nodes (install: sudo apt install numactl)
   Benchmark with:  numactl --interleave=all <workload>   (A/B a forced split via NUMA_FAKE=4 only to measure)
EOF
