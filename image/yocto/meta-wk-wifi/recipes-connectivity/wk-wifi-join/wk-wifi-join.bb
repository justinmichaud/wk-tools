SUMMARY = "Bring up WiFi from a credential seeded onto the card (wk)"
DESCRIPTION = "The script and unit that spend a WiFi credential wk sysimage \
write seeds onto the card at /etc/wpa_supplicant/wpa_supplicant-wlan0.conf, \
so a board with no cable at the bench (rpi3/rpi4/rpi5) still comes up \
reachable. See ../../conf/layer.conf for why this layer exists."
HOMEPAGE = "https://github.com/justinmichaud/wk-tools"
LICENSE = "CLOSED"

SRC_URI = "file://wk-wifi-join \
           file://wk-wifi-join.service"

S = "${WORKDIR}"

inherit systemd

REQUIRED_DISTRO_FEATURES = "systemd"

SYSTEMD_SERVICE:${PN} = "wk-wifi-join.service"

# wpa_supplicant is oe-core's own recipe; this package only carries what oe-
# core does not -- the join script and its unit. A DHCP client is not named
# here because the script detects what the image already carries (udhcpc or
# dhclient) rather than pulling a second one in for images that ship neither
# yet either.
RDEPENDS:${PN} += "wpa-supplicant"

do_install() {
    install -d ${D}${sbindir}
    install -m 0755 ${WORKDIR}/wk-wifi-join ${D}${sbindir}/wk-wifi-join

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/wk-wifi-join.service ${D}${systemd_system_unitdir}/
}

FILES:${PN} += "${systemd_system_unitdir}"
