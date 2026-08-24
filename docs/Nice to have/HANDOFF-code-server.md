# HANDOFF — remote development from a browser

Goal: do 100% of development from an iPad over tailscale. Nothing started.

## Remaining

Split before picking up — this is three separate tasks:

1. **code-server (or equivalent) in a workspace**, so a browser gets a real
   editor against the checkout, with clangd still available.
2. **A web interface to create/enter/tear down workspaces** over ssh — a thin UI
   over the existing driver contract, with the same cap the MCP server applies.
3. **Deployment** of that service via docker on the gateway host, reachable only
   over the tailnet.

## Constraint

Each piece is new remote-attack surface and new workspace egress. Whatever lands
feeds `docs/HANDOFF-sandboxing.md` and `docs/Security/HANDOFF-tailscale.md` —
both of which are themselves unrun.
