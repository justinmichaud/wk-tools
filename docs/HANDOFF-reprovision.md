# HANDOFF — wiping moose and tolken, and what to change while doing it

- [ ] wipe and rebuild in order: rpi5 first, then moose, then tolken — each gated on `wk doctor` reporting every machine-local entry regenerable/re-authable/backed-up, and `wk backup` + `git diff` showing nothing new [maintainer: credential/admin console]
- [ ] rename tolken's host user from `justinmichaud` to `jmichaud`, drop the `User` override in `dotfiles/ssh/config` and its warning comment, then grep for leftover `justinmichaud` paths (not the `github.com/justinmichaud/…` fork remotes) [maintainer]
- [ ] confirm the benchmark install's account is `bench`, not a person's name [maintainer]
- [ ] enable Remote Login on tolken (`sudo systemsetup -setremotelogin on`) and add each driving machine's key to `~/.ssh/authorized_keys`, per install (host and bench each have their own) [maintainer: credential/admin console]
- [ ] a second device: fresh clone + `./setup` picks up every registry machine as a target with no state copied from the first
- [ ] confirm `./setup` killed mid-stage still converges: a re-run completes only what's missing, a second full run reports no changes
- [ ] install Command Line Tools at bench-install time, since the benchmark driver's `prepare_env` does a bare `import objc` and only Apple's `/usr/bin/python3` has pyobjc [needs the Mac bench volume]
- [ ] confirm no FileVault on the bench install [needs the Mac bench volume]
- [ ] confirm 120 GB free in the APFS container before `wk bench mac-volume` (both installs share it) [needs the Mac bench volume]
- [ ] confirm a rebuild restores `$WK_STORE/pi-hosts` and the rpi5 tuning tree (`host/linux/rpi5/rpi5.conf`, gitignored)
- [ ] confirm `gh auth login` then `wk key register` after a rebuild ([maintainer] for the auth step)
- [ ] confirm `./setup` only verifies Tart's presence, not installs it (`~/.local/share/tart/tart.app`, `~/.local/bin/tart` symlinked into the bundle)
