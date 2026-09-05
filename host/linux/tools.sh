# `|| true`: grep exits 1 on no matches, which set -e would abort ./setup on.
_pkgs=$(grep -vE '^\s*(#|$)' "$WK_ROOT/host/linux/apt.txt" | tr '\n' ' ' || true)

while IFS= read -r _line; do
    case "$_line" in
        ''|\#*) continue ;;
    esac
    case "$_line" in
        *[!a-z0-9+.:-]*)
            die "host/linux/apt.txt: not a package name -- '$_line'
    Every line here is one package. A comment that lost its '#' is the usual
    cause. Nothing was installed." ;;
    esac
done < "$WK_ROOT/host/linux/apt.txt"

_missing=""
for p in $_pkgs; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || _missing="$_missing $p"
done

if [ -z "$_missing" ]; then
    unchanged "apt packages"
else
    info "installing:$_missing"
    sudo apt-get update -qq
    # shellcheck disable=SC2086
    sudo apt-get install -y $_missing
    changed "installed$_missing"
fi

if have tailscale; then
    unchanged "tailscale"
else
    info "installing Tailscale (adds its apt repository)"
    curl -fsSL https://tailscale.com/install.sh | sh
    changed "installed tailscale"
fi

if have zed || [ -x "$HOME/.local/bin/zed" ]; then
    unchanged "zed"
else
    info "installing Zed into ~/.local"
    curl -fsSL https://zed.dev/install.sh | sh
    changed "installed zed"
fi

if have claude || [ -x "$HOME/.local/bin/claude" ]; then
    unchanged "claude"
else
    info "installing Claude Code into ~/.local"
    curl -fsSL https://claude.ai/install.sh | bash
    changed "installed claude"
fi

# Rootless pasta re-emits container traffic as host sockets: nftables cannot see it.
if [ "$(id -u)" -ne 0 ] && ! sudo -n podman version >/dev/null 2>&1; then
    log "note: workspaces run under rootful podman so the egress firewall applies"
fi

unset _pkgs _missing p
