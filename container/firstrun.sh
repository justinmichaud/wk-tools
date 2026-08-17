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

# The build key is a GitHub deploy key scoped to justinmichaud/WebKit, so
# pushing anywhere else fails at the server regardless of what the checkout
# says. Reads are anonymous over HTTPS and need no credential.
if [ -f /secrets/build_key ]; then
    install -d -m 0700 "$HOME/.ssh"
    install -m 0600 /secrets/build_key "$HOME/.ssh/id_ed25519"
    cat >> "$HOME/.ssh/config" <<'EOF'
Host github.com
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
    log "installed build deploy key"
else
    log "WARNING: no build key at /secrets/build_key -- pushing to the fork will fail"
    log "         generate and register one with: wk key register"
fi

# The snapshot already carries the right remotes (origin -> WebKit/WebKit,
# fork -> justinmichaud/WebKit with an SSH push URL), but they live in the
# read-only lower layer. Re-assert them here so a workspace created from an
# older snapshot still gets them, and so the push URL is present even if the
# snapshot predates it.
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" remote set-url origin https://github.com/WebKit/WebKit.git 2>/dev/null || true
    git -C "$SRC" remote add fork https://github.com/justinmichaud/WebKit.git 2>/dev/null \
        || git -C "$SRC" remote set-url fork https://github.com/justinmichaud/WebKit.git
    git -C "$SRC" remote set-url --push fork git@github.com:justinmichaud/WebKit.git
    # Pushing to origin is not possible (no write access upstream) and is
    # almost always a mistake; make it fail immediately rather than after a
    # round trip and an auth prompt.
    git -C "$SRC" remote set-url --push origin no-push://use-the-fork-remote
    log "remotes: origin=WebKit/WebKit (read), fork=justinmichaud/WebKit (push)"
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
