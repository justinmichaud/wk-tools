systemctl stop netdata 2>/dev/null
case "$WK_BACKEND" in
  (systemd) systemctl start --no-block weston ;;
  (drm)
    pkill -x weston 2>/dev/null; sleep 1
    mkdir -p /run/user/0 && chmod 700 /run/user/0
    XDG_RUNTIME_DIR=/run/user/0 setsid weston --backend=drm-backend.so --tty=1 --socket=wk-bench \
      </dev/null >/tmp/wk-weston.log 2>&1 &
    sleep 5 ;;
  (rdp)
    mkdir -p /etc/wk-bench
    [ -f /etc/wk-bench/rdp.key ] || {
      openssl req -x509 -newkey rsa:2048 -nodes -keyout /etc/wk-bench/rdp.key -out /etc/wk-bench/rdp.crt \
        -days 3650 -subj /CN=wk-bench >/dev/null 2>&1 || exit 3
      chmod 600 /etc/wk-bench/rdp.key; }
    pkill -x weston 2>/dev/null; sleep 1
    mkdir -p /run/user/0 && chmod 700 /run/user/0
    XDG_RUNTIME_DIR=/run/user/0 setsid weston --backend=rdp-backend.so --renderer=pixman \
      --width=1280 --height=1024 --socket=wk-bench \
      --rdp-tls-cert=/etc/wk-bench/rdp.crt --rdp-tls-key=/etc/wk-bench/rdp.key \
      </dev/null >/tmp/wk-weston.log 2>&1 &
    sleep 6 ;;
esac
true
