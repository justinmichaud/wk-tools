# The phones a tailnet bridge can run on, and what differs between them.
#
# A bridge is a phone with two legs: WiFi to the house, USB-C Ethernet to a
# segment that has no other way onto the tailnet -- a BMC's dedicated
# management port, or a test board on a cable. The *role* is identical on both
# devices; what differs is hardware, and this file is the whole list of it.
# `bridge/provision.sh` therefore never asks which phone it is running on:
# everything below is either a message for a person or a fact the provisioner
# is handed.
#
# postmarketOS on both, and that is the reason this table is short. The
# predecessor of this role ran PureOS on a Librem 5 and was written in
# systemd, NetworkManager-Debian and apt; none of that was portable to a second
# phone. pmOS supports `purism-librem5` and `pine64-pinephone` from the same
# tree, with the same OpenRC, the same apk and the same NetworkManager, so one
# provisioner covers both and a third device is a table entry.
#
# A device sets two things, and only two, because only two are read:
#
#   DEV_KILLSWITCH one line: which switches exist and where they are. The camera
#                  stream depends on one of them being *on* and the uplink on
#                  another, so this is operational rather than trivia
#   DEV_NOTE       one line, for the listing
#
# It used to carry the pmOS codename, the WiFi part and the battery node as
# well. All three were dead: the codename is image/profiles.sh's (one source of
# truth for what gets built), the health check finds the battery by globbing
# /sys/class/power_supply rather than being told, and the WiFi part turned out
# to be prose rather than a value -- both phones' radios drop associations, which
# is why the watchdog ladder is not device-specific:
#
#   PinePhone   RTL8723CS on SDIO, driver `8723cs`, out of tree and never loved
#   Librem 5    RS9116 on SDIO, `rsi_sdio` over `rsi_91x`, and the reason the
#               module-reload rung exists at all -- it wedges its firmware in a
#               way a link bounce does not clear

bridge_device_list() {
    cat <<'LIST'
pinephone  PinePhone (Allwinner A64). Kill switches are DIP switches under the back cover.
librem5    Librem 5 (i.MX8M Quad). Three kill switches on the side.
LIST
}

bridge_device_load() {
    case "$1" in
    pinephone)
        DEV_KILLSWITCH="6 DIP switches under the back cover: modem, WiFi/BT, mic, speaker, cameras, headphone"
        DEV_NOTE="PinePhone, A64"
        ;;
    librem5)
        DEV_KILLSWITCH="3 switches on the side: WiFi/BT, cellular, camera+mic"
        DEV_NOTE="Librem 5, i.MX8M Quad"
        ;;
    *)
        return 1
        ;;
    esac
}
