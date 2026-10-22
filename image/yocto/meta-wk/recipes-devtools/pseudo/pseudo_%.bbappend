# scarthgap's own pseudo recipe today: 1.9.11 at this SRCREV (poky scarthgap, read 2026-10-21). WebKit's
# Tools/yocto/rpi/manifest.xml pins poky at d4576e3c, whose pseudo (1.9.3) cannot track the directory fd
# GNU tar 1.35 hands to mkdirat on an Ubuntu 24.04 build host. Delete this file when that manifest moves
# to a poky with pseudo >= 1.9.11.

SRCREV = "ba8887e5f1e922f866681ec7dec1a00b602a9328"
PV = "1.9.11+git"

# The first two are in 1.9.11 already (6831273, 865ca5b) and fail do_patch. The third keeps sstate
# portable between build hosts, applies no longer, and is not needed: SSTATE_DIR is namespaced per
# build-host image (yocto_ws.py). One assignment, since a second `SRC_URI:remove` replaces the first;
# unconditional, since `:class-native` misses nativesdk-pseudo, which populate_sdk builds.
SRC_URI:remove = "file://0001-configure-Prune-PIE-flags.patch \
                  file://glibc238.patch \
                  file://older-glibc-symbols.patch"
