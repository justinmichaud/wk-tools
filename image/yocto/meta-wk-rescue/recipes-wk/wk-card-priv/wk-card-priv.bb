SUMMARY = "The privileged card writer a wk rescue system needs (wk)"
DESCRIPTION = "admin/wk-card-priv from the wk-tools checkout: the fixed-verb, \
device-gated helper every 'wk sysimage write' step goes through. On a \
rescue system it is what lets the board write and arm its own bench medium \
(wk sysimage write --disk <board>:<device>) with no card reader in the loop."
HOMEPAGE = "https://github.com/justinmichaud/wk-tools"
LICENSE = "CLOSED"

# The repository's own copies, five directories up.
FILESEXTRAPATHS:prepend := "${THISDIR}/../../../../../admin:${THISDIR}/../../../../../boot:"
SRC_URI = "file://wk-card-priv file://check-boot-files.py"

S = "${WORKDIR}"

# Where lib/wk/boot/driver.py looks for it, on every machine alike.
CARD_PRIV_DIR = "/usr/local/libexec"

# Every external command the helper runs.
RDEPENDS:${PN} += "bash coreutils tar python3-core \
    util-linux-findmnt util-linux-lsblk util-linux-sfdisk util-linux-partx util-linux-blkid \
    e2fsprogs-resize2fs e2fsprogs-e2fsck"

do_install() {
    install -d ${D}${CARD_PRIV_DIR}
    install -m 0755 ${WORKDIR}/wk-card-priv ${D}${CARD_PRIV_DIR}/wk-card-priv
    # Beside the helper, under the name it knows (CHECK_BOOT_FILES).
    install -m 0644 ${WORKDIR}/check-boot-files.py ${D}${CARD_PRIV_DIR}/wk-check-boot-files.py
}

FILES:${PN} += "${CARD_PRIV_DIR}"
