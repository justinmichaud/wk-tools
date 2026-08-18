# Ubuntu 26.04: install the host's packages.
#
# Stock apt only, plus exactly one third-party repository (Tailscale). Zed and
# Claude Code install into ~/.local via their own scripts rather than adding
# repositories, so nothing else can push updates into the base system. The host
# is meant to stay boring; everything that changes often lives in a workspace.

# `|| true`: grep exits 1 on no matches, and an unguarded failing command
# substitution aborts the whole of ./setup under `set -e`.
_pkgs=$(grep -vE '^\s*(#|$)' "$WK_ROOT/host/linux/apt.txt" | tr '\n' ' ' || true)

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

# --- Tailscale: the one permitted third-party repository ---------------------
if have tailscale; then
    unchanged "tailscale"
else
    info "installing Tailscale (adds its apt repository)"
    curl -fsSL https://tailscale.com/install.sh | sh
    changed "installed tailscale"
fi

# --- Zed: tarball into ~/.local, not a repository ----------------------------
if have zed || [ -x "$HOME/.local/bin/zed" ]; then
    unchanged "zed"
else
    info "installing Zed into ~/.local"
    curl -fsSL https://zed.dev/install.sh | sh
    changed "installed zed"
fi

# --- Claude Code: likewise ---------------------------------------------------
if have claude || [ -x "$HOME/.local/bin/claude" ]; then
    unchanged "claude"
else
    info "installing Claude Code into ~/.local"
    curl -fsSL https://claude.ai/install.sh | bash
    changed "installed claude"
fi

# Rootless podman would be simpler, but its pasta backend re-emits container
# traffic as ordinary host sockets, which never traverse the forward chain --
# so there is nothing for nftables to filter and the workspace egress policy
# would be unenforceable.
if [ "$(id -u)" -ne 0 ] && ! sudo -n podman version >/dev/null 2>&1; then
    log "note: workspaces run under rootful podman so the egress firewall applies"
fi

unset _pkgs _missing p
