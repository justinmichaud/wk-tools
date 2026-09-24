{ vcgencmd bootloader_config 2>/dev/null || rpi-eeprom-config 2>/dev/null; } | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p'
