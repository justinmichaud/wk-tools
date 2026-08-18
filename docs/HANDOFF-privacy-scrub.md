# HANDOFF — this repo is public and carries private infrastructure details

github.com/justinmichaud/wk-tools is PUBLIC. Found in-tree on 2026-08-18:

- `dotfiles/ssh/config` — internal hostnames and RFC1918 addresses for the
  whole home network, including the gateway.
- `host/linux/rpi5/rpi5-setup.sh` — the WiFi SSID **and BSSID** of the home
  network, pinned for roaming stability. **Fixed in HEAD 2026-08-18**: the
  identity now lives in a gitignored `rpi5.conf` and the BSSID is derived by
  scan (`auto`), so a router swap re-derives instead of stranding the box.
  The old values are still in git history.
- Assorted docs and command output name internal machines and services
  (moose, the BMC, nextcloud/immich/overleaf, the gateway) — mostly harmless
  individually, but together they map the network for anyone who looks.

None of it is a credential (`wk backup` correctly excludes those), but it is
more topology than a public repo should publish, and all of it also lives in
git history, not just HEAD.

## Decisions needed from the user (ask, don't guess)

1. Make the repo private, or keep it public and scrub?
2. If scrubbing: is rewriting published history acceptable, or scrub HEAD only
   and accept the history?
3. Where should the machine-local data live instead? The repo already has the
   pattern: `/targets/local/` is gitignored for exactly this reason, and the
   ssh aliases could move to the `~/.ssh/config.d/` include that setup already
   manages.

## Work, once decided

- Move `dotfiles/ssh/config`'s private entries to an untracked location that
  `./setup` still installs (document in SETUP.md that this file must be
  carried to a new machine by hand or via backup).
- Parameterise the rpi5 SSID/BSSID into an untracked conf next to the script.
- One pass over docs for gratuitous topology (keep names where they are the
  point, e.g. the tailscale audit doc).
