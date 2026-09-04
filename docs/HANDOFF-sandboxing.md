# HANDOFF — the sandbox escape audit

Not yet run, on either platform. Scheduled last because everything else built changes the attack surface.

- [ ] re-attempt the incident list against the current tree: work overwritten, unauthorized GitHub posting/replying, ssh-key search on the host, a suid binary built to bypass auto mode, a sudo timestamp seat searched for [decision]
- [ ] audit the egress allowlist's general-purpose browsing set and CDNs against `BLOCKED_NETS` and the `pi-hosts` exemption (`container/proxy/wk-proxy.py`) [decision]
- [ ] the build wall is captured by anything that resolves a tool name on PATH and records the result: yocto's bitbake did it (`tmp/hosttools`), which `image/yocto-build.sh` now avoids by taking `container/bin` off PATH. Audit the other builders and any `configure`-style cache for the same shape rather than waiting for the next one to fail hours in [no hardware needed]
- [ ] audit `apt` access to the Ubuntu archive and `ddebs.ubuntu.com` from inside a workspace [decision]
- [ ] audit the fleet-request broker (`container/broker/wk-broker.py`): argument validation, whether `WK_FORCE` can be set from a request, the `keep` verb, the per-machine lock, and the macOS `ssh -R` forward [decision]
- [ ] audit the one read-write host mount: every container can write `~/.config/wk/agent-rw` (the claude.ai login the Claude CLI rotates in place), and an agent holding that credential can act as the account -- measure what a workspace can do with it and whether the directory can be narrowed further [decision]
- [ ] audit the widened API path: `api.github.com:443` is allowed and its TLS terminated by the injector, which is the only interception in the design. Reads are authenticated from a standing token in either switch position; every write is authenticated while push is on and unauthenticated while it is off, and the injector refuses nothing (`container/proxy/github-inject.py`). What is owed is whether the leaf key living permanently on the machine is the right trade, and what a workspace can reach with a read of any private repository the token can see [decision]
- [ ] install `wk-ssh-agent.service` from `remote/provision.sh` the way host/linux/sdk.sh does, so `wk push` on a build box throws a switch instead of reporting there is none, and `wk push --all` covers the fleet again
- [ ] audit remote build targets [decision]
- [ ] audit the tailnet bridges: camera streaming and the routed 10.99.x segment [decision]
- [ ] audit the device paths: `wk pi deploy`, `wk pi bench`, `wk sysimage write` [decision]
- [ ] audit the yocto build's own egress widening (`docs/HANDOFF-yocto.md`) [decision]
- [ ] audit the restored git helpers: `wk pr`, `wk pick`, `container/bin/git-sync-fork` [decision]
- [ ] run the audit again on macOS against the Tart VM model, including whether the golden base still provisions with unfiltered egress and whether the push switch behaves the same [needs a macOS Tart host]
- [ ] fix the rpi5's `(ALL) NOPASSWD: ALL` sudo grant via `wk sudo setup` [needs the rpi5]
- [ ] narrow moose's NOPASSWD `/usr/bin/tee` sudo grant [needs moose]
