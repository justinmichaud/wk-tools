# scarthgap's pseudo (e11ae91) cannot track the directory fd tar hands to mkdirat on an aarch64
# 7.x kernel ("got *at() syscall for unknown directory, fd 4" ... "Cannot mkdir: Bad address");
# `pseudo bash -c 'tar -cf - . | tar -xf -'` reproduces it outside bitbake. Upstream c63f439
# (__open64_2 wrapper) and b3958b0 (AT_EMPTY_PATH efault) fix it. pseudo is a build-time fakeroot,
# never installed, so this changes how the image is built and not what it contains.
# Delete this file when the pinned poky carries pseudo >= 1.9.11.

SRCREV = "ba8887e5f1e922f866681ec7dec1a00b602a9328"
PV = "1.9.11+git"

# The first two are in 1.9.11 already (6831273, 865ca5b) and fail do_patch. The third keeps sstate
# portable between build hosts, applies no longer, and is not needed: SSTATE_DIR is namespaced per
# build-host image (yocto_ws.py). One assignment, since a second `SRC_URI:remove` replaces the first;
# unconditional, since `:class-native` misses nativesdk-pseudo, which populate_sdk builds.
SRC_URI:remove = "file://0001-configure-Prune-PIE-flags.patch \
                  file://glibc238.patch \
                  file://older-glibc-symbols.patch"
