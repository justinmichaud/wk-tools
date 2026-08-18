# HANDOFF — remote development from a browser

Goal: do 100% of development from an iPad over tailscale.

Split before picking up — this is two or three separate tasks:

1. **code-server (or equivalent) in a workspace**, so a browser gets a real
   editor against the checkout, with clangd still available.
2. **A web interface to create/enter/tear down workspaces** over ssh — a thin
   UI over the existing driver contract, with the same cap the MCP server
   applies.
3. **Deployment** of that service via docker on the gateway host, reachable
   only over the tailnet.

Security note: each piece is new remote-attack surface and new workspace
egress; whatever lands here feeds the sandbox audit
(`docs/HANDOFF-sandboxing.md`) and the tailscale ACL audit
(`docs/HANDOFF-tailscale.md`).
