# HANDOFF — remote development from a browser

## Remaining

- [ ] code-server (or equivalent) in a workspace, so a browser gets a real editor against the checkout with clangd available [decision]
- [ ] a web interface to create/enter/tear down workspaces over ssh, a thin UI over the existing driver contract with the same cap the MCP server applies, hooked into `wk status` [decision]
- [ ] deploy that service via docker on the gateway host, reachable only over the tailnet [needs the gateway host]
- [ ] make it easy to spawn new claude sessions in workspaces from a phone with no computer at hand [decision]
