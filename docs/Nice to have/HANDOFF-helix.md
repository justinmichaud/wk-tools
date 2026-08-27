# HANDOFF — helix

## Remaining

- [ ] decide whether `remote/provision.sh` should install helix and lazygit to `$HOME/.local/bin` on a shared remote machine [decision]
- [ ] write a macOS `languages.toml` for VM workspaces (`--malloc-trim` is glibc-only, `--compile-commands-dir` defaults to `WebKitBuild/Release` not `WebKitBuild/JSCOnly/Release`) and verify it against clangd in a guest [needs a macOS VM]
- [ ] build helix from source for linux/armhf, since helix publishes no release for it [decision]
