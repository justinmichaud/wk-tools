#!/usr/bin/env bash
#
# Per-workspace setup. Installed as ~/.wkdev-firstrun and run once by
# .wkdev-init when the container first starts.
#
# Once is all you get: .wkdev-init guards its whole task list behind an
# init-done file and then sleeps forever, so this never runs again on restart.
# Nothing here may therefore be a mount, a daemon, or anything else that has to
# be re-established on every boot -- those belong in the container create flags
# or in `wk enter`.

set -euo pipefail

WK_TOOLS=/opt/wk-tools
SRC=/src/WebKit

log()  { printf '[firstrun] %s\n' "$*"; }
warn() { printf '[firstrun] warning: %s\n' "$*" >&2; }

# --- egress ------------------------------------------------------------------
# This runs during container init, before any wk command has had a chance to
# start the loopback bridge -- and the first thing below that needs the network
# is the Claude installer. Without this the workspace comes up with no CLI and a
# confusing "could not connect to 127.0.0.1:3128" in the init log.
if [ -S /run/wk/proxy.sock ]; then
    /opt/wk-tools/container/proxy/ensure-bridge.sh true && log "egress bridge up"
else
    log "WARNING: no egress proxy socket at /run/wk/proxy.sock -- no network"
fi

# --- the fleet-request door ---------------------------------------------------
# Reported at first run rather than discovered later. Without it `wk boot` and
# `wk pi deploy|bench` refuse in here, which is also exactly what a correctly
# locked-down workspace looks like -- so the two are worth telling apart before
# anybody spends an afternoon on it. Nothing is started or retried: the socket
# belongs to the workstation and appears here when it is there.
if [ -S /run/wk/broker.sock ]; then
    log "fleet-request broker present -- wk boot / wk pi deploy|bench become requests"
else
    log "no fleet-request broker at /run/wk/broker.sock -- no bench device from here"
fi

# --- identity ----------------------------------------------------------------
git config --global user.name  "Justin Michaud"
git config --global user.email "jmichaud@igalia.com"
git config --global --add safe.directory "$SRC"

# The forks, read from the mounted tooling at use time rather than from an
# environment variable frozen into the container at creation: `wk_push_forks`
# in lib/store.sh is the one list, and a workspace created last month must see
# today's answer. In a subshell, because those files are `wk`'s and define
# log(), warn() and a shell mode of their own.
_forks() { bash -c '. "$1/lib/store.sh"; wk_push_forks' _ "$WK_TOOLS" 2>/dev/null; }

# One deploy key per fork. Both forks are on github.com, so the key is selected
# by ssh host alias rather than hostname -- that is the only way to use two
# different keys against the same host.
#
# Reads are anonymous over HTTPS and need no credential; these are push-only.
install -d -m 0700 "$HOME/.ssh"
: > "$HOME/.ssh/config"
chmod 0600 "$HOME/.ssh/config"

# ssh has no network to reach: the workspace has no interface at all, so every
# connection goes through the egress proxy's unix socket. Written first and for
# every host, because ssh takes the first value it sees for each keyword and the
# per-fork blocks below deliberately do not set ProxyCommand. %h expands to the
# resolved HostName, so an alias still arrives at the proxy as github.com -- and
# the proxy refuses anything not in its allowlist, which makes this a path
# rather than a permission. It also covers the Pi test devices, which are
# reachable by address and nothing else.
cat >> "$HOME/.ssh/config" <<'PROXYEOF'
Host *
    ProxyCommand /opt/wk-tools/container/proxy/ssh-proxy.py %h %p

PROXYEOF

# The key is *pointed at*, never copied.
#
# Two reasons, and the second is the one that matters. A copy goes stale: the
# day a key is rotated, every existing workspace goes on offering the dead one.
# And a copy cannot be taken back -- /secrets is mounted read-only for the
# container's life, so the host can add and remove keys there and this
# workspace sees it immediately, which is exactly what makes `wk push off` a
# switch rather than a suggestion. A copy in here would be a second key nobody
# can reach. (The Claude credentials are already linked for the first reason;
# this is the same pattern with a second reason behind it.)
#
# ssh follows the symlink and checks the *target's* permissions, which are 0600
# in the store; a dangling link is simply "no key", which is the off position.
_have_key=0
while read -r _remote _repo _alias; do
    [ -n "$_remote" ] || continue

    ln -sfn "/secrets/build_key_${_remote}" "$HOME/.ssh/id_${_remote}"
    cat >> "$HOME/.ssh/config" <<EOF
Host ${_alias}
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_${_remote}
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
    if [ -e "$HOME/.ssh/id_${_remote}" ]; then
        _have_key=1
        log "deploy key linked for $_repo (push via $_alias)"
    else
        log "no key for $_repo right now -- pushes there will be refused ('wk push status')"
    fi
done <<EOF
$(_forks)
EOF
[ "$_have_key" = 1 ] || log "         'wk push on' exposes them; 'wk key register' creates them"

# Remotes. The snapshot already carries these, but it was published at some
# point in the past and lives in a read-only lower layer, so they are
# re-asserted here from the same one authority (wk_wiring_script): a workspace
# made from an older snapshot still gets today's origin and today's forks.
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
# Installed by its own installer, to its own standard location
# (~/.local/bin/claude -> ~/.local/share/claude/versions/N). Nothing is copied
# to a path of our choosing: Claude Code manages that versions directory and
# self-updates into it, so a binary parked somewhere else could never update
# itself and would silently drift out of date.
#
# claude.ai resolves inside Anthropic's published range, which the egress
# policy already allows, so the workspace can fetch this itself.
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

# settings, hooks and CLAUDE.md are read-only policy: they come from the synced
# tooling tree and are the same in every workspace.
for f in settings.json hooks CLAUDE.md; do
    ln -sfn "$WK_TOOLS/claude/$f" "$HOME/.claude/$f"
done

# Skills are different: workspaces are expected to improve them as they learn,
# so all workspaces share one *mutable* directory on a volume rather than each
# getting a private read-only copy. An edit made in one workspace is
# immediately visible to the others, and survives the workspace being deleted.
# `wk skills` diffs it against the repo so those edits can be reviewed and
# committed rather than quietly accumulating.
ln -sfn /skills "$HOME/.claude/skills"

# Credentials live on a shared volume so one `claude login` serves every
# workspace. They cannot come from the host: Claude Code keeps them in the
# macOS Keychain on Darwin and in this file on Linux.
if [ -f /secrets/claude-credentials.json ]; then
    ln -sfn /secrets/claude-credentials.json "$HOME/.claude/.credentials.json"
else
    log "no shared Claude credentials yet -- run 'claude login' once; it will persist"
fi

# --- profiling tools -----------------------------------------------------------
# docs/Urgent/HANDOFF-profile.md: nothing installed these, so 'wk profile'
# refused every native mode by name and the only way through was a
# hand-installed binary at a host path. heaptrack, valgrind (its massif tool)
# and sysprof-cli are distro packages; samply ships no .deb, so it is fetched
# from its own pinned GitHub release and checksummed, the same shape as the
# helix install below. Wrapped the same way and for the same reason: a
# profiler is not load-bearing for the workspace the way the shell rc and lldb
# config are, so its failure is reported and not fatal.
#
# This is where it actually has to run: the workspace image is
# ghcr.io/igalia/wkdev-sdk, pulled pre-built (this repo owns no Containerfile
# for it, only sdk-patches/apply.sh, which patches the SDK's *scripts*, not
# its filesystem). The two Containerfiles this repo does own
# (container/buildroot, container/yocto) build separate cross-compile hosts
# that 'wk profile' never runs against; they carry the same install for
# parity, in case that changes, but this firstrun hook is the path that
# actually reaches a `wk build`/`wk profile` workspace today.
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
        # The tarball is not flat -- it unpacks to samply-<target>/samply
        # beside its README/LICENSE/RELEASES files, one top-level directory
        # per target triple, confirmed by listing the archive rather than
        # assumed.
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
# docs/Urgent/HANDOFF-debug.md: without a core_pattern that writes somewhere
# writable and a ulimit that allows a dump at all, a crash has nothing to hand
# back but an exit code -- which is what 'wk run --until-crash' needs most.
#
# core_pattern is a kernel-global sysctl on the host, but per-pid-namespace
# once a process owns one: a rootless podman container gets its own user and
# pid namespace, and the mapped root inside holds CAP_SYS_ADMIN *in that
# namespace* without the host-level --cap-add=SYS_ADMIN grant
# sdk-patches/apply.sh gates behind --unsafe-caps (that grant is about the
# host's own namespace, not this one). So this is attempted with sudo and
# warned about, not treated as fatal: an environment where it does not apply
# (a shared host pid namespace) is not this hook's problem to solve, only to
# report -- the same shape as the helix and profiler installs above.
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

# The ddebs debug-symbol repository (ddebs.ubuntu.com) is not set up here: it
# needs a host this repo's egress proxy does not allow
# (container/proxy/wk-proxy.py's ALLOWED_HOSTS), and widening that allowlist
# is a call this hook does not get to make on its own -- see
# docs/Urgent/HANDOFF-debug.md.

# --- editors -----------------------------------------------------------------
# helix is workspace-only now: it needs the checkout's clangd and compile
# commands, which only exist here.
#
# Wrapped so a failure here cannot cost the workspace anything that matters.
# An editor is a convenience; the lldb config and the shell rc set up below are
# not, and under `set -e` a helix tarball that does not unpack the way this
# expects throws both away.
_install_helix() {
    local ver=25.07 arch hxarch tmp
    arch=$(uname -m)
    case "$arch" in
        aarch64|arm64) hxarch=aarch64 ;;
        x86_64)        hxarch=x86_64 ;;
        *)             return 0 ;;
    esac

    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/helix-editor/helix/releases/download/${ver}/helix-${ver}-${hxarch}-linux.tar.xz" \
        | tar -xJ -C "$tmp" --strip-components=1 || { rm -rf "$tmp"; return 1; }

    sudo install -m 0755 "$tmp/hx" /usr/local/bin/hx || { rm -rf "$tmp"; return 1; }

    # A real directory, copied -- not a symlink to the repo. helix wants its
    # runtime/ beside the config, /opt/wk-tools is mounted read-only, and
    # writing several thousand runtime files into the tooling checkout would be
    # wrong even if it were writable.
    install -d "$HOME/.config/helix"
    cp -R "$WK_TOOLS/container/helix/." "$HOME/.config/helix/" 2>/dev/null || true
    [ -d "$tmp/runtime" ] && cp -R "$tmp/runtime" "$HOME/.config/helix/runtime"
    rm -rf "$tmp"
}

if ! command -v hx >/dev/null 2>&1; then
    log "installing helix"
    _install_helix || warn "helix install failed -- continuing without it"
fi

# --- lldb --------------------------------------------------------------------
# WebKit's lldb helpers live in the checkout, so they are wired up per
# workspace rather than pinned to one tree in a host dotfile.
{
    echo "command script import $SRC/Tools/lldb/lldb_webkit.py"
    echo "command script import $WK_TOOLS/container/lldb/rr.py"
    cat "$WK_TOOLS/dotfiles/lldbinit"
} > "$HOME/.lldbinit"

# --- shell -------------------------------------------------------------------
# ~/.local/bin as well as the tooling: that is where the Claude installer puts
# its own launcher (a symlink into ~/.local/share/claude/versions), and without
# it on PATH `wk claude` -- which runs `bash -lc "exec claude ..."` -- fails
# with "claude: not found" in a workspace where the CLI is installed and
# working. The image does not put it there and neither did anything else.
#
# The `cd` is guarded on an interactive shell, and that is not decoration:
# bash started by sshd reads ~/.bashrc even non-interactively, so an unguarded
# `cd` moves the working directory of every `ssh <ws> <command>`, every rsync
# and every file transfer -- which is a relative path landing in the checkout
# instead of the home directory. It cost a debugging round when Zed uploaded its
# server to `~/.zed_server/...` and sftp resolves that against /src/WebKit.
# (`wk zed` asks sshd for internal-sftp, which runs no shell at all, and this
# guard is the other half of the same fix.)
grep -qF 'wk-tools/shell/bashrc' "$HOME/.bashrc" 2>/dev/null || \
    printf '\n. %s/shell/bashrc\nexport PATH="%s:$HOME/.local/bin:$PATH"\ncase $- in *i*) cd %s ;; esac\n' \
        "$WK_TOOLS" "$WK_TOOLS" "$SRC" >> "$HOME/.bashrc"

# bash reads ~/.bashrc for interactive non-login shells and ~/.bash_profile for
# login shells, and the image ships neither. Without this, `wk enter` gets the
# history settings, the editor and the PATH -- and anything arriving over ssh
# as a login shell silently does not.
[ -f "$HOME/.bash_profile" ] || printf '%s\n' \
    '# wk: login shells read this, interactive non-login shells read .bashrc.' \
    '[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"' > "$HOME/.bash_profile"

# --- the workspace marker ----------------------------------------------------
# Says "this machine IS a workspace", and where its checkout is. That is what
# lets the wk in /opt/wk-tools act on this container -- `wk build <config>`,
# `wk run`, `wk test` -- instead of trying to reach a workspace from outside,
# which from in here means a podman machine that does not exist. See
# targets/local.sh and "am I a workspace?" in lib/target.sh.
#
# Written from $WK_WORKSPACE rather than guessed: the name reaches `wk status`
# and every message, and a workspace calling itself by the wrong name is worse
# than one that says it does not know. Refuses to invent one.
#
# arch= is the one field that cannot be re-derived in here at all. The kernel
# is the host's, so `uname -m` in an armhf container answers aarch64; only
# whoever created the container knows, and this is where they say so. Without
# it an in-workspace `wk build` would drop linux32 and the ARM flags and
# configure a 64-bit tree with a 32-bit compiler.
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

# Creation's completion marker, and the last thing this hook does.
#
# .wkdev-init runs this hook and then carries on regardless of how it exited,
# so a failure part-way through is invisible: the container comes up, the
# creation reports success, and the workspace is quietly missing whatever came
# after the failing step. That happened -- a root-owned ~/.config aborted the
# helix install and cost this workspace its lldb config and its shell rc --
# and the only honest fix is for something downstream to check.
#
# `.wk-ready` is that check, and it is now the same name on every target
# (lib/target.sh, "creation's completion marker"): the file every driver writes
# last, next to the workspace rather than on whichever machine drove the
# creation. It is what `wk new` waits for, what stops `wk build` starting on a
# half-initialised checkout, and what tells a workspace whose container was
# removed by hand apart from one that never finished being made. This home
# directory is a directory on the host, so the host reads it without entering
# anything.
#
# Written here rather than by the driver because this is genuinely the last
# act of creating a container workspace -- the driver's part ended when
# wkdev-create returned, minutes before the workspace was usable.
: > "$HOME/.wk-ready"

log "workspace ${WK_WORKSPACE:-?} ready"
