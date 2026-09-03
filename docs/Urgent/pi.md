# The pi coding agent -- owed work

`wk ai pi <ws>` installs and launches it; `wk key set litellm` stores the key.
The reasoning -- the node version it needs, the npm install, what
`~/.pi/agent/models.json` carries -- lives in cmd/ai. What is not done:

- A live run in a workspace: whether the SDK image actually has a usable node
  and npm, whether the npm install completes through the egress allowlist, and
  which of pi's own startup requests the proxy refuses.

- One `~/.pi/agent/models.json` per machine instead of one per workspace: `wk
  ai pi` prints the file for a person to write, and a workspace made tomorrow
  needs it again.
