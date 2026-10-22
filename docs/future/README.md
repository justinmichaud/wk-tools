# Future

Nothing in this directory is part of the tree's design, and no agent works on
it. It lists what the maintainer may want one day, kept so the idea is not
lost. An item moves out of here into README.md and a test when it has a user.

## Features with no user today

- Windows build machines.
- A rented cloud VM as a place, and PII-free perf testing on it.
- code-server, a web UI over the driver contract on the gateway, and sessions
  spawned from a phone.
- helix on a shared remote, and built from source for armhf.
- Haiku and BSD guests.
- iOS via public entitlements.
- Backing up Claude conversations and sharing memory across machines.
- devcontainers.
- moose as a bench machine (`--bootnext`, `toram`, UEFI rules, a reboot refused
  while its BMC is unreachable): its BMC was reached through a bridge phone,
  and the bridge is gone.

## By hand, for the maintainer

- Tailscale admin console: Karen limited to immich, nextcloud, overleaf and
  proxmox with those apps isolated from each other; both of your accounts
  reach everything; the untrusted-router question for the camera; the MBP on
  public WiFi; a full port scan.
- Delete the stray `refs/remotes/fork/dev/mya-1` on your WebKit fork.
- Supply the three memory patches `wk bench mem` builds with as real diffs
  (`container/bin/patches/` holds stubs).
- The clangd LSP errors and jump-to-definition misses need a person in
  `wk zed` on a real checkout.
