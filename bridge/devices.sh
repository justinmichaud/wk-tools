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
# A device sets:
#
#   DEV_PMOS       the postmarketOS device codename -- what `pmbootstrap init`
#                  and the release image filenames call it
#   DEV_WIFI       the WiFi part, for the message when the uplink misbehaves.
#                  Named because on both phones it *is* the weak component,
#                  and knowing which one you have is the difference between
#                  "expected, the watchdog handles it" and "something is wrong"
#   DEV_BATTERY    the power-supply node, for the health report's battery line
#   DEV_KILLSWITCH one line: which switches exist and where they are. The
#                  camera stream depends on one of them being *on*, so this is
#                  operational rather than trivia
#   DEV_NOTE       one line, for the listing

bridge_device_list() {
    cat <<'LIST'
pinephone  PinePhone (Allwinner A64). Kill switches are DIP switches under the back cover.
librem5    Librem 5 (i.MX8M Quad). Three kill switches on the side.
LIST
}

bridge_device_load() {
    DEV_NAME="$1"
    case "$1" in
    pinephone)
        DEV_PMOS=pine64-pinephone
        # RTL8723CS on SDIO, driver `8723cs`. Out of tree and never loved; it
        # drops associations under load the same way the Librem 5's part does,
        # which is why the escalating watchdog is not device-specific.
        DEV_WIFI="RTL8723CS (8723cs)"
        DEV_BATTERY=axp20x-battery
        DEV_KILLSWITCH="6 DIP switches under the back cover: modem, WiFi/BT, mic, speaker, cameras, headphone"
        DEV_NOTE="PinePhone, A64"
        ;;
    librem5)
        DEV_PMOS=purism-librem5
        # RS9116 on SDIO, driver `rsi_sdio` over `rsi_91x`. The module reload
        # rung of the watchdog exists because of this part specifically: it
        # wedges its firmware in a way a link bounce does not clear.
        DEV_WIFI="RS9116 (rsi_sdio)"
        DEV_BATTERY=max170xx_battery
        DEV_KILLSWITCH="3 switches on the side: WiFi/BT, cellular, camera+mic"
        DEV_NOTE="Librem 5, i.MX8M Quad"
        ;;
    *)
        return 1
        ;;
    esac
}
