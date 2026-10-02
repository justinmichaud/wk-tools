# The pi coding agent -- owed work

`wk ai pi <ws>` installs and launches it; `wk key set litellm` stores the key.
The reasoning -- the node version it needs, the npm install, what
`~/.pi/agent/models.json` carries -- lives in cmd/ai. What is not done:

- A live run on a macOS guest and on a build box (container is done).

- An interactive session (the TUI) in a container; only `-p` has been run.

- One `~/.pi/agent/models.json` per machine instead of one per workspace: `wk
  ai pi` writes it into each workspace on first use, so a workspace made
  tomorrow asks the endpoint again.
