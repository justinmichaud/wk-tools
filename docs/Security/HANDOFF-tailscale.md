Audit my tailscale config.

1) Karen should only have access to immich, nc/nextcloud, overleaf and proxmox
2) Those apps should not access each other or anything else
3) My two accounts should access everything
4) wk containers should access the rpi4 test board and the Pis' *bench
   systems* -- never the rpi5 workstation identity, which stays out of
   pi-hosts (docs/HANDOFF-benchmarking.md, the rpi5 model)
5) Look for escalation opportunities in this config in general
6) How can I prevent an untrusted router from escalating an attack on my network? How can I prevent local devices that are compromised from escalating an attack against my router or other devices?
7) Do a full port scan and confirm everything. Other devices on my wifi/lan network cannot be trusted.
8) The MBP must be safe to use on public WIFI
