nohup sh -c 'sleep 3; if [ "$0" = reboot-tryboot ]; then printf "0 tryboot" > /run/systemd/reboot-param && systemctl reboot; else reboot; fi' "$WK_DO" </dev/null >/dev/null 2>&1 &
