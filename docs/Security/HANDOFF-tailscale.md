# HANDOFF — audit the tailscale config

Not started; all eight items open.

## Remaining

1. Karen should only have access to immich, nc/nextcloud, overleaf and proxmox.
2. Those apps should not access each other or anything else.
3. My two accounts should access everything.
4. wk containers should access the rpi4 test board and the Pis' *bench systems*
   — never the rpi5 workstation identity, which stays out of `pi-hosts`.
5. Look for escalation opportunities in this config in general.
6. How can I prevent an untrusted router from escalating an attack on my
   network? How can I prevent compromised local devices from escalating against
   my router or other devices?
7. Do a full port scan and confirm everything. Other devices on my wifi/LAN
   cannot be trusted.
8. The MBP must be safe to use on public WiFi.

## Re-scope before starting — the surface has grown

- The **tailnet bridges** now route whole segments (`wk bridge`; the PinePhone
  carries the rpi4 at 10.99.1.10, the Librem 5 is next in front of moose's BMC
  at 10.99.0.2). Item 6's "untrusted router" question now has a device of ours
  in the middle of it, and the camera streaming is new external surface.
- `wk pi setup` tags boards `tag:wk` and feeds `pi-hosts`, which is what item 4
  is actually about — and it has never been run (`docs/HANDOFF-linux-pi.md`).
- `tag:server` covers moose, nextcloud, immich, overleaf and the gateway, and
  the Pi grant must not reuse it.

Findings feed `docs/HANDOFF-sandboxing.md`, whose items 5-7 are the same
question asked from the host side. Run the two together.
