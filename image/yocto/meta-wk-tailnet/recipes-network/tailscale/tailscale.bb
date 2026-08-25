SUMMARY = "Tailscale node agent (upstream static build)"
DESCRIPTION = "The tailscale client and daemon, so that a board built from this \
image is reachable by its tailnet name and nothing about how to reach it has to \
be written down anywhere. See ../../conf/layer.conf for why this layer exists."
HOMEPAGE = "https://tailscale.com"

# Upstream's own LICENSE, carried here because the release tarball does not
# contain one -- so the checksum is over a file this layer ships, which is a
# thing that can actually be verified, rather than over a copy of the common
# BSD-3-Clause text that only looks like the same statement.
LICENSE = "BSD-3-Clause"
LIC_FILES_CHKSUM = "file://LICENSE;md5=cadeae10a8856ddfdb129866b75b33e3"

require ${THISDIR}/tailscale-release.inc
PV = "${TS_VERSION}"

# Yocto's architecture names on the left, tailscale's on the right. The default
# is deliberately invalid rather than a guess: an architecture nobody has
# checked should fail at the fetch, loudly, and not install a binary for the
# wrong machine. COMPATIBLE_HOST below is what makes it fail earlier still.
TS_ARCH = "unsupported"
TS_ARCH:aarch64 = "arm64"
TS_ARCH:arm = "arm"

COMPATIBLE_HOST = "(aarch64|arm).*-linux"

SRC_URI = "https://pkgs.tailscale.com/stable/tailscale_${PV}_${TS_ARCH}.tgz \
           file://LICENSE \
           file://wk-tailnet-join \
           file://wk-tailnet-join.service"
SRC_URI[sha256sum] = "${@d.getVar('TS_SHA256_%s' % d.getVar('TS_ARCH'))}"

S = "${WORKDIR}/tailscale_${PV}_${TS_ARCH}"

inherit systemd features_check

# The units are the whole delivery mechanism here, so an image without systemd
# would install this recipe and quietly never run any of it.
REQUIRED_DISTRO_FEATURES = "systemd"

SYSTEMD_SERVICE:${PN} = "tailscaled.service wk-tailnet-join.service"

# tailscaled programs the host firewall on startup. The images this layer is
# built into carry iptables and no nft; naming it here is what keeps that true
# rather than incidental.
RDEPENDS:${PN} += "iptables"

# Prebuilt, static, and already stripped: the three QA checks that exist for
# things this recipe compiled itself, and did not.
INHIBIT_PACKAGE_STRIP = "1"
INHIBIT_PACKAGE_DEBUG_SPLIT = "1"
INHIBIT_SYSROOT_STRIP = "1"
#
# `buildpaths` belongs with them and is the least obvious: a Go binary records
# the paths it was built from, and upstream's builder is not this build -- so
# the check fires on every file here, correctly, about something no recipe can
# fix without compiling tailscale itself.
INSANE_SKIP:${PN} += "already-stripped ldflags buildpaths"

# Nothing to configure or compile: this recipe unpacks a release and installs
# it. Marked rather than left to the default tasks finding no Makefile, so that
# a tarball that grows one some day does not quietly start building it.
do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -d ${D}${bindir} ${D}${sbindir}
    install -m 0755 ${S}/tailscale  ${D}${bindir}/tailscale
    install -m 0755 ${S}/tailscaled ${D}${sbindir}/tailscaled

    # Upstream's own unit and defaults, from the tarball, rather than a copy
    # written here: they carry the state, socket and runtime directories the
    # daemon expects, and a hand-written unit is a second opinion about them.
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${S}/systemd/tailscaled.service ${D}${systemd_system_unitdir}/
    install -d ${D}${sysconfdir}/default
    install -m 0644 ${S}/systemd/tailscaled.defaults ${D}${sysconfdir}/default/tailscaled

    # And the join, which is ours: the daemon coming up is not the same as this
    # node being on the tailnet, and what makes it one is a key that arrives
    # with the card rather than in the image.
    install -m 0755 ${WORKDIR}/wk-tailnet-join ${D}${sbindir}/wk-tailnet-join
    install -m 0644 ${WORKDIR}/wk-tailnet-join.service ${D}${systemd_system_unitdir}/

    install -d ${D}${localstatedir}/lib/tailscale
}

FILES:${PN} += "${systemd_system_unitdir} ${localstatedir}/lib/tailscale"
