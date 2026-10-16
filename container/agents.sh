# lib/wk/agents.py's: `find <agent>` prints the one that runs, `node` asks whether pi's node runs; otherwise it installs what is missing, exiting 1 on a failure.
[ ! -r "$HOME/.wk-egress" ] || . "$HOME/.wk-egress"
PATH="$HOME/.local/bin:$PATH"
node_ok() {
    command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 \
        && node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a === 22 && b >= 19) ? 0 : 1)' 2>/dev/null
}
claude_install() {    # a refusal's body says why, so it is shown, never run
    s=$(mktemp) || return 1
    if curl -sSL --fail-with-body -o "$s" https://claude.ai/install.sh; then bash "$s" >/dev/null; else head -c 2000 "$s" >&2; false; fi
    r=$?; rm -f "$s"; return $r
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
elif npm install -g --ignore-scripts --prefix "$HOME/.local" @earendil-works/pi-coding-agent >/dev/null && found pi >/dev/null; then
    echo pi=installed
else
    echo pi=failed
    exit 1
fi
[ -f "$HOME/.pi/agent/models.json" ] && echo models=yes || echo models=no
