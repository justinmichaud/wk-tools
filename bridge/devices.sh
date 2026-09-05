# The phones a tailnet bridge can run on. The role is identical on both, so bridge/provision.sh never asks which one it runs on; only the kill switches differ, and the camera stream and the uplink each need one on.

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
