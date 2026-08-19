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

# --- identity ----------------------------------------------------------------
git config --global user.name  "Justin Michaud"
git config --global user.email "jmichaud@igalia.com"
git config --global --add safe.directory "$SRC"

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

# Comma-separated, not space-separated: the value reaches the container through
# wkdev-create's --additional-flags, which word-splits, so a space would break
# it into separate podman arguments.
_have_key=0
_IFS_SAVE=$IFS; IFS=,
for _spec in ${WK_FORKS:-}; do
    IFS=$_IFS_SAVE
    # remote:repo:alias, packed by cmd/new because firstrun has no wk libs.
    _remote=${_spec%%:*}; _rest=${_spec#*:}
    _repo=${_rest%%:*}; _alias=${_rest#*:}
    _src="/secrets/build_key_${_remote}"

    [ -f "$_src" ] || { log "WARNING: no key for $_repo -- pushing there will fail"; continue; }

    install -m 0600 "$_src" "$HOME/.ssh/id_${_remote}"
    cat >> "$HOME/.ssh/config" <<EOF
Host ${_alias}
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_${_remote}
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
    _have_key=1
    log "deploy key installed for $_repo (push via $_alias)"
    IFS=,
done
IFS=$_IFS_SAVE
[ "$_have_key" = 1 ] || log "         generate and register keys with: wk key register"

# Remotes. The snapshot already carries these, but they live in the read-only
# lower layer, so re-assert them here: a workspace made from an older snapshot
# still gets the right push URLs.
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" remote set-url origin https://github.com/WebKit/WebKit.git 2>/dev/null || true
    # Pushing to origin is impossible (no upstream write access) and almost
    # always a mistake; fail immediately rather than after an auth round trip.
    git -C "$SRC" remote set-url --push origin no-push://use-a-fork-remote 2>/dev/null || true

    IFS=,
    for _spec in ${WK_FORKS:-}; do
        IFS=$_IFS_SAVE
        _remote=${_spec%%:*}; _rest=${_spec#*:}
        _repo=${_rest%%:*}; _alias=${_rest#*:}
        git -C "$SRC" remote add "$_remote" "https://github.com/${_repo}.git" 2>/dev/null \
            || git -C "$SRC" remote set-url "$_remote" "https://github.com/${_repo}.git"
        git -C "$SRC" remote set-url --push "$_remote" "git@${_alias}:${_repo}.git"
        log "remote $_remote -> $_repo (fetch https, push ssh)"
        IFS=,
    done
    IFS=$_IFS_SAVE
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

# --- editors -----------------------------------------------------------------
# helix is workspace-only now: it needs the checkout's clangd and compile
# commands, which only exist here.
#
# Wrapped so a failure here cannot cost the workspace anything that matters.
# An editor is a convenience; the lldb config and the shell rc set up below are
# not, and `set -e` used to throw both away because a helix tarball did not
# unpack the way this expected.
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
grep -qF 'wk-tools/shell/bashrc' "$HOME/.bashrc" 2>/dev/null || \
    printf '\n. %s/shell/bashrc\nexport PATH="%s:$PATH"\ncd %s\n' \
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
if [ -n "${WK_WORKSPACE:-}" ]; then
    cat > "$HOME/.wk-workspace" <<EOF
# wk: this machine IS a workspace. Written by container/firstrun.sh.
name=$WK_WORKSPACE
src=$SRC
EOF
    log "workspace marker written ($HOME/.wk-workspace)"
else
    warn "WK_WORKSPACE is not set -- no workspace marker, so 'wk build' in here
         will not find this workspace. Recreate with a current wk."
fi

# A completion marker, checked by `wk new`.
#
# .wkdev-init runs this hook and then carries on regardless of how it exited,
# so a failure part-way through is invisible: the container comes up, the
# creation reports success, and the workspace is quietly missing whatever came
# after the failing step. That happened -- a root-owned ~/.config aborted the
# helix install and cost this workspace its lldb config and its shell rc --
# and the only honest fix is for something downstream to check.
: > "$HOME/.wk-firstrun-complete"

log "workspace ${WK_WORKSPACE:-?} ready"
