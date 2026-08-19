# HANDOFF — this repo is public and carries private infrastructure details

**Closed 2026-08-19 by user decision: the repo stays public, and the internal
addressing in it is accepted as published.** Nothing below is a task any more.
It is kept as the record of what was weighed, so the same inventory does not
get re-discovered and re-escalated by the next pass over the tree.

## What is in-tree, and knowingly so

- `dotfiles/ssh/config` — internal hostnames and RFC1918 addresses for the
  whole home network, including the gateway. **Accepted.** They are private
  addresses: reaching them requires already being on the network or the
  tailnet, and the tailnet's own access control is audited separately
  (`docs/HANDOFF-tailscale.md`), which is where that boundary actually lives.
- `host/linux/rpi5/rpi5-setup.sh` — the WiFi SSID and BSSID. **Already out of
  HEAD**, fixed 2026-08-18 for a reason that was never about privacy: the
  identity now lives in a gitignored `rpi5.conf` and the BSSID is derived by
  scan (`auto`), so a router swap re-derives instead of stranding the box.
  Still in git history, and staying there.
- Assorted docs and command output name internal machines and services (moose,
  the BMC, nextcloud/immich/overleaf, the gateway). **Accepted**, and in the
  audit docs the names are the point.

None of it is a credential. `wk backup` excludes those, `wk verify` covers the
sdk-patches' security-relevant sections, and no key, token or password is in
the tree — that is the line that still matters, and it is the one to keep
checking. A future addition that crosses it is a bug regardless of this
decision.

## Consequences worth remembering

- **No history rewrite.** The published history stands, so nothing downstream
  of a clone breaks and no force-push is needed. This also means removing one
  of the items above from HEAD later would not un-publish it.
- The `/targets/local/` gitignore and the rpi5 `.conf` pattern stay the right
  home for *machine-local* data — not because the addressing is secret, but
  because per-machine values do not belong in a shared repo.
