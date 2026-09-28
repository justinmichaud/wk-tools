# The Claude CLI inside a workspace (container/firstrun.sh, lib/wk/guest.py): prints claude=present|installed|failed.
[ ! -r "$HOME/.wk-egress" ] || . "$HOME/.wk-egress"
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
    echo claude=present
    exit 0
fi
curl -fsSL https://claude.ai/install.sh | bash >/dev/null || { echo claude=failed; exit 1; }
echo claude=installed
