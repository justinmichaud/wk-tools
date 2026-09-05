# The config file is shared with other MCP servers, so merge rather than write.

_cfg="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

if [ ! -d "$(dirname "$_cfg")" ]; then
    debug "Claude Desktop is not installed; skipping MCP registration"
else
    _merged=$(WK_ROOT="$WK_ROOT" CFG="$_cfg" python3 - <<'PYEOF'
import json, os, sys

cfg_path = os.environ["CFG"]
wk_root = os.environ["WK_ROOT"]

try:
    with open(cfg_path) as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}

servers = cfg.setdefault("mcpServers", {})
want = {"command": os.path.join(wk_root, "cmd", "mcp")}

if servers.get("wk") == want:
    sys.exit(3)          # already correct; signal "no change"

servers["wk"] = want
print(json.dumps(cfg, indent=2))
PYEOF
) && _rc=0 || _rc=$?

    if [ "${_rc:-0}" -eq 3 ]; then
        unchanged "Claude Desktop MCP server"
    elif [ "${_rc:-0}" -ne 0 ]; then
        warn "could not update $_cfg"
    else
        printf '%s\n' "$_merged" > "$_cfg"
        changed "registered the wk MCP server with Claude Desktop"
        log "  restart Claude Desktop to pick it up"
    fi
fi

unset _cfg _merged _rc
