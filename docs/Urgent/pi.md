# The pi coding agent -- owed work

`wk new` installs it (lib/wk/agents.py, container/agents.sh) and `wk ai pi <ws>`
launches it; `wk key set litellm` stores the key. What is not done:

- A live run on a macOS guest and on a build box (container is done).

- An interactive session (the TUI) in a container; only `-p` has been run.

- One `~/.pi/agent/models.json` per machine instead of one per workspace: `wk
  new` writes it into each workspace it makes, so a workspace made tomorrow
  asks the endpoint again.
