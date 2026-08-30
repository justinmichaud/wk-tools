# HANDOFF — audit non-default host settings, ask before persisting

`wk backup` writes live settings into `host/linux/config.dconf` and `host/macos/defaults.conf`; this audits whether what's tracked is still deliberate.

- [ ] run `wk backup`, then `git diff` it; apply `cmd/backup`'s junk filters and fix the filter if it misses something; ask about everything left, keep or drop; write the kept set back and confirm the `./setup` round trip (macOS: `wk backup` on tolken reports no settings changed since the last backup, nothing to review) [needs a Linux workstation]
- [ ] write a short summary: what is persisted and why, one line each
- [ ] verify the dconf filters strip the known junk (weather location, WiFi UUIDs, GTK last-folder, Ptyxis UUIDs/timestamps)
- [ ] audit `apt.txt` for packages that crept in without a deliberate decision
- [ ] enumerate `defaults read` across macOS's ~574 domains, filter window-position/transient state, surface non-default candidates missing from `host/macos/defaults.conf`
- [ ] audit the rest of `host/macos/`: `symbolichotkeys.plist`, `softnet.sh`, `vmtools.sh`, `mcp.sh`, `tools.sh`, `playbook.yaml`
- [ ] confirm `wk backup` killed mid-write leaves repo files whole or unchanged (cmp-guarded), never truncated
