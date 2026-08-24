# HANDOFF — architecture review and upstreaming pass

Not started, and deliberately last: the point is to look at the system as it
actually turned out, not as it was planned, so starting early means redoing it
after every later change. The gate is that the open work in `docs/` — both
settings audits, the sandbox and tailscale audits above all — has run.

Two lenses, run together because they find the same seams: **abstract what grew
twice**, so a future fix lands once; and **upstream what is not specific to this
machine or this person**.

Output is a short written decision per candidate — worth doing / not worth it /
needs a bigger conversation — and, for anything upstreamed, a link to the actual
PR or issue rather than an intent recorded here. It is a review, not a mandate
to execute all of it.

## Remaining — abstraction candidates

- **The layering** (`home` / `lab` / `wk` / `field` / `stock`, one-way
  dependency rule; recorded in `docs/HANDOFF-vocabulary.md` and binding for new
  code via `CLAUDE.md`). **Nothing has moved**: no directory moves, and no grep
  selftest that the lab layer knows no WebKit. The largest item here.
- **The target-driver contract** (`t_create`/`t_exec`/`t_info`/`t_list`/
  `t_destroy`, plus `t_pull`/`t_pull_dir`) is still three files agreeing by
  convention — `targets/container.sh`, `remote.sh`, `vm.sh` — with no
  conformance test. `boot/machines.sh`'s `b_*` drivers are a fourth
  driver-shaped thing to fold into the same question.
- **`cmd/backup` and the settings-audit workflow** are platform-branched doing
  the same round trip with different file formats. Worth one script with a
  per-platform adapter — check after `docs/HANDOFF-settings-audit.md` has
  exercised both halves.
- **"Get a thing out of an isolated workspace"** is now four copies, not the
  three originally listed: the egress proxy, `wk pi deploy`'s archive path,
  `wk sysimage write`'s image path, and `t_pull`/`t_pull_dir`.
- (Resolved in the building: the profiling wrappers became one command,
  `cmd/profile`, rather than four per-target scripts.)

## Remaining — upstreaming candidates

None filed.

- **The rootless-podman + unix-socket egress-proxy design** — `--network none`
  plus an allowlist-by-hostname proxy over one unix socket, so `wk` never needs
  root on Linux. A real answer to something rootless podman does not solve out
  of the box; worth writing up where podman users would find it.
- **The carried SDK patches** — patch 11 (`--isolated`: no host D-Bus, session
  bus or `$HOME` mounts) and patch 3 (gating `--unsafe-caps` behind an explicit
  flag rather than always disabling WebKit's own bwrap sandbox). Check whether
  they are already proposed upstream; if not, that is a PR rather than a patch
  to keep re-applying.
- **The two cross-compile commits** — `60bf63e60` and `3d8e55c92` onto
  `Igalia/webkit-container-sdk@main` (`docs/Nice to have/HANDOFF-cross-compile.md`).
- **RPi5 NUMA Path A** — the Launchpad request to enable `CONFIG_NUMA_EMU` in
  stock linux-raspi (Igalia authored the feature). Path B has validated the
  approach; the request is still unfiled (`host/linux/rpi5/HANDOFF.md`).
- **`gpr`/`wk pr` and the profiling wrappers** — generically useful to any
  WebKit contributor. `Tools/Scripts` upstream, or a linked standalone toolkit.
