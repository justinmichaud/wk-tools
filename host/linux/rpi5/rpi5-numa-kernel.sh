#!/usr/bin/env bash
# Build a linux-raspi kernel with CONFIG_NUMA_EMU=y so numa=fake=N works on
# Ubuntu 26.04, which ships every NUMA piece but that one switch. ~1-2h to
# compile, resumable; 26.04's A/B boot auto-reverts a kernel that fails to boot.
KBUILD_DIR="${KBUILD_DIR:-$HOME/kbuild}"
JOBS="${JOBS:-$(nproc)}"
DO_INSTALL="${DO_INSTALL:-ask}"     # ask | yes | no  -> dpkg -i the built .debs
set -euo pipefail
log(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok(){  printf '   \033[32m✓\033[0m %s\n' "$*"; }
skip(){ printf '   \033[33m-\033[0m %s\n' "$*"; }
die(){ printf '\n\033[1;31mABORT:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run as your normal user (not root/sudo). sudo is used internally."
command -v sudo >/dev/null || die "sudo is required."

# Keep the sudo timestamp warm so the long build never stalls on a password.
if ! sudo -n true 2>/dev/null; then
  sudo -v || die "sudo authentication failed."
  ( while true; do sudo -n true 2>/dev/null; sleep 50; done ) &
  SUDO_KEEPALIVE=$!
  trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true' EXIT
fi

log "1  Enable deb-src (needed for 'apt-get source linux-raspi')"
# 26.04 uses deb822 /etc/apt/sources.list.d/ubuntu.sources with only 'Types: deb'.
SRC=/etc/apt/sources.list.d/ubuntu.sources
DSRC=/etc/apt/sources.list.d/ubuntu-src.sources
if [ -f "$SRC" ]; then
  if [ ! -f "$DSRC" ] || ! grep -q 'deb-src' "$DSRC" 2>/dev/null; then
    sudo sed 's/^Types: deb$/Types: deb-src/' "$SRC" | sudo tee "$DSRC" >/dev/null
    ok "wrote $DSRC (deb-src for the same suites)"
  else skip "deb-src already present ($DSRC)"; fi
else
  if ! grep -rqsE '^deb-src ' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
    . /etc/os-release
    printf 'deb-src http://archive.ubuntu.com/ubuntu %s main\ndeb-src http://archive.ubuntu.com/ubuntu %s-updates main\n' \
      "$VERSION_CODENAME" "$VERSION_CODENAME" | sudo tee /etc/apt/sources.list.d/deb-src.list >/dev/null
    ok "wrote classic deb-src list"
  else skip "deb-src already present"; fi
fi
sudo apt-get update -qq || die "apt-get update failed"

log "2  Install build toolchain + kernel build-deps"
sudo apt-get install -y build-essential fakeroot dpkg-dev debhelper rsync kmod cpio bc \
  flex bison libssl-dev libncurses-dev libelf-dev libdw-dev dwarves zstd python3 \
  || die "toolchain install failed"
sudo apt-get build-dep -y linux-raspi >/dev/null 2>&1 && ok "build-dep linux-raspi satisfied" \
  || skip "build-dep linux-raspi unavailable (explicit deps installed above — continuing)"

log "3  Fetch linux-raspi kernel source"
mkdir -p "$KBUILD_DIR"
cd "$KBUILD_DIR"
find_srcdir(){ find "$KBUILD_DIR" -maxdepth 1 -type d -name 'linux-raspi-*' 2>/dev/null | sort -V | tail -1; }
SRCDIR="$(find_srcdir)"
if [ -z "$SRCDIR" ]; then
  # 'linux-raspi' is a metapackage whose source is 'linux-meta-raspi' (no kernel);
  # the real tree comes from the versioned linux-image-<abi>-raspi binary.
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

# Ubuntu ships symlinks the .orig tarball cannot carry, recreated by
# debian.<flavour>/reconstruct, which `make bindeb-pkg` never runs: arch/arm64
# overlays must be a symlink to the arm/ tree or `make dtbs` dies, and
# reconstruct's own `ln -sf` misfires when the empty dir already exists.
for rc in debian.raspi/reconstruct debian.master/reconstruct; do
  [ -f "$rc" ] && sh "$rc" >/dev/null 2>&1 || true
done
OVL=arch/arm64/boot/dts/overlays
if [ ! -L "$OVL" ] && [ -d arch/arm/boot/dts/overlays ]; then
  rm -rf "$OVL"; ln -sf ../../../arm/boot/dts/overlays "$OVL"
fi
[ -f "$OVL/Makefile" ] && ok "dts overlays symlink OK" || skip "overlays Makefile still missing (dtbs may fail)"

log "4  Configure: base on running config; trim modules (lean) + NUMA + max-perf"
LEAN="${LEAN:-yes}"        # yes = localmodconfig: build only currently-loaded modules
MAXPERF="${MAXPERF:-yes}"  # yes = throughput-oriented tuning (see below)
if [ ! -f .config ] || [ "${RECONFIG:-}" = 1 ]; then
  make clean >/dev/null 2>&1 || true
  cp "/boot/config-$(uname -r)" .config

  if [ "$LEAN" = yes ]; then
    # Process substitution, not `yes | make`: under pipefail, yes's SIGPIPE (141)
    # would false-fail the pipeline.
    make LSMOD=/proc/modules localmodconfig < <(yes '') || die "localmodconfig failed"
    ok "localmodconfig: trimmed to $(grep -c '=m' .config) modules"
  fi

  scripts/config --enable NUMA --enable NUMA_MEMBLKS --enable NUMA_EMU

  if [ "$MAXPERF" = yes ]; then
    # ARCH_HAS_PREEMPT_LAZY=y makes PREEMPT_NONE and PREEMPT_VOLUNTARY
    # unbuildable, and forcing the choice lands on full PREEMPT — worse throughput.
    scripts/config --disable HZ_1000 --disable HZ_300 --disable HZ_250 --enable HZ_100 --set-val HZ 100  # fewer timer ticks
    scripts/config --enable NO_HZ_IDLE                                                    # tickless idle
    scripts/config --enable  CPU_FREQ_DEFAULT_GOV_PERFORMANCE                             # governor default = performance
    scripts/config --disable CPU_FREQ_DEFAULT_GOV_SCHEDUTIL --disable CPU_FREQ_DEFAULT_GOV_ONDEMAND --disable CPU_FREQ_DEFAULT_GOV_POWERSAVE
    scripts/config --enable  CC_OPTIMIZE_FOR_PERFORMANCE --disable CC_OPTIMIZE_FOR_SIZE   # -O2 for speed, not size
    # sched_ext needs kernel BTF, so DEBUG_INFO_NONE must be off; scripts/config
    # cannot drive a choice by --enable, so pick the DWARF default explicitly.
    scripts/config --disable DEBUG_INFO_NONE --enable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
                   --enable DEBUG_INFO --enable DEBUG_INFO_BTF \
                   --enable BPF_SYSCALL --enable SCHED_CLASS_EXT
    scripts/config --disable DEBUG_PREEMPT --disable SCHEDSTATS --disable LATENCYTOP
    scripts/config --disable PROVE_LOCKING --disable DEBUG_ATOMIC_SLEEP
    ok "max-perf knobs applied (HZ=100, gov=performance, -O2, sched_ext+BTF kept, lazy preempt)"
  fi

  # localmodconfig drops any module not loaded at build time, which killed
  # USB_STORAGE/USB_UAS (no USB disk attached) and MMC_BLOCK (empty microSD slot).
  scripts/config \
    --enable USB_SUPPORT --enable USB --enable USB_XHCI_HCD --enable USB_DWC2 \
    --module USB_HID --module HID_GENERIC \
    --module USB_STORAGE --module USB_UAS \
    --module MMC_BLOCK --module MMC_SDHCI_BRCMSTB --module MMC_SDHCI_OF_DWCMSHC \
    --module EXFAT_FS --module NTFS3_FS --module NLS_ISO8859_1
  ok "storage/USB pinned (USB_STORAGE, USB_UAS, MMC_BLOCK, exFAT/NTFS) — survives LEAN"

  # Tailscale runs userspace WireGuard over a TUN device; without it tailscaled
  # dies with 'CreateTUN("tailscale0") failed; /dev/net/tun does not exist'. This
  # box's base config has "# CONFIG_TUN is not set", so build TUN in (=y). The
  # netfilter modules carry tailscaled's subnet-router / exit-node rules.
  scripts/config --enable TUN
  scripts/config \
    --module WIREGUARD \
    --module NF_TABLES --module NFT_COMPAT --module NFT_NAT --module NFT_MASQ \
    --module IP_NF_IPTABLES --module IP_NF_FILTER --module IP_NF_MANGLE \
    --module IP_NF_NAT --module IP_NF_TARGET_MASQUERADE \
    --module IP6_NF_IPTABLES --module IP6_NF_NAT \
    --module NF_NAT \
    --module NETFILTER_XT_MATCH_COMMENT --module NETFILTER_XT_MATCH_MARK \
    --module NETFILTER_XT_TARGET_MASQUERADE
  ok "tailscale networking pinned (TUN builtin; iptables/nftables + masquerade for subnet-router/exit-node)"

  # The localversion MUST carry a numeric ABI field ("-1-numa", not "-numa"):
  # flash-kernel's pi-try filters names through include_only_flavors, which only
  # recognises VERSION-ABINUM-FLAVOUR, and a bare "-numa" is silently dropped.
  scripts/config --set-str LOCALVERSION "-1-numa" --disable LOCALVERSION_AUTO
  scripts/config --disable SYSTEM_TRUSTED_KEYS   --disable SYSTEM_REVOCATION_KEYS 2>/dev/null || true
  scripts/config --set-str SYSTEM_TRUSTED_KEYS "" --set-str SYSTEM_REVOCATION_KEYS "" 2>/dev/null || true
  make olddefconfig
  ok "config prepared"
else
  skip ".config already exists (RECONFIG=1 to rebuild it)"
fi

log "5  PRE-FLIGHT: verify CONFIG_NUMA_EMU=y before the long build"
if ! grep -q '^CONFIG_NUMA_EMU=y' .config; then
  echo "   current state:"; grep -E 'CONFIG_NUMA(_EMU|_MEMBLKS)?=' .config || true
  die "CONFIG_NUMA_EMU did not stick — refusing to spend 1-2h on a bad build.
     Investigate: does this arch/config allow NUMA_EMU? (needs NUMA=y, which is set.)"
fi
grep -E '^CONFIG_NUMA(_EMU|_MEMBLKS)?=y' .config | sed 's/^/   /'
ok "CONFIG_NUMA_EMU=y confirmed"

# olddefconfig can silently drop a symbol whose dependency it could not satisfy,
# so assert the *resolved* .config before the long compile.
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

# TUN must be =y so /dev/net/tun always exists; assert before the long compile.
if ! grep -q '^CONFIG_TUN=y' .config; then
  echo "   current TUN state:"; grep -E '^(# )?CONFIG_TUN\b' .config | sed 's/^/     /'
  die "CONFIG_TUN is not built in (=y) — refusing to build a kernel tailscale can't
     use. It is pinned via 'scripts/config --enable TUN' in step 4; if it won't
     stick, its dependency (NET/INET) may have been trimmed by localmodconfig."
fi
# The netfilter extras only matter for exit-node use; a client works with TUN alone.
TS_MISSING=""
for sym in NF_TABLES IP_NF_IPTABLES NFT_MASQ NETFILTER_XT_MATCH_MARK; do
  grep -qE "^CONFIG_$sym=[ym]$" .config || TS_MISSING="$TS_MISSING $sym"
done
[ -z "$TS_MISSING" ] || skip "tailscale netfilter extras missing (client OK; exit-node/subnet-router may not work):$TS_MISSING"
grep -E '^CONFIG_(TUN|WIREGUARD|NF_TABLES|NFT_MASQ|IP_NF_IPTABLES|IP_NF_TARGET_MASQUERADE|NETFILTER_XT_MATCH_(COMMENT|MARK))=' .config | sed 's/^/   /'
ok "TUN (built-in) + tailscale netfilter support confirmed"

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

log "6  Build .deb packages (~1-2h on the Pi; JOBS=$JOBS)"
if [ "${RECONFIG:-}" = 1 ]; then
  rm -f "$KBUILD_DIR"/linux-{image,headers}-*-numa_*.deb 2>/dev/null || true
fi
if ls "$KBUILD_DIR"/linux-image-*-numa_*.deb >/dev/null 2>&1; then
  skip "built .debs already exist in $KBUILD_DIR — skipping compile (rm them to rebuild)"
else
  # scripts/Makefile.dtbinst uses `install -D`, which races under -j (two jobs
  # mkdir the same overlays/ dir), so retry packaging serially; the compile is
  # cached, so the retry is quick.
  if ! make -j"$JOBS" bindeb-pkg 2>&1 | tee "$KBUILD_DIR/build.log"; then
    echo "   (parallel packaging failed — retrying dtbs_install/packaging with -j1)"
    make -j1 bindeb-pkg 2>&1 | tee -a "$KBUILD_DIR/build.log" || die "kernel build failed (see $KBUILD_DIR/build.log)"
  fi
  ok "build complete"
fi
ls -1 "$KBUILD_DIR"/linux-{image,headers}-*.deb 2>/dev/null | sed 's/^/   /' || true

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

  KVER="$(basename "$IMG_DEB")"; KVER="${KVER#linux-image-}"; KVER="${KVER%%_*}"
  [ -f "/boot/vmlinuz-$KVER" ] || die "dpkg installed but /boot/vmlinuz-$KVER is missing"

  # With os_check=1 (the default) the Pi 5 bootloader refuses a kernel it cannot
  # confirm is Pi5-compatible. Ubuntu's raspi images carry a trailer that satisfies
  # it; a bindeb-pkg kernel does not. Set before the first section so [tryboot] too.
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

  # The firmware boots the image under /boot/firmware/ (os_prefix), not
  # /boot/vmlinuz-*, and dpkg's zz-flash-kernel hook does not reliably promote it.
  if command -v flash-kernel >/dev/null; then
    log "7b Promote $KVER into /boot/firmware (flash-kernel)"
    # pi-try stages only the kernel it ranks "latest"; one it cannot rank it
    # dismisses with "Ignoring old or unknown version ..." and still exits 0.
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

  # pi-try searches /usr/lib/firmware/$KVER/device-tree/ for the board DTB, where
  # Ubuntu's packaged kernels put them; a bindeb-pkg kernel ships them under
  # /usr/lib/linux-image-$KVER/, so new/ ends up with no DTB and cannot boot.
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
      sudo cp "$OVL_SRC"/overlay_map.dtb "$OVL_SRC"/README "$NEWDIR"/overlays/ 2>/dev/null || true
      ok "copied $(ls "$NEWDIR"/overlays/*.dtbo 2>/dev/null | wc -l) overlays into new/overlays"
    fi
    DTB_ID="bcm2712-rpi-5-b.dtb"
    sudo test -e "$NEWDIR/$DTB_ID" || die "$NEWDIR/$DTB_ID still missing after copy — tryboot would fail at the firmware"
    ok "$NEWDIR/$DTB_ID present — firmware has a device tree to boot"
  fi

  # A silent promotion failure otherwise lands the reboot back on stock.
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
