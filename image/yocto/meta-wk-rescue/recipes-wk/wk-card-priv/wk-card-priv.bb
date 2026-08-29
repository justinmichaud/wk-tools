SUMMARY = "The privileged card writer a wk rescue system needs (wk)"
DESCRIPTION = "admin/wk-card-priv from the wk-tools checkout: the fixed-verb, \
device-gated helper every 'wk sysimage write' step goes through. On a \
rescue system it is what lets the board write and arm its own bench medium \
(wk sysimage write --disk <board>:<device>) with no card reader in the loop."
HOMEPAGE = "https://github.com/justinmichaud/wk-tools"
LICENSE = "CLOSED"

# The repository's own copies, five directories up from this recipe: the
# helper and the boot-file checker it runs (`boot-check`) each have one
# source, and a copy under files/ would be a second one to drift.
FILESEXTRAPATHS:prepend := "${THISDIR}/../../../../../admin:${THISDIR}/../../../../../boot:"
SRC_URI = "file://wk-card-priv file://check-boot-files.py"

S = "${WORKDIR}"

# Where lib/boot/disk.sh looks for it (CARD_PRIV), on every machine alike.
CARD_PRIV_DIR = "/usr/local/libexec"

# Every external command the helper runs (grep it: findmnt, lsblk, sfdisk,
# partx, blkid, resize2fs, e2fsck, mtools, python3, base64, sha256sum, tar, dd).
RDEPENDS:${PN} += "bash coreutils tar python3-core \
    util-linux-findmnt util-linux-lsblk util-linux-sfdisk util-linux-partx util-linux-blkid \
    e2fsprogs-resize2fs e2fsprogs-e2fsck mtools"

do_install() {
    install -d ${D}${CARD_PRIV_DIR}
    install -m 0755 ${WORKDIR}/wk-card-priv ${D}${CARD_PRIV_DIR}/wk-card-priv
    # Root runs it, so it lives beside the helper under the name the helper
    # knows (CHECK_BOOT_FILES), never at a path a caller names.
    install -m 0644 ${WORKDIR}/check-boot-files.py ${D}${CARD_PRIV_DIR}/wk-check-boot-files.py
}

FILES:${PN} += "${CARD_PRIV_DIR}"
