# HANDOFF — helix

Container workspaces are done (`container/firstrun.sh`, `container/helix/`).

## Remaining

- Remote workspaces: `remote/provision.sh` installs nothing third-party on a
  shared machine and never uses root. Installing helix and lazygit there is a
  decision to take first; the install would go to `$HOME/.local/bin`.
- VM (macOS) workspaces: helix publishes macOS tarballs, but
  `container/helix/languages.toml` does not carry over -- `--malloc-trim` is
  glibc-only and the default `--compile-commands-dir` is `WebKitBuild/Release`
  for `mac-release`, not `WebKitBuild/JSCOnly/Release`. A macOS
  `languages.toml` is owed, verified against clangd in a guest.
- armhf: helix publishes no linux/armhf release; `_install_helix` reports it
  and continues. Building from source is its own project.
