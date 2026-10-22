# Streamed in on every start: the browser's network process reads no http_proxy, so the system proxy is set too.
set -u
addr=$WK_ADDR port=$WK_PORT
if [ -s /tmp/.wk-egress.new ]; then sed "s|\$HOME|$HOME|g" /tmp/.wk-egress.new > "$HOME/.wk-egress"; rm -f /tmp/.wk-egress.new
else rm -f "$HOME/.wk-egress"; fi   # written with the guest's own home, so a shell with another HOME (sudo) reads the same paths
if [ -s /tmp/.wk-github-ca.new ]; then
    mv /tmp/.wk-github-ca.new "$HOME/.wk-github-ca.pem"
    cat /etc/ssl/cert.pem "$HOME/.wk-github-ca.pem" > "$HOME/.wk-ca-bundle.pem"
else
    rm -f "$HOME/.wk-github-ca.pem" "$HOME/.wk-ca-bundle.pem"
fi
dev=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
[ -n "$dev" ] || { echo "no default route in the guest" >&2; exit 1; }
svc=$(networksetup -listnetworkserviceorder | awk -v d="$dev" '
    /^\([0-9]+\)/ { name = substr($0, index($0, ") ") + 2) }
    index($0, "Device: " d ")") { print name; exit }')
[ -n "$svc" ] || { echo "no network service owns $dev" >&2; exit 1; }
read_state() {
    networksetup -getsecurewebproxy "$svc" | awk '
        /^Enabled:/ { e = $2 } /^Server:/ { s = $2 } /^Port:/ { p = $2 }
        END { print e ":" s ":" p }'
}
if [ -z "$addr" ]; then
    [ "$(read_state)" = "No::0" ] && exit 0
    sudo -n networksetup -setwebproxystate "$svc" off &&
    sudo -n networksetup -setsecurewebproxystate "$svc" off
    exit
fi
[ "$(read_state)" = "Yes:$addr:$port" ] && exit 0
sudo -n networksetup -setwebproxy "$svc" "$addr" "$port" &&
sudo -n networksetup -setsecurewebproxy "$svc" "$addr" "$port" &&
sudo -n networksetup -setproxybypassdomains "$svc" localhost 127.0.0.1
