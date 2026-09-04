#!/usr/bin/env bash
#
# Per-workspace setup, installed as ~/.wkdev-firstrun and run once by
# .wkdev-init: nothing here may be a mount, a daemon or anything else that has
# to be re-established on every boot.

set -euo pipefail

WK_TOOLS=/opt/wk-tools
SRC=/src/WebKit

log()  { printf '[firstrun] %s\n' "$*"; }
warn() { printf '[firstrun] warning: %s\n' "$*" >&2; }

# --- egress ------------------------------------------------------------------
# Container init runs before any wk command has started the loopback bridge.
if [ -S /run/wk/proxy.sock ]; then
    /opt/wk-tools/container/proxy/ensure-bridge.sh true && log "egress bridge up"
else
    log "WARNING: no egress proxy socket at /run/wk/proxy.sock -- no network"
fi

# --- the fleet-request door ---------------------------------------------------
# Without it `wk boot` and `wk pi deploy|bench` refuse in here, which is also
# what a correctly locked-down workspace looks like.
if [ -S /run/wk/broker.sock ]; then
    log "fleet-request broker present -- wk boot / wk pi deploy|bench become requests"
else
    log "no fleet-request broker at /run/wk/broker.sock -- no bench device from here"
fi

# --- identity, and how fast git is in here -----------------------------------
# The include also brings the settings that make `git status` in a WebKit
# checkout milliseconds rather than seconds (fsmonitor, untrackedCache,
# manyFiles).
git config --global --replace-all include.path "$WK_TOOLS/dotfiles/gitconfig"
# The include's core.excludesFile: git ignores a missing one silently.
[ -f "$HOME/.gitignore" ] || printf '.DS_Store\n.cache\ncompile_commands.json\n' > "$HOME/.gitignore"
git config --global --add safe.directory "$SRC"

# Two lines and nothing else, because no credential is on this side of the
# boundary. The per-fork aliases are in /secrets/ssh_config, written by
# `wk push on|off` -- an include rather than a copy, so a rotated key reaches
# every existing workspace at once. Each names a public half and an ssh-agent
# socket; the private halves are outside this container.
install -d -m 0700 "$HOME/.ssh"

# The workspace has no network interface, so every connection goes through the
# egress proxy's unix socket. First and for every host, because ssh takes the
# first value it sees for each keyword. %h expands to the resolved HostName, so
# an alias still arrives at the proxy as github.com. ssh ignores an Include whose
# file is absent.
cat > "$HOME/.ssh/config" <<'PROXYEOF'
Host *
    ProxyCommand /opt/wk-tools/container/proxy/ssh-proxy.py %h %p

Include /secrets/ssh_config
PROXYEOF
chmod 0600 "$HOME/.ssh/config"

if [ -s /secrets/ssh_config ]; then
    log "ssh: fork aliases from /secrets/ssh_config; keys are in an agent outside this workspace"
else
    log "no /secrets/ssh_config, so ~/.ssh/config names no fork host and a push"
    log "         from here cannot resolve one -- 'wk push on' (or 'off') writes it"
fi

# Remotes. Re-asserted from wk_wiring_script, so a workspace made from an older
# snapshot still gets today's origin and today's forks.
if [ -d "$SRC/.git" ]; then
    _wiring=$(bash -c '. "$1/lib/common.sh"; . "$1/lib/store.sh"; wk_wiring_script "$2"' \
                  _ "$WK_TOOLS" "$SRC" 2>/dev/null) || _wiring=""
    if [ -n "$_wiring" ] && sh -c "$_wiring"; then
        log "remotes: origin=WebKit/WebKit (push refused), forks added"
    else
        log "WARNING: could not wire the checkout's remotes"
    fi
fi

# --- claude ------------------------------------------------------------------
# Its own installer, because Claude Code self-updates into that directory.
# claude.ai resolves inside the range the egress policy already allows.
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
    log "Claude CLI already present"
else
    log "installing Claude Code"
    if curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1; then
        log "Claude CLI installed ($("$HOME/.local/bin/claude" --version 2>/dev/null || echo unknown))"
    else
        log "claude install failed -- check egress; run the installer by hand"
    fi
fi

install -d "$HOME/.claude"

# Read-only policy from the synced tooling tree, the same in every workspace.
for f in settings.json hooks CLAUDE.md; do
    ln -sfn "$WK_TOOLS/claude/$f" "$HOME/.claude/$f"
done

# One mutable directory on a volume shared by every workspace.
ln -sfn /skills "$HOME/.claude/skills"

# One secret per name in the store, each read by shell/bashrc into the variable
# its agent expects. Symlinks onto the read-only /secrets mount, made whether or
# not the secret exists yet -- a dangling link is "not set".
#
# Value rows only. A file row (the claude.ai login) is rewritten in place by its
# own tool, so it lives in the writable /agent-rw mount every container shares:
# a symlink there would be replaced by a regular file on the first refresh, which
# goes through a temp file and a rename.
_agent_secrets() { bash -c '. "$1/lib/store.sh"; wk_agent_secrets' _ "$WK_TOOLS" 2>/dev/null; }
while read -r _sname _sfile _shome _svar _skind; do
    [ -n "$_sname" ] || continue
    if [ "$_skind" = file ]; then
        [ -s "/agent-rw/$_sfile" ] \
            || log "no $_sname credential yet -- 'wk key set $_sname' on the host stores one"
        continue
    fi
    ln -sfn "/secrets/$_sfile" "$HOME/$_shome"
    [ -e "$HOME/$_shome" ] \
        || log "no $_sname credential yet -- 'wk key set $_sname' on the host stores one ($_svar)"
done <<EOF
$(_agent_secrets)
EOF

# Without the mount there is nowhere for a file row to live.
[ -d /agent-rw ] \
    || log "no /agent-rw mount -- this container predates it; 'wk rm' and 'wk new' remake it"

# --- profiling tools -----------------------------------------------------------
# heaptrack, valgrind (its massif tool) and sysprof-cli are distro packages;
# samply ships no .deb, so it comes from a pinned GitHub release, checksummed.
# Wrapped, because a profiler is not load-bearing the way the shell rc is. Here
# rather than in a Containerfile: the image is pulled pre-built.
_install_profilers() {
    if sudo apt-get update -qq >/dev/null 2>&1 \
       && sudo apt-get install -y --no-install-recommends heaptrack valgrind sysprof >/dev/null 2>&1; then
        log "heaptrack, valgrind and sysprof-cli installed"
    else
        warn "apt install of heaptrack/valgrind/sysprof failed -- check egress. Retry by hand:
         sudo apt-get update && sudo apt-get install heaptrack valgrind sysprof
         Until then 'wk profile --mode heaptrack|massif' refuses by name."
    fi

    if command -v samply >/dev/null 2>&1; then
        log "samply already present"
        return 0
    fi
    local ver=0.13.1 sarch sum tmp got
    case "$(uname -m)" in
        x86_64)  sarch=x86_64-unknown-linux-gnu
                 sum=61875daad67888798690dea3cb2748279df6ac299c5c6a857d67eed7642473d9 ;;
        aarch64) sarch=aarch64-unknown-linux-gnu
                 sum=aa465162b62830168775b7ff4804bc35049436dcbc29bb3d1ea9f580380ea06a ;;
        *)       warn "samply: no linux/$(uname -m) release published upstream (github.com/mstange/samply), skipping"
                 return 0 ;;
    esac
    tmp=$(mktemp -d)
    if curl -fsSL -o "$tmp/samply.tar.xz" \
           "https://github.com/mstange/samply/releases/download/samply-v${ver}/samply-${sarch}.tar.xz"; then
        got=$(sha256sum "$tmp/samply.tar.xz" | awk '{print $1}')
        # The tarball unpacks to samply-<target>/samply, one top-level
        # directory per target triple.
        if [ "$got" = "$sum" ] \
           && tar -xJf "$tmp/samply.tar.xz" -C "$tmp" \
           && sudo install -m 0755 "$tmp/samply-${sarch}/samply" /usr/local/bin/samply; then
            log "samply $ver installed (github.com/mstange/samply, sha256 verified)"
        else
            warn "samply $ver download did not verify (expected sha256 $sum) -- not installed"
        fi
    else
        warn "samply download failed -- check egress. 'wk profile --mode samply' will refuse by name."
    fi
    rm -rf "$tmp"
}
_install_profilers

# --- core dumps ------------------------------------------------------------
# Without a core_pattern that writes somewhere writable and a ulimit that allows
# a dump, a crash hands back nothing but an exit code. core_pattern is a
# kernel-global sysctl but per-pid-namespace once a process owns one: a rootless
# podman container gets its own, and the mapped root inside holds CAP_SYS_ADMIN
# there without the host-level --cap-add=SYS_ADMIN. Attempted, not fatal.
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

# The ddebs archive backtraces glibc/Mesa/GTK with real symbols instead of `??`.
# ddebs.ubuntu.com is in the egress allowlist (container/proxy/wk-proxy.py).
DDEBS_SOURCES=/etc/apt/sources.list.d/ddebs.list
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

# --- editors -----------------------------------------------------------------
# Workspace-only: helix needs the checkout's clangd and compile commands, and
# lazygit runs against the checkout it sits in. Wrapped, because under `set -e`
# a tarball that does not unpack as expected would throw away the lldb config
# and shell rc set up below.
_install_helix() {
    local ver=25.07 arch hxarch tmp
    arch=$(uname -m)
    case "$arch" in
        aarch64|arm64) hxarch=aarch64 ;;
        x86_64)        hxarch=x86_64 ;;
        # helix publishes no linux/armhf release, and there is no toolchain
        # here to build one.
        *)             log "helix: no linux/$arch release published upstream (github.com/helix-editor/helix) -- not installed"
                        return 0 ;;
    esac

    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/helix-editor/helix/releases/download/${ver}/helix-${ver}-${hxarch}-linux.tar.xz" \
        | tar -xJ -C "$tmp" --strip-components=1 || { rm -rf "$tmp"; return 1; }

    sudo install -m 0755 "$tmp/hx" /usr/local/bin/hx || { rm -rf "$tmp"; return 1; }

    # A real directory, not a symlink to the repo: helix wants its runtime/
    # beside the config, and /opt/wk-tools is mounted read-only.
    install -d "$HOME/.config/helix"
    cp -R "$WK_TOOLS/container/helix/." "$HOME/.config/helix/" 2>/dev/null || true
    [ -d "$tmp/runtime" ] && cp -R "$tmp/runtime" "$HOME/.config/helix/runtime"
    rm -rf "$tmp"
}

# Same release-tarball shape as helix, but checksummed: this one lands a bare
# binary straight onto PATH, where a truncated or swapped download would run.
_install_lazygit() {
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

# --- lldb --------------------------------------------------------------------
# WebKit's lldb helpers live in the checkout, so they are wired up per workspace.
{
    echo "command script import $SRC/Tools/lldb/lldb_webkit.py"
    echo "command script import $WK_TOOLS/container/lldb/rr.py"
    cat "$WK_TOOLS/dotfiles/lldbinit"
} > "$HOME/.lldbinit"

# --- shell -------------------------------------------------------------------
# PATH comes from shell/bashrc, not a second list here. The `cd` is guarded on
# an interactive shell because bash started by sshd reads ~/.bashrc even
# non-interactively, and would otherwise move the working directory of every
# `ssh <ws> <command>`, rsync and file transfer.
grep -qF 'wk-tools/shell/bashrc' "$HOME/.bashrc" 2>/dev/null || \
    printf '\n. %s/shell/bashrc\ncase $- in *i*) cd %s ;; esac\n' \
        "$WK_TOOLS" "$SRC" >> "$HOME/.bashrc"

# bash reads ~/.bashrc for interactive non-login shells and ~/.bash_profile for
# login shells, and the image ships neither.
[ -f "$HOME/.bash_profile" ] || printf '%s\n' \
    '# wk: login shells read this, interactive non-login shells read .bashrc.' \
    '[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"' > "$HOME/.bash_profile"

# --- the workspace marker ----------------------------------------------------
# What lets the wk in /opt/wk-tools act on this container rather than reaching
# for a podman machine that does not exist from in here (lib/target.sh).
# arch= cannot be re-derived here: the kernel is the host's, so `uname -m` in an
# armhf container answers aarch64, and `wk build` would drop linux32 and the ARM
# flags.
if [ -n "${WK_WORKSPACE:-}" ]; then
    cat > "$HOME/.wk-workspace" <<EOF
# wk: this machine IS a workspace. Written by container/firstrun.sh.
name=$WK_WORKSPACE
src=$SRC
config=jsc-release
arch=${WK_ARCH:-native}
EOF
    log "workspace marker written ($HOME/.wk-workspace)"
else
    warn "WK_WORKSPACE is not set -- no workspace marker, so 'wk build' in here
         will not find this workspace. Recreate with a current wk."
fi

# Creation's completion marker, the same name every target uses last
# (lib/target.sh): what `wk new` waits for, readable from the host's view of
# this home directory with no `wk enter`. .wkdev-init carries on regardless of
# how this hook exited, so without it a failure part-way through is invisible.
: > "$HOME/.wk-ready"

log "workspace ${WK_WORKSPACE:-?} ready"
