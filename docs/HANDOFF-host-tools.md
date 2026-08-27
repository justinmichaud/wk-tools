# HANDOFF — a minimal host

What a host (a workstation, the podman VM, a build box, the machine holding a
card reader) installs is exactly what has to run outside a sandbox: the
container/VM runtime, the egress proxy and broker that police it, the tailnet,
the display and GPU of a bench box, the card reader's media, the developer's
own identities. Everything a build or a benchmark needs lives in the
workspace image or the guest.

- [ ] `git-lfs` in host/linux/apt.txt: the mirror is bare and the macOS
      podman VM has no git-lfs, so Linux snapshots smudge LFS objects and
      macOS ones keep pointers; one behaviour -- drop it, or add it to
      host/macos/playbook.yaml [decision]
- [ ] the pmOS image builder's prerequisites (`multipath-tools`,
      `python3-venv`, `xz-utils`) are installed on every Linux host though
      only the aarch64 build host runs pmbootstrap; either the builder runs
      in a privileged container on that host, or its prerequisites move to
      a list image/pmos.sh installs there [decision]
- [ ] `claude` on the host: it is there to work on wk-tools itself; WebKit
      work uses the workspace's own copy -- decide whether wk-tools work
      moves into a workspace too, so the host copy goes [decision]
- [ ] `mesa-utils`, `wayland-utils`, `grim`: verification aids for the bench
      session; confirm `wk session` refuses without them or drop them
- [ ] `nmap` on macOS: `wk find` from a Mac needs it and `./setup` neither
      installs nor checks it; `wk doctor` names it as optional (`wk find`)
- [ ] every apt.txt entry names the `wk` command that needs it on the host;
      a test greps that each named command exists
