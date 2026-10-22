#!/usr/bin/env bash
# Run once, as ~/.wkdev-firstrun: nothing here may be a mount or a daemon.

set -euo pipefail

WK_TOOLS=/opt/wk-tools
SRC=$WK_SRC                             # the repo's checkout, lib/wk/repos.py

log()  { printf '[firstrun] %s\n' "$*"; }
warn() { printf '[firstrun] warning: %s\n' "$*" >&2; }

# Every fetch below goes through a bridge of this run's own, with the injector's CA and GITHUB_COM_* in the environment.
[ "${1:-}" = --bridged ] || exec "$WK_TOOLS/container/proxy/ensure-bridge.sh" "$0" --bridged
[ -S /run/wk/proxy.sock ] || log "WARNING: no egress proxy socket at /run/wk/proxy.sock -- no network"

# root keeps this bridge and its CA bundle across sudo's env_reset (lib/wk/sudo.py), so `sudo apt-get` reaches the proxy.
PYTHONPATH="$WK_TOOLS/lib" python3 -m wk.sudo workspace \
    || warn "the sudoers drop-in was not installed (above) -- 'sudo apt-get' in here has no network"

if [ -S /run/wk/broker.sock ]; then
    log "fleet-request broker present -- 'wk boot' / 'wk bench' become requests"
else
    log "no fleet-request broker at /run/wk/broker.sock -- no bench machine from here"
fi

git config --global --replace-all include.path "$WK_TOOLS/dotfiles/gitconfig"
[ -f "$HOME/.gitignore" ] || printf '.DS_Store\n.cache\ncompile_commands.json\n' > "$HOME/.gitignore"
git config --global --add safe.directory "$SRC"
# git's ssh transport, the placeholder claude.ai login and the agent credentials (lib/wk/secrets.py, deliver).
PYTHONPATH="$WK_TOOLS/lib" python3 -m wk.secrets deliver container "$WK_TOOLS" \
    || warn "the agent credentials were not all delivered (above) -- 'wk rm' and 'wk new' remake the workspace"

install -d -m 0700 "$HOME/.ssh"

cat > "$HOME/.ssh/config" <<'PROXYEOF'
Host *
    ProxyCommand /opt/wk-tools/container/proxy/ssh-proxy.py %h %p
PROXYEOF
chmod 0600 "$HOME/.ssh/config"

if [ -S /run/wk/push.sock ]; then
    log "push: git's ssh transport is the host's push service; no key is in here"
else
    log "WARNING: no push service at /run/wk/push.sock -- a push from here is refused"
fi

_git_py() { PYTHONPATH="$WK_TOOLS/lib" WK_ROOT="$WK_TOOLS" python3 -m wk.git "$@"; }

if [ -n "${WK_CLONE:-}" ]; then         # a repo cloned here, not overlaid on a snapshot
    git clone --quiet --branch "$WK_BRANCH" "$WK_CLONE" "$SRC"
    log "checkout: $WK_CLONE's $WK_BRANCH cloned into $SRC"
    git -C "$SRC" remote set-url --push origin "$WK_PUSH"
    log "push: $WK_PUSH, through the push service"
elif [ -d "$SRC/.git" ]; then           # an old snapshot's remotes are stale
    _mirror="${WK_MIRROR:-}"
    [ -n "$_mirror" ] || log "no mirror on this place, so every fetch in here reads github.com"
    _wiring=$(_git_py wiring-script "$SRC" "$_mirror") \
        || { _wiring=""; warn "wk.git wiring-script failed, so it wired nothing"; }
    if [ -n "$_wiring" ] && sh -c "$_wiring"; then
        log "remotes: origin=WebKit/WebKit (push refused), forks added; fetches read ${_mirror:-github.com}"
    else
        warn "could not wire the checkout's remotes"
    fi

    _setup=$(_git_py pr-tool-setup-script "$SRC") || _setup=""
    if [ -n "$_setup" ] && _out=$(sh -c "$_setup" </dev/null); then
        log "git-webkit: $_out"
    else
        log "WARNING: 'git-webkit setup' did not finish (above) -- 'wk sync ${WK_WORKSPACE:-?} --fix' on the host"
    fi
fi

install -d "$HOME/.claude"

for f in settings.json hooks CLAUDE.md; do    # read-only, from the synced tree
    ln -sfn "$WK_TOOLS/claude/$f" "$HOME/.claude/$f"
done

ln -sfn /skills "$HOME/.claude/skills"  # one mutable dir, shared by every ws

_install_profilers() {                  # wrapped: not load-bearing
    if sudo apt-get update -qq >/dev/null 2>&1 \
       && sudo apt-get install -y --no-install-recommends heaptrack valgrind sysprof >/dev/null 2>&1; then
        log "heaptrack, valgrind and sysprof-cli installed"
    else
        warn "apt install of heaptrack/valgrind/sysprof failed -- check egress. Retry by hand:
         sudo apt-get update && sudo apt-get install heaptrack valgrind sysprof
         Until then 'wk run --profile=heaptrack|massif' refuses by name."
    fi

    if command -v samply >/dev/null 2>&1; then
        log "samply already present"
        return 0
    fi
    local release ver sarch sum url tmp got  # samply ships no .deb
    if ! release=$(PYTHONPATH="$WK_TOOLS/lib" python3 -m wk.samply release "$(uname -m)"); then
        warn "samply: no linux/$(uname -m) release published upstream (github.com/mstange/samply), skipping"
        return 0
    fi
    read -r ver sarch sum url <<< "$release"
    tmp=$(mktemp -d)
    if curl -fsSL -o "$tmp/samply.tar.xz" "$url"; then
        got=$(sha256sum "$tmp/samply.tar.xz" | awk '{print $1}')
        if [ "$got" = "$sum" ] \
           && tar -xJf "$tmp/samply.tar.xz" -C "$tmp" \
           && sudo install -m 0755 "$tmp/samply-${sarch}/samply" /usr/local/bin/samply; then
            log "samply $ver installed (github.com/mstange/samply, sha256 verified)"
        else
            warn "samply $ver download did not verify (expected sha256 $sum) -- not installed"
        fi
    else
        warn "samply download failed -- check egress. 'wk run --profile=samply' will refuse by name."
    fi
    rm -rf "$tmp"
}
_install_profilers

# core_pattern is a kernel-global sysctl but per-pid-namespace once a process
# owns one, and the mapped root of a rootless container has CAP_SYS_ADMIN there.
CORE_DIR="$HOME/wk-cores"
install -d "$CORE_DIR"
if sudo sh -c "echo '$CORE_DIR/core.%e.%p.%t' > /proc/sys/kernel/core_pattern" 2>/dev/null; then
    log "core_pattern -> $CORE_DIR/core.<comm>.<pid>.<time>"
else
    warn "could not set core_pattern (this container's pid namespace may not own it) -- check:
         cat /proc/sys/kernel/core_pattern"
fi
grep -qF 'ulimit -c unlimited' "$HOME/.bashrc" 2>/dev/null || \
    printf '\nulimit -c unlimited\n' >> "$HOME/.bashrc"
log "ulimit -c unlimited added to .bashrc"

DDEBS_SOURCES=/etc/apt/sources.list.d/ddebs.list   # symbols, not `??`
if [ -f "$DDEBS_SOURCES" ]; then
    log "ddebs.ubuntu.com already provisioned ($DDEBS_SOURCES)"
else
    _ddebs_codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
    log "provisioning ddebs.ubuntu.com for $_ddebs_codename"
    if sudo apt-get update -qq >/dev/null 2>&1 \
       && sudo apt-get install -y --no-install-recommends ubuntu-dbgsym-keyring >/dev/null 2>&1 \
       && printf 'deb http://ddebs.ubuntu.com %s main restricted universe multiverse\ndeb http://ddebs.ubuntu.com %s-updates main restricted universe multiverse\ndeb http://ddebs.ubuntu.com %s-proposed main restricted universe multiverse\n' \
              "$_ddebs_codename" "$_ddebs_codename" "$_ddebs_codename" \
              | sudo tee "$DDEBS_SOURCES" >/dev/null \
       && sudo apt-get update -qq >/dev/null 2>&1; then
        log "ddebs.ubuntu.com added ($DDEBS_SOURCES) -- 'sudo apt-get install <pkg>-dbgsym' now works"
    else
        warn "ddebs.ubuntu.com provisioning failed -- check egress. Retry by hand:
         sudo apt-get install ubuntu-dbgsym-keyring
         sudo tee $DDEBS_SOURCES <<<'deb http://ddebs.ubuntu.com $_ddebs_codename main restricted universe multiverse'
         sudo apt-get update"
    fi
fi

_install_helix() {                      # wrapped: `set -e` would skip below
    local ver=25.07 arch hxarch tmp
    arch=$(uname -m)
    case "$arch" in
        aarch64|arm64) hxarch=aarch64 ;;
        x86_64)        hxarch=x86_64 ;;
        *)             log "helix: no linux/$arch release published upstream (github.com/helix-editor/helix) -- not installed"
                        return 0 ;;
    esac

    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/helix-editor/helix/releases/download/${ver}/helix-${ver}-${hxarch}-linux.tar.xz" \
        | tar -xJ -C "$tmp" --strip-components=1 || { rm -rf "$tmp"; return 1; }

    sudo install -m 0755 "$tmp/hx" /usr/local/bin/hx || { rm -rf "$tmp"; return 1; }

    install -d "$HOME/.config/helix"    # a real dir: /opt/wk-tools is ro
    cp -R "$WK_TOOLS/container/helix/." "$HOME/.config/helix/" 2>/dev/null || true
    [ -d "$tmp/runtime" ] && cp -R "$tmp/runtime" "$HOME/.config/helix/runtime"
    rm -rf "$tmp"
}

_install_lazygit() {                    # checksummed: a bare binary on PATH
    local ver=0.64.1 arch lgarch sum tmp got
    arch=$(uname -m)
    case "$arch" in
        aarch64|arm64) lgarch=linux_arm64
                       sum=8b7ca3b344e60340ad1f89f29b9868ee39bcaba5bb92ee818bbe65476bb8b6e7 ;;
        x86_64)        lgarch=linux_x86_64
                       sum=f8ea237c41f194cd799b48505518bfdaae4edf5a2ad6bd3d898e939785ee4532 ;;
        *)             log "lazygit: no linux/$arch release published upstream (github.com/jesseduffield/lazygit) -- not installed"
                        return 0 ;;
    esac

    tmp=$(mktemp -d)
    if curl -fsSL -o "$tmp/lazygit.tar.gz" \
           "https://github.com/jesseduffield/lazygit/releases/download/v${ver}/lazygit_${ver}_${lgarch}.tar.gz"; then
        got=$(sha256sum "$tmp/lazygit.tar.gz" | awk '{print $1}')
        if [ "$got" = "$sum" ] \
           && tar -xzf "$tmp/lazygit.tar.gz" -C "$tmp" lazygit \
           && sudo install -m 0755 "$tmp/lazygit" /usr/local/bin/lazygit; then
            log "lazygit $ver installed (github.com/jesseduffield/lazygit, sha256 verified)"
        else
            warn "lazygit $ver download did not verify (expected sha256 $sum) -- not installed"
        fi
    else
        warn "lazygit download failed -- check egress"
    fi
    rm -rf "$tmp"
}

if ! command -v hx >/dev/null 2>&1; then
    log "installing helix"
    _install_helix || warn "helix install failed -- continuing without it"
fi
if ! command -v lazygit >/dev/null 2>&1; then
    log "installing lazygit"
    _install_lazygit || warn "lazygit install failed -- continuing without it"
fi
if ! command -v npm >/dev/null 2>&1; then
    if sudo apt-get install -y --no-install-recommends nodejs npm >/dev/null 2>&1; then
        log "node $(node -v) and npm installed (Ubuntu's archive), for 'wk new' to install pi with"
    else
        warn "apt install of nodejs/npm failed -- check egress; this workspace is made without pi"
    fi
fi

{                                       # WebKit's helpers are in its checkout
    [ -n "${WK_CLONE:-}" ] || echo "command script import $SRC/Tools/lldb/lldb_webkit.py"
    echo "command script import $WK_TOOLS/container/lldb/rr.py"
    cat "$WK_TOOLS/dotfiles/lldbinit"
} > "$HOME/.lldbinit"

# The `cd` is guarded on an interactive shell: sshd's bash reads ~/.bashrc even
# non-interactively, and would move the cwd of every `ssh <ws> <cmd>` and rsync.
grep -qF 'wk-tools/shell/bashrc' "$HOME/.bashrc" 2>/dev/null || \
    printf '\n. %s/shell/bashrc\ncase $- in *i*) cd %s ;; esac\n' \
        "$WK_TOOLS" "$SRC" >> "$HOME/.bashrc"

[ -f "$HOME/.bash_profile" ] || printf '%s\n' \
    '# wk: login shells read this, interactive non-login shells read .bashrc.' \
    '[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"' > "$HOME/.bash_profile"

# The marker lib/wk/places.py's Local reads. arch= is recorded because the kernel is the
# host's: `uname -m` in an armhf container answers aarch64.
if [ -n "${WK_WORKSPACE:-}" ]; then
    cat > "$HOME/.wk-workspace" <<EOF
# wk: this machine IS a workspace. Written by container/firstrun.sh.
name=$WK_WORKSPACE
src=$SRC
repo=$WK_REPO
arch=${WK_ARCH:-native}
EOF
    log "workspace marker written ($HOME/.wk-workspace)"
else
    warn "WK_WORKSPACE is not set -- no workspace marker, so 'wk build' in here
         will not find this workspace. Recreate with a current wk."
fi

: > "$HOME/.wk-ready"                   # what `wk new` waits for

log "workspace ${WK_WORKSPACE:-?} ready"
