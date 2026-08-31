# HANDOFF — the sandbox escape audit

Not yet run, on either platform. Scheduled last because everything else built changes the attack surface.

- [ ] re-attempt the incident list against the current tree: work overwritten, unauthorized GitHub posting/replying, ssh-key search on the host, a suid binary built to bypass auto mode, a sudo timestamp seat searched for [decision]
- [x] disable `git commit` inside a container while an agent holds it: `wk claude` runs the agent under bwrap with `.git/{objects,refs,logs,HEAD,packed-refs}` read-only (commit_wall_prefix, lib/common.sh), unprivileged and un-escapable (no in-container sudo change needed -- the container has no SYS_ADMIN, so nothing inside can mount); `wk verify` proves it blocks a commit in the container; `wk push on[ --force]` is the one off switch; tests/test_commit_wall.py
- [ ] audit the egress allowlist's general-purpose browsing set and CDNs against `BLOCKED_NETS` and the `pi-hosts` exemption (`container/proxy/wk-proxy.py`) [decision]
- [ ] audit `apt` access to the Ubuntu archive and `ddebs.ubuntu.com` from inside a workspace [decision]
- [ ] audit the fleet-request broker (`container/broker/wk-broker.py`): argument validation, whether `WK_FORCE` can be set from a request, the `keep` verb, the per-machine lock, and the macOS `ssh -R` forward [decision]
- [ ] audit remote build targets [decision]
- [ ] audit the tailnet bridges: camera streaming and the routed 10.99.x segment [decision]
- [ ] audit the device paths: `wk pi deploy`, `wk pi bench`, `wk sysimage write` [decision]
- [ ] audit the yocto build's own egress widening (`docs/HANDOFF-yocto.md`) [decision]
- [ ] audit the restored git helpers: `wk pr`, `wk pick`, `container/bin/git-sync-fork` [decision]
- [ ] run the audit again on macOS against the Tart VM model, including whether the golden base still provisions with unfiltered egress and whether the push switch behaves the same [needs a macOS Tart host]
- [ ] fix the rpi5's `(ALL) NOPASSWD: ALL` sudo grant via `wk sudo setup` [needs the rpi5]
- [ ] narrow moose's NOPASSWD `/usr/bin/tee` sudo grant [needs moose]
