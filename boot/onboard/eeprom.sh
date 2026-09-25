case "$WK_DO" in
    has-config) command -v rpi-eeprom-config >/dev/null 2>&1 ;;
    has-vc) command -v vcgencmd >/dev/null 2>&1 ;;
    read) rpi-eeprom-config ;;
    vc-read) vcgencmd bootloader_config 2>/dev/null ;;
    soc) tr "\0" "\n" < /proc/device-tree/compatible 2>/dev/null ;;
    apply) cat > /tmp/wk-boot.conf && rpi-eeprom-config --apply /tmp/wk-boot.conf ;;
    bootfs) for d in $(awk '$3 == "vfat" { print $2 }' /proc/mounts); do [ -f "$d/start4.elf" ] && { echo "$d"; exit 0; }; done; exit 1 ;;
    clear) cd "$WK_DIR" && rm -f pieeprom.upd pieeprom.sig recovery.bin RECOVERY.0* recovery.0* && sync ;;
    sum) sha256sum "$WK_PATH" | cut -d" " -f1 ;;
    sync) sync ;;
esac
