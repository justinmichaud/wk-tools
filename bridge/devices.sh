# The phones a tailnet bridge can run on -- WiFi to the house, USB-C
# Ethernet to a segment with no other way onto the tailnet. Role identical
# on both (postmarketOS, `purism-librem5`/`pine64-pinephone`, same tree), so
# `bridge/provision.sh` never asks which one it runs on. Only two fields are
# read: DEV_KILLSWITCH (which switches exist and where -- the camera stream
# needs one *on*, the uplink another) and DEV_NOTE (for the listing). Not
# the pmOS codename (image/profiles.sh's own source of truth) or the WiFi
# chip/watchdog (differ per phone; bridge/bin/wk-bridge-netwatch, -healthcheck).

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
