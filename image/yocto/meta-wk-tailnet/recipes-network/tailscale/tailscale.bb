SUMMARY = "Tailscale node agent (upstream static build)"
DESCRIPTION = "The tailscale client and daemon, so that a board built from this \
image is reachable by its tailnet name and nothing about how to reach it has to \
be written down anywhere. See ../../conf/layer.conf for why this layer exists."
HOMEPAGE = "https://tailscale.com"

# Upstream's LICENSE, shipped here since the release tarball (${S}) has none; it lands in ${WORKDIR}.
LICENSE = "BSD-3-Clause"
LIC_FILES_CHKSUM = "file://${WORKDIR}/LICENSE;md5=cadeae10a8856ddfdb129866b75b33e3"

require ${THISDIR}/tailscale-release.inc
PV = "${TS_VERSION}"

# An unchecked architecture fails the fetch rather than installing the wrong binary.
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

REQUIRED_DISTRO_FEATURES = "systemd"

SYSTEMD_SERVICE:${PN} = "tailscaled.service wk-tailnet-join.service"

# tailscaled programs the firewall on startup; these images carry iptables and no nft.
RDEPENDS:${PN} += "iptables"

# Prebuilt, static and stripped; a Go binary records upstream's build paths (buildpaths).
INHIBIT_PACKAGE_STRIP = "1"
INHIBIT_PACKAGE_DEBUG_SPLIT = "1"
INHIBIT_SYSROOT_STRIP = "1"
INSANE_SKIP:${PN} += "already-stripped ldflags buildpaths"

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -d ${D}${bindir} ${D}${sbindir}
    install -m 0755 ${S}/tailscale  ${D}${bindir}/tailscale
    install -m 0755 ${S}/tailscaled ${D}${sbindir}/tailscaled

    # Upstream's own unit and defaults, from the tarball.
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${S}/systemd/tailscaled.service ${D}${systemd_system_unitdir}/
    install -d ${D}${sysconfdir}/default
    install -m 0644 ${S}/systemd/tailscaled.defaults ${D}${sysconfdir}/default/tailscaled

    # The join spends the key that arrives with the card.
    install -m 0755 ${WORKDIR}/wk-tailnet-join ${D}${sbindir}/wk-tailnet-join
    install -m 0644 ${WORKDIR}/wk-tailnet-join.service ${D}${systemd_system_unitdir}/

    install -d ${D}${localstatedir}/lib/tailscale
}

FILES:${PN} += "${systemd_system_unitdir} ${localstatedir}/lib/tailscale"
