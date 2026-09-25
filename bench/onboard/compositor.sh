grep -oiE "Output .[a-z0-9-]+. enabled" /tmp/wk-weston.log 2>/dev/null | tail -1
pidof weston-desktop-shell 2>/dev/null
true
