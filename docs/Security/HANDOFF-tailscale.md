# HANDOFF — audit the tailscale config

## Remaining

- [ ] Karen should only have access to immich, nc/nextcloud, overleaf and proxmox [decision]
- [ ] those apps should not access each other or anything else [decision]
- [ ] my two accounts should access everything [decision]
- [ ] decide the pi-hosts grant, covering `wk pi setup`'s `tag:wk` boards, and confirm it does not reuse `tag:server` (moose, nextcloud, immich, overleaf, the gateway) [decision, `docs/defects` 12]
- [ ] look for escalation opportunities in the config in general [needs a tailscale admin audit]
- [ ] decide the untrusted-router question, now including the tailnet bridges (PinePhone carrying the rpi4, Librem 5 in front of moose's BMC) and the camera streaming surface [decision, `docs/defects` 12]
- [ ] do a full port scan and confirm everything; other devices on the wifi/LAN are untrusted [needs a port scan]
- [ ] the MBP must be safe to use on public WiFi [decision]
- [ ] audit `wk pi setup`'s `tag:wk`/`pi-hosts` grant — it has never been run [needs Pi boards enrolled]
