# lib/wk/agents.py's, with WK_AGENTS_CACHE and WK_CLAUDE_VERSION set: `find <agent>` prints the one that runs, `node` asks whether pi's node runs; otherwise it installs what is missing, exiting 1 on a failure.
[ ! -r "$HOME/.wk-egress" ] || . "$HOME/.wk-egress"
PATH="$HOME/.local/bin:$PATH"
node_ok() {
    command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 \
        && node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a === 22 && b >= 19) ? 0 : 1)' 2>/dev/null
}
claude_dir() { echo "$WK_AGENTS_CACHE/claude/$WK_CLAUDE_VERSION"; }
claude_fetch() {    # the one download, under the lock; a refusal's body says why, so it is shown, never run
    d=$(claude_dir)
    [ -d "$d" ] && return 0
    stage=$WK_AGENTS_CACHE/claude/.stage
    rm -rf "$stage"; mkdir "$stage" || return 1
    if curl -sSL --fail-with-body -o "$stage/install.sh" https://claude.ai/install.sh; then
        HOME=$stage bash "$stage/install.sh" "$WK_CLAUDE_VERSION" >/dev/null
        [ -x "$stage/.local/share/claude/versions/$WK_CLAUDE_VERSION" ] && rm "$stage/install.sh" && mv "$stage" "$d" && return 0
    else
        head -c 2000 "$stage/install.sh" >&2
    fi
    rm -rf "$stage"; return 1
}
export -f claude_fetch claude_dir
claude_install() {
    d=$(claude_dir)
    [ -d "$d" ] || { mkdir -p "$WK_AGENTS_CACHE/claude" && python3 -c 'import fcntl, subprocess, sys; lock = open(sys.argv[1], "w"); fcntl.flock(lock, fcntl.LOCK_EX); sys.exit(subprocess.call(sys.argv[2:]))' \
        "$WK_AGENTS_CACHE/claude.lock" bash -c claude_fetch; } || return 1
    mkdir -p "$HOME/.local/share" "$HOME/.local/bin" && cp -a "$d/.local/share/claude" "$HOME/.local/share/" || return 1
    ln -sf "$HOME/.local/share/claude/versions/$WK_CLAUDE_VERSION" "$HOME/.local/bin/claude"
}
found() {
    for c in "$HOME/.local/bin/$1" "$(command -v "$1" 2>/dev/null)"; do
        [ -n "$c" ] && [ -x "$c" ] && "$c" --version >/dev/null 2>&1 && { echo "$c"; return 0; }
    done
    return 1
}
case "${1:-}" in
    find) found "$2"; exit ;;
    node) node_ok; exit ;;
esac
if found claude >/dev/null; then
    echo claude=present
elif claude_install && found claude >/dev/null; then
    echo claude=installed
else
    echo claude=failed
    exit 1
fi
if found pi >/dev/null; then
    echo pi=present
elif ! node_ok; then
    echo "pi=no-node $(node -v 2>/dev/null || echo absent)"
elif npm_config_cache="$WK_AGENTS_CACHE/npm" npm install -g --ignore-scripts --prefer-offline --prefix "$HOME/.local" @earendil-works/pi-coding-agent >/dev/null && found pi >/dev/null; then
    echo pi=installed
else
    echo pi=failed
    exit 1
fi
[ -f "$HOME/.pi/agent/models.json" ] && echo models=yes || echo models=no
