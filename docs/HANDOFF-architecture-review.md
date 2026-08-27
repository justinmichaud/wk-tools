# HANDOFF — architecture review and upstreaming pass

Not started; gated on the other open work under `docs/` — both settings audits, the sandbox and tailscale audits above all.

- [ ] fold the `home`/`lab`/`wk`/`field`/`stock` layering into directory moves plus a grep selftest that the lab layer knows no WebKit [decision]
- [ ] give the target-driver contract (`t_create`/`t_exec`/`t_info`/`t_list`/`t_destroy`, `t_pull`/`t_pull_dir`) a conformance test across `targets/container.sh`, `remote.sh`, `vm.sh`, folding in `boot/machines.sh`'s `b_*` drivers [decision]
- [ ] unify `cmd/backup` and the settings-audit workflow into one script with a per-platform adapter [needs docs/HANDOFF-settings-audit.md exercised on both platforms first]
- [ ] unify the four "get a thing out of an isolated workspace" copies: the egress proxy, `wk pi deploy`'s archive path, `wk sysimage write`'s image path, `t_pull`/`t_pull_dir` [decision]
- [ ] write up the rootless-podman + unix-socket egress-proxy design for other podman users [decision]
- [ ] check whether SDK patch 11 (`--isolated`) and patch 3 (`--unsafe-caps` gating) are already proposed upstream; file a PR if not [decision]
- [ ] upstream the two cross-compile commits onto `Igalia/webkit-container-sdk@main` (`docs/Nice to have/HANDOFF-cross-compile.md`) [decision]
- [ ] file the RPi5 NUMA Path A request: `CONFIG_NUMA_EMU` in stock linux-raspi [decision]
- [ ] upstream `gpr`/`wk pr` and the profiling wrappers to `Tools/Scripts` or a standalone toolkit [decision]
