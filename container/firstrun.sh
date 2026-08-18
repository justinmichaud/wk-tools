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

log() { printf '[firstrun] %s\n' "$*"; }

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
if ! command -v hx >/dev/null 2>&1; then
    log "installing helix"
    ver=25.07
    arch=$(uname -m)
    case "$arch" in
        aarch64|arm64) hxarch=aarch64 ;;
        x86_64)        hxarch=x86_64 ;;
        *)             hxarch="" ;;
    esac
    if [ -n "$hxarch" ]; then
        tmp=$(mktemp -d)
        if curl -fsSL "https://github.com/helix-editor/helix/releases/download/${ver}/helix-${ver}-${hxarch}-linux.tar.xz" \
             | tar -xJ -C "$tmp" --strip-components=1; then
            sudo install -m 0755 "$tmp/hx" /usr/local/bin/hx
            install -d "$HOME/.config"
            ln -sfn "$WK_TOOLS/container/helix" "$HOME/.config/helix"
            [ -d "$tmp/runtime" ] && cp -r "$tmp/runtime" "$HOME/.config/helix/runtime"
        fi
        rm -rf "$tmp"
    fi
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

log "workspace ${WK_WORKSPACE:-?} ready"
